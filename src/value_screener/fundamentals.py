"""Phase 3 — fundamentals from SEC XBRL companyfacts (one cached call per company).

Flow items (revenue, earnings, cash flow) become trailing-twelve-month values:
quarters come from 10-Q 3-month figures, from differencing year-to-date figures
(cash-flow statements are reported cumulatively: Q2 = H1 - Q1, Q4 = FY - 9M), or
from FY minus the three reported quarters. TTM = the 4 latest consecutive quarters;
otherwise the latest annual. Balance-sheet items take the latest reported value.

Each field records its source (``ttm`` / ``annual`` / ``instant``) and period end;
the row's ``as_of`` is the latest revenue / earnings period and ``stale`` flags data
older than ``stale_months``. Adapted from ``stock_research/edgar.py``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import yaml

from . import errors, history
from .config import Settings
from .sec import SecClient

FLOW_FIELDS_FOR_ASOF = ("revenue", "net_income")
# Proxy statements (DEF 14A and friends) tag net income in their pay table, sometimes in
# millions, and as the latest filing would replace the 10-K (FDX: $4,433). Other forms stay:
# S-1 and 6-K are the only source for recent IPOs and some foreign filers.
PROXY_FORM = "14A"
ROW_FIELDS = ("revenue", "net_income", "operating_income", "pretax_income", "interest_expense",
              "d_and_a", "eps_diluted", "operating_cash_flow", "capex", "dividends", "equity",
              "cash", "shares_outstanding", "assets", "assets_current", "liabilities",
              "liabilities_and_equity", "equity_incl_nci", "liabilities_current",
              "retained_earnings", "goodwill", "intangibles")
# Concepts the TTM row needs; the rest of xbrl_tags.yaml is for annual history only
# (and TTM construction is the slow part, so don't build what nobody reads).
PARTS_CONCEPTS = ("debt_parts_noncurrent", "debt_parts_current", "debt_parts_any")
MAX_FLOW_LAG_DAYS = 120     # about one missed quarterly filing
MAX_DEBT_LAG_DAYS = 400     # an annual-only debt tag is fine; older means it was abandoned
MAX_PARTS_LAG_DAYS = 120
MAX_IMPLIED_RATE = 0.25     # interest / debt above this means some of the debt isn't tagged
STUB_MIN_DAYS = 28          # a fiscal-year change can leave a one-month stub period
CHAIN_DAYS = (300, 430)     # chained periods this long can stand in for 12 months, scaled
TTM_CONCEPTS = frozenset(ROW_FIELDS) | {"debt_total", "debt_noncurrent", "debt_current",
                                        "notes_payable", "short_term_borrowings"}


def load_tags(config_dir: Path) -> dict:
    return yaml.safe_load((config_dir / "xbrl_tags.yaml").read_text(encoding="utf-8"))["concepts"]


def tags_fingerprint(tags_cfg: dict) -> str:
    """Short hash of the tag config; the slimmed-facts cache is keyed on it, so adding
    a tag triggers one cold refetch instead of silently reading slices without it."""
    blob = json.dumps(tags_cfg, sort_keys=True).encode("utf-8")
    return "t" + hashlib.sha1(blob).hexdigest()[:8]


def load_predecessors(config_dir: Path) -> dict[int, list[int]]:
    path = config_dir / "cik_predecessors.yaml"
    if not path.exists():
        return {}
    raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("predecessors") or {}
    return {int(k): [int(c) for c in v] for k, v in raw.items()}


def merge_slims(slims: list[dict]) -> dict:
    """Combine slimmed facts, oldest registrant first, successor last.

    Rows are simply pooled: the readers keep the latest *filed* row per period, so
    wherever periods overlap the successor's (later) filings win.
    """
    out = {"entity": slims[-1].get("entity"),
           "taxonomies": sorted({t for s in slims for t in s.get("taxonomies") or []}),
           "tags": {}}
    for s in slims:
        for tag, rows in (s.get("tags") or {}).items():
            out["tags"].setdefault(tag, []).extend(rows)
    return out


def _split_tag(tag: str) -> tuple[str, str]:
    return tuple(tag.split(":", 1)) if ":" in tag else ("us-gaap", tag)


def slim_facts(payload: dict, tags_cfg: dict) -> dict:
    """Cache transform: keep only configured tags, rows as [start, end, val, filed, form]."""
    facts = payload.get("facts") or {}
    out = {"entity": payload.get("entityName"), "taxonomies": sorted(facts), "tags": {}}
    for spec in tags_cfg.values():
        for tag in spec["tags"]:
            tax, name = _split_tag(tag)
            units = ((facts.get(tax) or {}).get(name) or {}).get("units") or {}
            rows = units.get(spec["unit"])
            if rows:
                out["tags"][tag] = [[r.get("start"), r.get("end"), r.get("val"), r.get("filed"),
                                     r.get("form"), r.get("accn")] for r in rows]
    return out


def _frame(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["start", "end", "val", "filed", "form", "accn"])
    for c in ("start", "end", "filed"):
        df[c] = pd.to_datetime(df[c], format="%Y-%m-%d", errors="coerce")  # ISO; no inference
    df["val"] = pd.to_numeric(df["val"], errors="coerce")
    df = df[~df["form"].str.contains(PROXY_FORM, na=False)]
    return df.dropna(subset=["end", "val"])


# --- flows ------------------------------------------------------------------- #

def _series(d: dict) -> pd.Series:
    """Float series on a DatetimeIndex (also when empty, so date comparisons work)."""
    return pd.Series(list(d.values()), index=pd.DatetimeIndex(list(d.keys())),
                     dtype=float).sort_index()


def quarters_and_annuals(rows) -> tuple[pd.Series, pd.Series]:
    """(quarterly values by period end, annual values by period end) for a flow tag."""
    df = _frame(rows).dropna(subset=["start"])
    if df.empty:
        return _series({}), _series({})
    df = df.sort_values("filed").drop_duplicates(["start", "end"], keep="last")  # restatements
    df["days"] = (df["end"] - df["start"]).dt.days

    q: dict[pd.Timestamp, float] = {}
    for _, r in df[df["days"].between(80, 100)].iterrows():
        q[r["end"]] = r["val"]
    # Year-to-date differencing: same start, durations one quarter apart.
    for _, g in df[df["days"] > 100].groupby("start"):
        g = g.sort_values("end")
        prior = df[(df["start"] == g["start"].iloc[0]) & df["days"].between(80, 100)]
        chain = pd.concat([prior, g]).sort_values("end")
        for a, b in zip(chain.itertuples(), chain.iloc[1:].itertuples()):
            if 80 <= (b.end - a.end).days <= 100 and b.end not in q:
                q[b.end] = b.val - a.val

    annual = df[df["days"].between(350, 380)].set_index("end")["val"].sort_index()
    # FY minus the three quarters inside it, when the 4th quarter is still missing.
    for end, val in annual.items():
        if end in q:
            continue
        start = end - pd.Timedelta(days=380)
        inside = [k for k in q if start < k < end - pd.Timedelta(days=60)]
        if len(inside) == 3:
            q[end] = val - sum(q[k] for k in inside)
    return _series(q), annual


def twelve_months(quarters: pd.Series, annual: pd.Series, end: pd.Timestamp):
    """(value, source) for the 12 months ending near ``end``, or (None, None).

    "Near" = within 15 days, so 52/53-week fiscal calendars (period ends that drift
    by a few days year to year) still line up.
    """
    tol = pd.Timedelta(days=15)
    near = annual[(annual.index >= end - tol) & (annual.index <= end + tol)]
    if not near.empty:
        return float(near.iloc[-1]), "annual"
    close = quarters[(quarters.index >= end - tol) & (quarters.index <= end + tol)]
    if close.empty:
        return None, None
    anchor = close.index[abs(close.index - end).argmin()]
    window = quarters[(quarters.index > anchor - pd.Timedelta(days=350)) & (quarters.index <= anchor)]
    if len(window) == 4 and 250 <= (window.index[-1] - window.index[0]).days <= 300:
        return float(window.sum()), "ttm"
    return None, None


def periods(rows) -> pd.DataFrame:
    """Every reported or year-to-date-derived period of 28-100 days: start, end, val.

    Unlike :func:`quarters_and_annuals`, keeps the short stubs a fiscal-year change leaves
    (Greif's Aug-Sep 2025 in its 10-KT; Ferguson's Nov-Dec 2025, its transition period less
    the quarter before it)."""
    df = _frame(rows).dropna(subset=["start"])
    if df.empty:
        return pd.DataFrame(columns=["start", "end", "val"])
    df = df.sort_values("filed").drop_duplicates(["start", "end"], keep="last")
    df["days"] = (df["end"] - df["start"]).dt.days
    out = [df.loc[df["days"].between(STUB_MIN_DAYS, 100), ["start", "end", "val"]]]
    for _, g in df.groupby("start"):
        g = g.sort_values("end")
        for a, b in zip(g.itertuples(), g.iloc[1:].itertuples()):
            if STUB_MIN_DAYS <= (b.end - a.end).days <= 100:
                out.append(pd.DataFrame({"start": [a.end + pd.Timedelta(days=1)], "end": [b.end],
                                         "val": [b.val - a.val]}))
    p = pd.concat(out, ignore_index=True)
    return p.drop_duplicates(["start", "end"], keep="first").sort_values("end").reset_index(drop=True)


def chained_months(p: pd.DataFrame, end: pd.Timestamp):
    """(value scaled to 12 months, days covered) from back-to-back periods ending at ``end``.

    For a fiscal-year change, when no four quarters line up: the chain of contiguous periods
    whose length is nearest a year, within ``CHAIN_DAYS``, scaled to 365 days. (None, None)
    when there's no such chain, so a gap in the filings never gets bridged."""
    best = None

    def walk(cur_start, total_val, first_end, pieces):
        nonlocal best
        days = (first_end - cur_start).days + 1
        if CHAIN_DAYS[0] <= days <= CHAIN_DAYS[1]:
            key = (abs(days - 365), pieces)
            if best is None or key < best[0]:
                best = (key, total_val * 365 / days, days)
        if days >= CHAIN_DAYS[1] or pieces >= 6:
            return
        prev = p[(p["end"] >= cur_start - pd.Timedelta(days=4)) & (p["end"] < cur_start)]
        for r in prev.itertuples():
            walk(r.start, total_val + r.val, first_end, pieces + 1)

    for r in p[p["end"] == end].itertuples():
        walk(r.start, r.val, end, 1)
    return (best[1], best[2]) if best else (None, None)


def fresh_start(rows) -> bool:
    """Whether an annual report splits its year into back-to-back partial periods: the
    predecessor and successor of a bankruptcy's fresh-start accounting (XBP Global, July 2025,
    whose one-month predecessor stub holds a $1.48B reorganization gain). Together the two make
    up one ordinary year: Jan-Jul plus Aug-Dec. A fiscal-year change's transition period
    doesn't, as it ends on a new year-end (Greif: a 9-month year-to-date and a 2-month stub)."""
    df = _frame(rows).dropna(subset=["start"])
    df = df[df["form"].str.startswith("10-K", na=False)]
    df = df.assign(days=(df["end"] - df["start"]).dt.days)
    part = df[(df["days"] >= STUB_MIN_DAYS) & ~df["days"].between(80, 100) & ~df["days"].between(350, 380)]
    for _, g in part.groupby("accn"):
        for a in g.itertuples():
            # Skydance's successor starts Aug 8, two days after its predecessor ends.
            after = g[(g["start"] > a.end) & (g["start"] <= a.end + pd.Timedelta(days=4))]
            if ((after["end"] - a.start).dt.days.between(350, 380)).any():
                return True
    return False


def flow_value(rows) -> dict:
    """Latest TTM (or annual) value, its source and end, plus the year-earlier value.

    When that falls behind the latest quarter reported, a fiscal-year change is usually why:
    the newest periods are then chained and scaled to a year (``src`` = ``"chained"``)."""
    q, annual = quarters_and_annuals(rows)
    ends = sorted(set(q.index) | set(annual.index), reverse=True)
    out = {"val": None, "src": None, "end": None, "prev": None}
    for end in ends:
        val, src = twelve_months(q, annual, end)
        if val is not None:
            prev, _ = twelve_months(q, annual, end - pd.DateOffset(years=1))
            out = {"val": val, "src": src, "end": end, "prev": prev}
            break
    p = periods(rows)
    if len(p) and (out["end"] is None or p["end"].max() > out["end"]) and not fresh_start(rows):
        newest = p["end"].max()
        val, _ = chained_months(p, newest)
        if val is not None:
            year_ago = newest - pd.DateOffset(years=1)
            prev, _ = twelve_months(q, annual, year_ago)
            if prev is None:
                near = p[(p["end"] - year_ago).abs() <= pd.Timedelta(days=15)]["end"]
                prev = chained_months(p, near.iloc[-1])[0] if len(near) else None
            out = {"val": val, "src": "chained", "end": newest, "prev": prev}
    return out


# --- instants ---------------------------------------------------------------- #

def instant_value(rows) -> dict:
    df = _frame(rows)
    if df.empty:
        return {"val": None, "src": None, "end": None}
    df = df.sort_values("filed").drop_duplicates(["end"], keep="last").sort_values("end")
    r = df.iloc[-1]
    return {"val": float(r["val"]), "src": "instant", "end": r["end"]}


def shares_value(rows) -> dict:
    """Latest share count, summed across classes reported in the same filing."""
    df = _frame(rows)
    if df.empty:
        return {"val": None, "src": None, "end": None}
    last_end = df["end"].max()
    latest = df[df["end"] == last_end]
    latest = latest[latest["accn"] == latest.sort_values("filed")["accn"].iloc[-1]]
    total = float(latest.drop_duplicates()["val"].sum())
    return {"val": total, "src": "instant", "end": last_end, "filed": latest["filed"].max()}


def _parts_at(slim: dict, spec: dict | None) -> list[tuple[pd.Timestamp, float]]:
    """(period end, value) of the latest row of each tag in a ``parts`` concept."""
    found = [instant_value(slim["tags"][t]) for t in (spec or {}).get("tags", []) if slim["tags"].get(t)]
    return [(v["end"], v["val"]) for v in found if v["end"] is not None]


def debt_from_parts(slim: dict, tags_cfg: dict, row: dict, as_of: pd.Timestamp) -> tuple | None:
    """(debt, period end) summed from the latest balance sheet's debt pieces, or None.

    Only some pieces may be tagged (DE's secured debt is $6B of a much larger total), so a
    sum that can't carry the interest bill, or exceeds total liabilities, is rejected.
    """
    groups = [_parts_at(slim, tags_cfg.get(k)) for k in PARTS_CONCEPTS]
    ends = [e for g in groups for e, _ in g if abs((as_of - e).days) <= MAX_PARTS_LAG_DAYS]
    if not ends:
        return None
    end = max(ends)
    noncurrent, current, other = (sum(v for e, v in g if e == end) for g in groups)
    debt = max(noncurrent + current, other)
    liabilities = row.get("liabilities") or row.get("liabilities_and_equity")
    if liabilities and debt > liabilities:
        return None
    interest, interest_end = row.get("interest_expense"), row.get("interest_expense_end")
    if interest and interest > 0 and interest_end is not None \
            and abs((as_of - interest_end).days) <= MAX_PARTS_LAG_DAYS \
            and interest > MAX_IMPLIED_RATE * debt:
        return None
    return debt, end


# --- per company -------------------------------------------------------------- #

def _best(slim: dict, spec: dict, reader) -> tuple[dict, str | None]:
    """Value from the tag with the most recent period (priority breaks ties)."""
    best, best_tag = {"val": None, "src": None, "end": None}, None
    for tag in spec["tags"]:
        rows = slim["tags"].get(tag)
        if not rows:
            continue
        v = reader(rows)
        if v["end"] is not None and (best["end"] is None or v["end"] > best["end"]):
            best, best_tag = v, tag
    return best, best_tag


def _pooled_rows(slim: dict, spec: dict) -> list:
    """Rows of all a concept's tags, the most recently used tag winning any shared period."""
    rows = {tag: [r for r in slim["tags"].get(tag) or [] if PROXY_FORM not in (r[4] or "")]
            for tag in spec["tags"]}
    seen, out = set(), []
    for tag in sorted(rows, key=lambda t: max((r[1] or "" for r in rows[t]), default=""), reverse=True):
        out += [r for r in rows[tag] if (r[0], r[1]) not in seen]
        seen |= {(r[0], r[1]) for r in rows[tag]}
    return out


def _stitch_lagging(slim: dict, tags_cfg: dict, vals: dict) -> None:
    """Rebuild revenue or earnings across tags when one tag can't reach the latest period.

    A filer switching tags mid-year leaves no tag with four straight quarters (CMI, PEP, and MU
    in June 2019, whose $30.4B from FY2018 sat beside trough earnings)."""
    ends = [vals[k]["end"] for k in FLOW_FIELDS_FOR_ASOF if vals[k]["end"] is not None]
    for key in FLOW_FIELDS_FOR_ASOF:
        end = vals[key]["end"]
        if ends and end is not None and (max(ends) - end).days > MAX_FLOW_LAG_DAYS:
            v = flow_value(_pooled_rows(slim, tags_cfg[key]))
            if v["end"] is not None and v["end"] > end:
                vals[key] = v


ANNUAL_FORMS = ("10-K", "10-KT", "20-F", "40-F")


def annual_form(slim: dict) -> str | None:
    """The form of the latest annual report in ``slim`` (10-K, 20-F, 40-F...), or None. A
    20-F or 40-F filer is foreign, even when a few of its tags are US GAAP (ASML, BHP)."""
    latest = max(((r[3], r[4].split("/")[0]) for rows in (slim.get("tags") or {}).values() for r in rows
                  if r[3] and r[4] and r[4].split("/")[0] in ANNUAL_FORMS), default=None)
    return latest[1] if latest else None


def extract(slim: dict, tags_cfg: dict, *, today: dt.date | None = None,
            stale_months: int = 15) -> dict:
    """One row of fundamentals from a slimmed companyfacts payload."""
    readers = {"flow": flow_value, "instant": instant_value, "shares": shares_value}
    vals: dict[str, dict] = {}
    empty = {"val": None, "src": None, "end": None}
    for key in TTM_CONCEPTS:
        spec = tags_cfg.get(key)
        vals[key] = _best(slim, spec, readers[spec["kind"]])[0] if spec else dict(empty)
    _stitch_lagging(slim, tags_cfg, vals)

    row: dict = {"entity": slim.get("entity"),
                 "us_gaap": "us-gaap" in (slim.get("taxonomies") or []),
                 "annual_form": annual_form(slim)}
    for key in ROW_FIELDS:
        v = vals[key]
        row[key] = v["val"]
        row[f"{key}_src"] = v["src"]
        row[f"{key}_end"] = v["end"]
    # A count filed after a split is already restated for it, whatever date it is as of.
    row["shares_outstanding_filed"] = vals["shares_outstanding"].get("filed")
    row["net_income_prev"] = vals["net_income"].get("prev")
    row["revenue_prev"] = vals["revenue"].get("prev")

    # Debt: a total tag or noncurrent + current, whichever is more recent; + short-term.
    tot, nonc, cur = vals["debt_total"], vals["debt_noncurrent"], vals["debt_current"]
    split_end = nonc["end"]
    use_split = split_end is not None and (tot["end"] is None or split_end > tot["end"])
    if use_split:
        debt = nonc["val"] + (cur["val"] if cur["end"] == split_end else 0.0)
        debt_end = split_end
    else:
        debt, debt_end = tot["val"], tot["end"]
    if debt is None and vals["notes_payable"]["val"] is not None:
        debt, debt_end = vals["notes_payable"]["val"], vals["notes_payable"]["end"]
    stb = vals["short_term_borrowings"]
    if debt is not None and stb["val"] is not None and stb["end"] == debt_end:
        debt += stb["val"]
    elif debt is None and stb["val"] is not None:
        debt, debt_end = stb["val"], stb["end"]

    ends = [row[f"{k}_end"] for k in FLOW_FIELDS_FOR_ASOF if row[f"{k}_end"] is not None]
    as_of = max(ends) if ends else None
    row["as_of"] = as_of
    # Still behind after stitching: treated as missing, as an old figure beside current
    # earnings misstates every ratio built on it. ``as_of`` still says what was filed.
    for key in FLOW_FIELDS_FOR_ASOF:
        if row[f"{key}_end"] is not None and (as_of - row[f"{key}_end"]).days > MAX_FLOW_LAG_DAYS:
            row[key] = row[f"{key}_src"] = row[f"{key}_end"] = row[f"{key}_prev"] = None
    row["total_debt_src"] = None if debt is None else "tag"
    if as_of is not None and (debt_end is None or (as_of - debt_end).days > MAX_DEBT_LAG_DAYS):
        parts = debt_from_parts(slim, tags_cfg, row, as_of)
        if parts:
            debt, debt_end = parts
            row["total_debt_src"] = "parts"
    row["total_debt"], row["total_debt_end"] = debt, debt_end
    today = pd.Timestamp(today or dt.date.today())
    row["stale"] = as_of is None or as_of < today - pd.DateOffset(months=stale_months)

    # Recent US GAAP income filings but no 12-month figure yet: a new registrant,
    # often a reorganization whose history sits under a predecessor CIK.
    latest_flow = pd.to_datetime(max((r[1] for k in FLOW_FIELDS_FOR_ASOF for t in tags_cfg[k]["tags"]
                                      for r in slim["tags"].get(t, []) if r[1]), default=None),
                                 errors="coerce")    # ISO strings sort as dates
    row["short_history"] = bool(as_of is None and pd.notna(latest_flow)
                                and latest_flow >= today - pd.DateOffset(months=9))
    return row


SHORT_HISTORY_MSG = ("short history: recent filings but no 12-month figures yet "
                     "(new registrant? add its old CIK to config/cik_predecessors.yaml)")


def parse_company(slims: list[dict], tags_cfg: dict, stale_months: int, history_years: int,
                  today: dt.date | None = None) -> tuple[dict, pd.DataFrame | None, list[tuple[str, str]]]:
    """Process-pool worker: (TTM row, annual history, [(stage, message)] to log).

    ``slims`` is predecessors first, the company itself last. Parsing is CPU-bound
    pandas work, so it runs in processes; SEC fetching stays in threads behind the
    one shared rate limiter.
    """
    slim = merge_slims(slims) if len(slims) > 1 else slims[0]
    row = extract(slim, tags_cfg, today=today, stale_months=stale_months)
    msgs = [("fundamentals", SHORT_HISTORY_MSG)] if row["short_history"] else []
    try:
        hist = history.company_history(slim, tags_cfg, max_years=history_years)
    except Exception as exc:  # noqa: BLE001 - history is optional per company
        hist = None
        msgs.append(("history", f"{type(exc).__name__}: {exc}"))
    return row, hist, msgs


def refresh(settings: Settings, client: SecClient, universe_df: pd.DataFrame,
            *, verbose: bool = True) -> pd.DataFrame:
    tags_cfg = load_tags(settings.config_dir)
    predecessors = load_predecessors(settings.config_dir)
    with_cik = universe_df.dropna(subset=["cik"])
    by_cik = with_cik.drop_duplicates("cik").set_index("cik")["ticker"]
    variant = tags_fingerprint(tags_cfg)
    slim_of = lambda c: client.company_facts(c, lambda p: slim_facts(p, tags_cfg), variant)
    n_procs = max(1, (os.cpu_count() or 2) - 1)
    gate = threading.BoundedSemaphore(4 * n_procs)     # bounds slims held in memory

    def fetch(cik, procs):
        ticker = by_cik.get(cik, str(cik))
        try:
            slim = slim_of(cik)
            if slim is None:
                errors.log(settings.data_dir, "fundamentals", ticker, "no companyfacts (404)")
                return int(cik), None
            olds = [s for s in (slim_of(p) for p in predecessors.get(int(cik), [])) if s]
            gate.acquire()
            fut = procs.submit(parse_company, [*olds, slim], tags_cfg,
                               settings.stale_months, settings.history_years)
            fut.add_done_callback(lambda _: gate.release())
            return int(cik), fut
        except Exception as exc:  # noqa: BLE001 - logged, run continues
            errors.log(settings.data_dir, "fundamentals", ticker, exc)
            return int(cik), None

    rows: dict[int, dict] = {}
    hist: dict[int, pd.DataFrame] = {}
    with ProcessPoolExecutor(n_procs) as procs, ThreadPoolExecutor(settings.sec_workers) as pool:
        futures = dict(pool.map(lambda c: fetch(c, procs), by_cik.index))
        for cik, fut in futures.items():
            if fut is None:
                continue
            ticker = by_cik.get(cik, str(cik))
            try:
                row, h, msgs = fut.result()
            except Exception as exc:  # noqa: BLE001 - logged, run continues
                errors.log(settings.data_dir, "fundamentals", ticker, exc)
                continue
            row["predecessor_ciks"] = ",".join(map(str, predecessors.get(cik, []))) or None
            rows[cik] = row
            if h is not None:
                hist[cik] = h
            for stage, msg in msgs:
                errors.log(settings.data_dir, stage, ticker, msg)

    out = []
    for t, cik in zip(universe_df["ticker"], universe_df["cik"]):
        r = rows.get(int(cik)) if pd.notna(cik) else None
        out.append({"ticker": t, **(r or {})})
    df = pd.DataFrame(out)
    df.to_parquet(settings.data_dir / "fundamentals.parquet", index=False)
    hist_df = history.to_frame(hist)
    hist_df.to_parquet(history.path(settings), index=False)
    if verbose:
        have = df["revenue"].notna() if "revenue" in df else pd.Series(False, index=df.index)
        short = int(df.get("short_history", pd.Series(dtype=bool)).fillna(False).sum())
        print(f"Fundamentals: {int(have.sum())}/{len(df)} with revenue; "
              f"{int(df.get('stale', pd.Series(dtype=bool)).fillna(True).sum())} stale or missing; "
              f"{short} short history (see errors.csv)")
        years = hist_df.groupby("cik")["fy_end"].nunique() if len(hist_df) else pd.Series(dtype=int)
        print(f"History: {len(years)} companies, median {years.median():.0f} fiscal years, "
              f"{int((years >= 7).sum())} with >= 7")
    return df


def load(settings: Settings) -> pd.DataFrame:
    return pd.read_parquet(settings.data_dir / "fundamentals.parquet")

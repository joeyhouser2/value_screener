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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import yaml

from . import errors
from .config import Settings
from .sec import SecClient

FLOW_FIELDS_FOR_ASOF = ("revenue", "net_income")


def load_tags(config_dir: Path) -> dict:
    return yaml.safe_load((config_dir / "xbrl_tags.yaml").read_text(encoding="utf-8"))["concepts"]


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
        df[c] = pd.to_datetime(df[c], errors="coerce")
    df["val"] = pd.to_numeric(df["val"], errors="coerce")
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


def flow_value(rows) -> dict:
    """Latest TTM (or annual) value, its source and end, plus the year-earlier value."""
    q, annual = quarters_and_annuals(rows)
    ends = sorted(set(q.index) | set(annual.index), reverse=True)
    for end in ends:
        val, src = twelve_months(q, annual, end)
        if val is not None:
            prev, _ = twelve_months(q, annual, end - pd.DateOffset(years=1))
            return {"val": val, "src": src, "end": end, "prev": prev}
    return {"val": None, "src": None, "end": None, "prev": None}


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
    return {"val": total, "src": "instant", "end": last_end}


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


def extract(slim: dict, tags_cfg: dict, *, today: dt.date | None = None,
            stale_months: int = 15) -> dict:
    """One row of fundamentals from a slimmed companyfacts payload."""
    readers = {"flow": flow_value, "instant": instant_value, "shares": shares_value}
    vals: dict[str, dict] = {}
    for key, spec in tags_cfg.items():
        vals[key], _ = _best(slim, spec, readers[spec["kind"]])

    row: dict = {"entity": slim.get("entity"),
                 "us_gaap": "us-gaap" in (slim.get("taxonomies") or [])}
    for key in ("revenue", "net_income", "operating_income", "d_and_a", "eps_diluted",
                "operating_cash_flow", "capex", "dividends", "equity", "cash",
                "shares_outstanding"):
        v = vals[key]
        row[key] = v["val"]
        row[f"{key}_src"] = v["src"]
        row[f"{key}_end"] = v["end"]
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
    stb = vals["short_term_borrowings"]
    if debt is not None and stb["val"] is not None and stb["end"] == debt_end:
        debt += stb["val"]
    elif debt is None and stb["val"] is not None:
        debt, debt_end = stb["val"], stb["end"]
    row["total_debt"], row["total_debt_end"] = debt, debt_end

    ends = [row[f"{k}_end"] for k in FLOW_FIELDS_FOR_ASOF if row[f"{k}_end"] is not None]
    as_of = max(ends) if ends else None
    row["as_of"] = as_of
    today = pd.Timestamp(today or dt.date.today())
    row["stale"] = as_of is None or as_of < today - pd.DateOffset(months=stale_months)
    return row


def refresh(settings: Settings, client: SecClient, universe_df: pd.DataFrame,
            *, verbose: bool = True) -> pd.DataFrame:
    tags_cfg = load_tags(settings.config_dir)
    with_cik = universe_df.dropna(subset=["cik"])
    by_cik = with_cik.drop_duplicates("cik").set_index("cik")["ticker"]

    def fetch(cik):
        ticker = by_cik.get(cik, str(cik))
        try:
            slim = client.company_facts(cik, lambda p: slim_facts(p, tags_cfg))
            if slim is None:
                errors.log(settings.data_dir, "fundamentals", ticker, "no companyfacts (404)")
                return int(cik), None
            return int(cik), extract(slim, tags_cfg, stale_months=settings.stale_months)
        except Exception as exc:  # noqa: BLE001 - logged, run continues
            errors.log(settings.data_dir, "fundamentals", ticker, exc)
            return int(cik), None

    with ThreadPoolExecutor(settings.sec_workers) as pool:
        rows = dict(pool.map(fetch, by_cik.index))

    out = []
    for t, cik in zip(universe_df["ticker"], universe_df["cik"]):
        r = rows.get(int(cik)) if pd.notna(cik) else None
        out.append({"ticker": t, **(r or {})})
    df = pd.DataFrame(out)
    df.to_parquet(settings.data_dir / "fundamentals.parquet", index=False)
    if verbose:
        have = df["revenue"].notna() if "revenue" in df else pd.Series(False, index=df.index)
        print(f"Fundamentals: {int(have.sum())}/{len(df)} with revenue; "
              f"{int(df.get('stale', pd.Series(dtype=bool)).fillna(True).sum())} stale or missing")
    return df


def load(settings: Settings) -> pd.DataFrame:
    return pd.read_parquet(settings.data_dir / "fundamentals.parquet")

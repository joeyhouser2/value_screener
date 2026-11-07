"""Phase 10c — survivorship: who was listed at each panel date but is missing from the panel.

The panel replays today's universe only, so a company delisted since a date is absent from that
date's screen. This measures the hole from EDGAR alone, without any delisted prices:

- Delistings: an exchange files form 25-NSE when it removes a security, and an issuer files
  form 25 to withdraw one, both under the issuer's CIK (EDGAR's quarterly form indexes).
- Missing: delisted, not in today's universe or a predecessor of a company in it, and a 10-K
  filer in the two years before. 20-F and 40-F filers are out of the panel anyway.
- Size: ``dei:EntityPublicFloat`` from SEC's frames API, the latest value before the date.
  Float leaves out affiliates' shares, so it understates market cap and puts names in a
  smaller bucket than their cap would.
- Why it left, as far as filings show: ``acquired`` if a merger proxy, tender offer, going-private
  schedule or merger communication (425) was filed under its CIK in the 3 years before. ``reorganized`` if a
  company in today's universe shares the first word of its name and began filing within a
  year of the delisting (Apache -> APA, BlackRock, Ashland): it still trades under a new CIK,
  which ``cik_predecessors.yaml`` can link. The rest are bankruptcies, failures to meet
  listing rules, going dark, and reorganizations the name test misses (WestRock's WRKCo).
  ``spac`` marks a blank-check company by name, as a liquidation returns its cash; ``failed``
  is everything else. ``still_filing`` marks a company that filed a
  10-K over a year after delisting (moved to OTC, or listed only debt or preferred).

:func:`erase_loss` is the stress test: the loss on the missing names that would erase a
screen's excess return. It needs the backtest's returns, so 10d applies it.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import numpy as np
import pandas as pd

from .. import fundamentals, universe
from ..config import Settings
from ..sec import SecClient
from . import panel

INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx"
FRAME_URL = "https://data.sec.gov/api/xbrl/frames/dei/EntityPublicFloat/USD/CY{year}Q{q}I.json"
ANNUAL = {"10-K", "10-KT"}
DELISTING = {"25-NSE", "25"}
# 425s (merger communications) are indexed under the target too, so stock deals count (Sprint)
MERGER = {"DEFM14A", "DEFM14C", "SC 14D9", "SC TO-T", "SC 13E3", "425"}
KEPT = ANNUAL | DELISTING | MERGER
INDEX_VERSION = "v2"             # bump when KEPT changes: the cache holds only the kept forms
INDEX_START_YEAR = 2011          # a year before the panel, to see who was filing by its first date
DOMESTIC_DAYS = 730              # a 10-K this close before delisting makes a domestic filer
MERGER_DAYS = 1095               # a merger filing this close before delisting marks an acquisition:
                                 # regulators held Monsanto and Activision for 2 years
REORG_DAYS = 365                 # a successor whose filings start this close to the delisting
MERGER_AFTER_DAYS = 30
STILL_FILING_DAYS = 400
FLOAT_MAX_AGE_DAYS = 456         # floats are annual: 15 months
FINAL_TTL_DAYS = 36500           # a closed quarter's index never changes
# Form, then name, padded by 2+ spaces; read from the right, as names can hold double spaces.
ROW = re.compile(r"^(?P<form>\S.*?)\s{2,}(?P<name>.*?)\s+(?P<cik>\d+)\s+(?P<filed>\d{4}-\d{2}-\d{2})\s+\S+\s*$")
# Blank-check companies: a liquidation hands back the trust's cash, so it isn't a loss.
SPAC = re.compile(r"acquisition (?:corp|co\b|company|ltd|inc)|merger (?:corp|sub)|tontine|hedosophia|blank check", re.I)
SIZE_BUCKETS = [0, 50e6, 300e6, 2e9, 10e9, np.inf]
SIZE_LABELS = ["under $50M", "$50-300M", "$300M-2B", "$2-10B", "over $10B"]


def parse_form_index(text: str, forms=KEPT) -> list[dict]:
    """Rows of a ``form.idx`` whose form is in ``forms``: form, name, cik, filed (ISO date)."""
    lines = text.splitlines()
    head = next(i for i, line in enumerate(lines) if line.startswith("Form Type"))
    out = []
    for line in lines[head + 2:]:
        m = ROW.match(line)
        if m and m["form"] in forms:
            out.append({"form": m["form"], "name": m["name"], "cik": int(m["cik"]), "filed": m["filed"]})
    return out


def quarters(start_year: int, today: dt.date) -> list[tuple[int, int]]:
    last = (today.year, (today.month - 1) // 3 + 1)
    return [(y, q) for y in range(start_year, today.year + 1) for q in range(1, 5) if (y, q) <= last]


def filings(client: SecClient, today: dt.date | None = None, *, verbose: bool = True) -> pd.DataFrame:
    """The kept forms from every quarterly index since ``INDEX_START_YEAR``, cached."""
    today = today or dt.date.today()
    current = (today.year, (today.month - 1) // 3 + 1)
    rows = []
    for y, q in quarters(INDEX_START_YEAR, today):
        got = client.get_records(INDEX_URL.format(year=y, q=q), f"full_index/{INDEX_VERSION}/{y}Q{q}", parse_form_index,
                                 ttl_days=1 if (y, q) == current else FINAL_TTL_DAYS)
        rows.extend(got or [])
        if verbose:
            print(f"\rForm indexes: {y} Q{q} ({client.calls} downloaded)", end="", flush=True)
    if verbose:
        print()
    df = pd.DataFrame(rows, columns=["form", "name", "cik", "filed"])
    df["filed"] = pd.to_datetime(df["filed"])
    return df


def public_floats(client: SecClient, today: dt.date | None = None) -> pd.DataFrame:
    """cik, end, float: every ``EntityPublicFloat`` SEC's frames hold since the index start."""
    today = today or dt.date.today()
    rows = []
    for y, q in quarters(INDEX_START_YEAR - 1, today):
        got = client.get_json(FRAME_URL.format(year=y, q=q), f"frames/EntityPublicFloat_CY{y}Q{q}I")
        for d in (got or {}).get("data", []):
            rows.append({"cik": int(d["cik"]), "end": d["end"], "float": float(d["val"])})
    df = pd.DataFrame(rows, columns=["cik", "end", "float"])
    df["end"] = pd.to_datetime(df["end"])
    return df.drop_duplicates(["cik", "end"]).sort_values(["cik", "end"]).reset_index(drop=True)


def _first_word(name: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", " ", str(name).lower()).split()
    words = [w for w in words if w != "the"]
    return words[0] if words else ""


def successors(missing: pd.DataFrame, forms: pd.DataFrame, today_names: pd.Series) -> pd.Series:
    """For each missing company, a CIK in today's universe that looks like its reorganized self.

    ``today_names`` maps today's CIKs to names. A match shares the first word of the name (of
    3+ letters) and filed nothing before ``REORG_DAYS`` ahead of the delisting.
    """
    first_filed = forms.groupby("cik")["filed"].min()
    today = pd.DataFrame({"word": today_names.map(_first_word), "first": first_filed.reindex(today_names.index)})
    today = today[today["word"].str.len() >= 3]
    out = {}
    for row in missing.itertuples():
        word = _first_word(row.name)
        cand = today[(today["word"] == word) & (today["first"] >= row.delisted - pd.Timedelta(days=REORG_DAYS))
                     & (today["first"] <= row.delisted + pd.Timedelta(days=REORG_DAYS))]
        if len(cand):
            out[row.cik] = int(cand.index[0])
    return pd.Series(out, dtype="Int64")


def missing_companies(forms: pd.DataFrame, known_ciks: set[int], today_names: pd.Series | None = None) -> pd.DataFrame:
    """One row per delisted domestic filer missing from today's universe (see module docstring)."""
    delist = forms[forms["form"].isin(DELISTING)].groupby("cik")["filed"].max().rename("delisted")
    delist = delist[~delist.index.isin(known_ciks)]
    annual = forms[forms["form"].isin(ANNUAL)]
    first = annual.groupby("cik")["filed"].min().rename("first_10k")
    names = forms.sort_values("filed").groupby("cik")["name"].last()
    df = delist.to_frame().join(first, how="inner").join(names)

    def any_between(form_set, lo_days, hi_days):
        f = forms[forms["form"].isin(form_set)][["cik", "filed"]].merge(df["delisted"].reset_index(), on="cik")
        gap = (f["delisted"] - f["filed"]).dt.days
        return f.loc[(gap <= lo_days) & (gap >= -hi_days), "cik"].unique()

    domestic = any_between(ANNUAL, DOMESTIC_DAYS, 0)
    df = df[df.index.isin(domestic)].copy()
    df["acquired"] = df.index.isin(any_between(MERGER, MERGER_DAYS, MERGER_AFTER_DAYS))
    df["still_filing"] = df.index.isin(any_between(ANNUAL, -STILL_FILING_DAYS, 10**6))
    df = df.rename_axis("cik").reset_index()
    names = today_names if today_names is not None else pd.Series(dtype=object)
    df["successor"] = df["cik"].map(successors(df, forms, names)).astype("Int64")
    df["reorganized"] = df["successor"].notna() & ~df["acquired"]
    df["spac"] = df["name"].fillna("").str.contains(SPAC) & ~df["acquired"] & ~df["reorganized"]
    df["failed"] = ~df["acquired"] & ~df["reorganized"] & ~df["spac"]
    return df


def float_at(floats: pd.DataFrame, ciks, date: pd.Timestamp) -> pd.Series:
    """Each CIK's latest public float with a date in the 15 months up to ``date``."""
    f = floats[floats["cik"].isin(set(ciks)) & (floats["end"] <= date)
               & (floats["end"] > date - pd.Timedelta(days=FLOAT_MAX_AGE_DAYS))]
    return f.sort_values("end").groupby("cik")["float"].last()


def bucket(values: pd.Series) -> pd.Series:
    return pd.cut(values, SIZE_BUCKETS, labels=SIZE_LABELS, right=False)


def hole_at(missing: pd.DataFrame, floats: pd.DataFrame, date: dt.date) -> pd.DataFrame:
    """The missing companies listed at ``date``: filing by then and delisted after it."""
    day = pd.Timestamp(date)
    m = missing[(missing["first_10k"] <= day) & (missing["delisted"] > day)].copy()
    m["float"] = m["cik"].map(float_at(floats, m["cik"], day))
    m["size"] = bucket(m["float"])
    return m


def hit_rates(screen: pd.DataFrame, columns=("deep_value", "cyclical_trough")) -> pd.DataFrame:
    """Share of the panel's stocks in each screen, by market-cap bucket."""
    s = screen[screen["market_cap"].notna()].copy()
    s["size"] = bucket(s["market_cap"])
    g = s.groupby("size", observed=False)
    out = pd.DataFrame({"stocks": g.size()})
    for c in columns:
        out[c] = g[c].apply(lambda x: x.fillna(False).astype(bool).mean())
    return out


def erase_loss(excess: float, held: float, missing: float, screen_return: float) -> float:
    """The loss on each missing name (1 = total loss) at which the screen's excess is erased.

    The missing names join the ``held`` ones at equal weight with a return of minus that loss.
    Over 1 means even total losses couldn't erase it; NaN when nothing is missing.
    """
    if missing <= 0:
        return np.nan
    return excess * (held + missing) / missing - screen_return


def summary(settings: Settings, missing: pd.DataFrame, floats: pd.DataFrame, dates: list[dt.date]) -> pd.DataFrame:
    """Per panel date: stocks in the panel, missing companies by size and cause, and the
    screen members those missing companies would add at the panel's own hit rates."""
    floor = settings.screen_min_market_cap
    rows = []
    for date in dates:
        path = panel.panel_path(settings, date)
        if not path.exists():
            continue
        p = pd.read_parquet(path)
        hole = hole_at(missing, floats, date)
        big = hole[hole["float"] >= floor]
        rates = hit_rates(p)
        sizes = big["size"].value_counts().reindex(SIZE_LABELS, fill_value=0)
        held = (p["market_cap"] >= floor).sum()
        row = {"date": date, "panel": int(held), "missing": len(big), "missing_no_float": int(hole["float"].isna().sum()),
               "acquired": int(big["acquired"].sum()), "reorganized": int(big["reorganized"].sum()),
               "spac": int(big["spac"].sum()), "failed": int(big["failed"].sum()),
               "hole_share": len(big) / (held + len(big)) if held + len(big) else np.nan,
               **{f"missing {k}": int(v) for k, v in sizes.items() if k != SIZE_LABELS[0]}}
        for c in ("deep_value", "cyclical_trough"):
            row[f"{c}_held"] = int(p[c].fillna(False).astype(bool).sum())
            row[f"{c}_missing"] = float((sizes * rates[c].reindex(SIZE_LABELS).fillna(0)).sum())
            other = big[big["failed"]]["size"].value_counts().reindex(SIZE_LABELS, fill_value=0)
            row[f"{c}_missing_failed"] = float((other * rates[c].reindex(SIZE_LABELS).fillna(0)).sum())
        rows.append(row)
    return pd.DataFrame(rows)


def failure_drag(settings: Settings, missing: pd.DataFrame, floats: pd.DataFrame, summ: pd.DataFrame) -> pd.DataFrame:
    """Per calendar year: the failures (missing, not acquired, reorganized or a SPAC) delisted in
    it, how many each screen would have held at the start of the year at the panel's hit rates,
    and the drag if they were held all year and went to zero: that count over the screen's full
    size, held plus all the missing names it would have had. A screen whose yearly excess is
    below this drag can't be told apart from survivorship."""
    floor = settings.screen_min_market_cap
    rows = []
    for _, s in summ[pd.to_datetime(summ["date"]).dt.month == 12].iterrows():
        start = s["date"]
        p = pd.read_parquet(panel.panel_path(settings, start))
        rates = hit_rates(p)
        hole = hole_at(missing, floats, start)
        failed = hole[(hole["float"] >= floor) & hole["failed"]
                      & (hole["delisted"] <= pd.Timestamp(start) + pd.DateOffset(years=1))]
        sizes = failed["size"].value_counts().reindex(SIZE_LABELS, fill_value=0)
        row = {"year": start.year + 1, "failures": len(failed)}
        for c in ("deep_value", "cyclical_trough"):
            held_failed = float((sizes * rates[c].reindex(SIZE_LABELS).fillna(0)).sum())
            full = s[f"{c}_held"] + s[f"{c}_missing"]
            row[f"{c}_failures"] = held_failed
            row[f"{c}_drag"] = held_failed / full if full else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def known_ciks(settings: Settings, uni: pd.DataFrame) -> set[int]:
    preds = fundamentals.load_predecessors(settings.config_dir)
    return {int(c) for c in uni["cik"].dropna()} | {p for ps in preds.values() for p in ps}


def path(settings: Settings) -> Path:
    return settings.data_dir / "backtest" / "survivorship.parquet"


def run(settings: Settings, client: SecClient, *, verbose: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    forms = filings(client, verbose=verbose)
    floats = public_floats(client)
    uni = universe.load(settings)
    listed = uni.dropna(subset=["cik"]).drop_duplicates("cik")
    names = pd.Series(listed["name"].to_numpy(), index=listed["cik"].astype(int))
    missing = missing_companies(forms, known_ciks(settings, uni), names)
    missing.to_parquet(path(settings), index=False)
    dates = panel.quarter_ends(settings.backtest_start, dt.date.today())
    return missing, summary(settings, missing, floats, dates)

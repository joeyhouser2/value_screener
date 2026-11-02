"""Phase 10a — forward tracking: how have the screens done since they were recorded?

The one test free of look-ahead and survivorship, since members are recorded before their
returns exist. It needs years to mean anything: until then it only keeps the record.

- A cohort is one snapshot from ``data/history/``, at least ``track_cohort_days`` after the
  last, equal weight and held since, so cohorts barely overlap. Returns are before costs.
- Entry is the first close after the snapshot date, the same for stocks and benchmarks.
  Today's bar is ignored, as during market hours it isn't a close.
- Members are written to ``data/track/positions.parquet`` once and never reread, so later
  rule changes or a rerun snapshot can't rewrite the record. Returns update on each run.
- A stock Yahoo stops pricing keeps its last known return and is flagged ``gone``. It may
  have been bought out or may be worthless, so ``ret_if_gone_lost`` counts it as a total loss.
- The cheapest ``track_baseline_pct`` by earnings yield is tracked alongside the screens: if
  it does as well, they add nothing, and it can't be rebuilt later once its losers vanish.
"""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path
from typing import Protocol

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from . import errors, screens, trend
from .config import Settings

BENCHMARKS = ("SPY", "RSP", "IWD")
BASELINE = "Cheapest earnings yield"
COLUMNS = ["cohort", "screen", "ticker", "entry_date", "ret", "last_date", "gone"]
NEEDED = ("ticker", "market_cap", "pe", "stale", *screens.SCREENS.values())


class CloseSource(Protocol):
    def closes(self, tickers: list[str], start: dt.date) -> pd.DataFrame:
        """Adjusted closes, one column per ticker (tickers without data omitted)."""


class YahooCloses:
    def __init__(self, settings: Settings):
        self.s = settings

    def closes(self, tickers: list[str], start: dt.date) -> pd.DataFrame:
        out, todo = [], list(tickers)
        for attempt in range(self.s.trend_retries + 1):
            if attempt:
                time.sleep(self.s.trend_retry_pause)
            for i in range(0, len(todo), self.s.price_chunk):
                batch = todo[i:i + self.s.price_chunk]
                data = trend.download_batch(batch, start, self.s.trend_batch_timeout)
                if data is not None and not data.empty and "Adj Close" in data:
                    adj = data["Adj Close"]
                    out.append(adj.to_frame(batch[0]) if isinstance(adj, pd.Series) else adj)
            got = {c for f in out for c in f.columns[f.notna().any()]}
            todo = [t for t in todo if t not in got]
            if not todo:
                break
        if not out:
            return pd.DataFrame()
        df = pd.concat(out, axis=1).dropna(axis=1, how="all")
        return df.loc[:, ~df.columns.duplicated()]


def positions_path(settings: Settings) -> Path:
    return settings.data_dir / "track" / "positions.parquet"


def load_positions(settings: Settings) -> pd.DataFrame:
    path = positions_path(settings)
    return pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=COLUMNS)


def snapshots(settings: Settings) -> dict[dt.date, Path]:
    """Snapshots with the screen columns, by date; older ones predate the screens."""
    out = {}
    for path in sorted((settings.data_dir / "history").glob("screen_*.parquet")):
        if set(NEEDED) <= set(pq.read_schema(path).names):
            out[dt.date.fromisoformat(path.stem.removeprefix("screen_"))] = path
    return out


def new_cohorts(dates, recorded, gap_days: int) -> list[dt.date]:
    """Snapshot dates to start cohorts on, at least ``gap_days`` after the one before."""
    last, out = max(recorded, default=None), []
    for d in sorted(dates):
        if last is None or (d - last).days >= gap_days:
            out.append(d)
            last = d
    return out


def members(df: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """(screen, ticker) for one snapshot, the baseline included."""
    parts = {name: df.loc[df[col].fillna(False).astype(bool), "ticker"] for name, col in screens.SCREENS.items()}
    eligible = df[(df["pe"] > 0) & ~df["stale"].fillna(True).astype(bool)
                  & (df["market_cap"] >= settings.screen_min_market_cap)]
    parts[BASELINE] = eligible.nsmallest(round(len(eligible) * settings.track_baseline_pct), "pe")["ticker"]
    return pd.concat([pd.DataFrame({"screen": k, "ticker": v.to_numpy()}) for k, v in parts.items()], ignore_index=True)


def cached_closes(settings: Settings, tickers: list[str], start: dt.date, source: CloseSource,
                  today: dt.date) -> pd.DataFrame:
    """Closes for ``tickers``, fetching only those not already cached today."""
    path = settings.cache_dir / "prices" / f"track_{today.isoformat()}.parquet"
    have = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    missing = sorted(set(tickers) - set(have.columns))
    if missing:
        fresh = source.closes(missing, start)
        have = pd.concat([have, fresh], axis=1).sort_index()
        path.parent.mkdir(parents=True, exist_ok=True)
        have.to_parquet(path)
    have.index = pd.to_datetime(have.index)
    return have[have.index.date < today] if len(have) else have


def _entry_day(market: pd.DatetimeIndex, cohort) -> pd.Timestamp | None:
    later = market[market > pd.Timestamp(cohort)]
    return later[0] if len(later) else None


def update_returns(pos: pd.DataFrame, closes: pd.DataFrame) -> pd.DataFrame:
    """Refresh each position from ``closes``; one with no fresh price keeps what it had."""
    pos = pos.copy()
    market = closes["SPY"].dropna().index if "SPY" in closes else pd.DatetimeIndex([])
    latest = market[-1] if len(market) else None
    for i, row in pos.iterrows():
        entry = _entry_day(market, row["cohort"])
        if entry is None:
            continue                    # bought at the next close, which hasn't happened yet
        pos.at[i, "entry_date"] = entry
        s = closes[row["ticker"]].dropna() if row["ticker"] in closes else closes["SPY"].iloc[:0]
        s = s[(s.index >= entry) & (s > 0)]
        if s.empty:
            pos.at[i, "gone"] = True
            continue
        pos.at[i, "ret"] = float(s.iloc[-1] / s.iloc[0] - 1)
        pos.at[i, "last_date"] = s.index[-1]
        pos.at[i, "gone"] = bool((latest - s.index[-1]).days > trend.MAX_AGE_DAYS)
    return pos


def summarize(pos: pd.DataFrame, closes: pd.DataFrame) -> pd.DataFrame:
    """One row per cohort and screen, with each benchmark over the same dates."""
    market = closes["SPY"].dropna().index if "SPY" in closes else pd.DatetimeIndex([])
    rows = []
    for (cohort, screen), g in pos.groupby(["cohort", "screen"], sort=True):
        entry = _entry_day(market, cohort)
        row = {"cohort": cohort, "screen": screen, "stocks": len(g), "gone": int(g["gone"].sum()),
               "days": (market[-1] - entry).days if entry is not None else np.nan,
               "ret": g["ret"].mean(),
               "ret_if_gone_lost": g["ret"].where(~g["gone"], -1.0).where(g["gone"] | g["ret"].notna()).mean()}
        for b in BENCHMARKS:
            s = closes[b].dropna() if b in closes and entry is not None else None
            s = s[s.index >= entry] if s is not None else ()
            row[b] = float(s.iloc[-1] / s.iloc[0] - 1) if len(s) else np.nan
        row["excess_vs_spy"] = row["ret"] - row["SPY"]
        rows.append(row)
    return pd.DataFrame(rows)


def refresh(settings: Settings, source: CloseSource | None = None, today: dt.date | None = None) -> pd.DataFrame:
    today = today or dt.date.today()
    source = source or YahooCloses(settings)
    pos = load_positions(settings)
    snaps = snapshots(settings)
    recorded = {d for d in pos["cohort"]}
    added = []
    for d in new_cohorts(snaps, recorded, settings.track_cohort_days):
        m = members(pd.read_parquet(snaps[d], columns=list(NEEDED)), settings)
        added.append(m.assign(cohort=d, entry_date=pd.NaT, ret=np.nan, last_date=pd.NaT, gone=False))
    if added:
        pos = pd.concat([pos, *added], ignore_index=True) if len(pos) else pd.concat(added, ignore_index=True)
    pos = pos.reindex(columns=COLUMNS)
    pos["gone"] = pos["gone"].astype(bool)
    pos["ret"] = pos["ret"].astype(float)
    for c in ("entry_date", "last_date"):
        pos[c] = pd.to_datetime(pos[c])
    if pos.empty:
        return pd.DataFrame()

    tickers = sorted({*pos["ticker"], *BENCHMARKS})
    try:
        closes = cached_closes(settings, tickers, min(pos["cohort"]), source, today)
    except Exception as exc:  # noqa: BLE001 - the record stays as it was, and the run carries on
        errors.log(settings.data_dir, "track", "*", exc)
        closes = pd.DataFrame()
    if "SPY" in closes:
        pos = update_returns(pos, closes)
        for t in sorted(set(pos.loc[pos["gone"], "ticker"])):
            errors.log(settings.data_dir, "track", t, "no recent price from Yahoo")
    positions_path(settings).parent.mkdir(parents=True, exist_ok=True)
    pos.to_parquet(positions_path(settings), index=False)
    summary = summarize(pos, closes)
    summary.to_csv(settings.data_dir / "track" / "summary.csv", index=False)
    return summary

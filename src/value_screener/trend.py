"""Phase 9d — price trend, for the "is it turning?" check. Context only: no screen requires it.

- ``off_52w_high``: last close / highest close of the past year - 1 (0 = at the high)
- ``momentum_12_1``: total return from 12 months ago to 1 month ago, skipping the latest
  month as is usual for momentum, since very recent moves tend to reverse
- ``price_vs_200d``: last close / 200-day average close - 1

Yahoo's ``Close`` is split-adjusted, which suits price levels; momentum uses ``Adj Close``
so dividends count. A separate download from the last close in :mod:`prices`, so a failure
here can never touch market caps or labels. Results are cached per day, like closes.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Protocol

import numpy as np
import pandas as pd

from . import errors
from .config import Settings

METRICS = ["off_52w_high", "momentum_12_1", "price_vs_200d"]
COLUMNS = ["ticker", *METRICS, "trend_date"]
LOOKBACK_DAYS = 400         # 12-1 momentum needs a price from just over a year ago
START_TOL = pd.Timedelta(days=7)    # listed about 12 months ago still counts as 12 months
MAX_AGE_DAYS = 10           # no close this recent: halted or delisted, so no trend
SMA_DAYS = 200


class TrendSource(Protocol):
    def trend(self, tickers: list[str]) -> pd.DataFrame:
        """Frame with ``COLUMNS`` (tickers without data omitted)."""


def series_metrics(close: pd.Series, adj: pd.Series | None = None) -> dict:
    """The trend figures for one ticker's daily closes (NaN where history is too short)."""
    close = close.dropna()
    # Yahoo's dividend adjustment can push very old prices of tiny stocks below zero.
    adj = close if adj is None else adj[adj > 0]
    out = dict.fromkeys(METRICS, np.nan)
    if close.empty or adj.empty:
        return out
    last = close.index[-1]
    out["off_52w_high"] = float(close.iloc[-1] / close[close.index > last - pd.Timedelta(days=365)].max() - 1)
    if len(close) >= SMA_DAYS:
        out["price_vs_200d"] = float(close.iloc[-1] / close.tail(SMA_DAYS).mean() - 1)
    start, end = last - pd.DateOffset(months=12), last - pd.DateOffset(months=1)
    if adj.index[0] <= start + START_TOL:
        out["momentum_12_1"] = float(adj[adj.index <= end].iloc[-1] / adj[adj.index >= start].iloc[0] - 1)
    return out


def from_download(data: pd.DataFrame, batch: list[str], today: dt.date) -> pd.DataFrame:
    """One row per ticker with a recent close, from a yfinance download frame."""
    if data is None or data.empty or "Close" not in data:
        return pd.DataFrame(columns=COLUMNS)
    frame = lambda s: s.to_frame(batch[0]) if isinstance(s, pd.Series) else s
    close = frame(data["Close"])
    adj = frame(data["Adj Close"]) if "Adj Close" in data else close
    cutoff = pd.Timestamp(today) - pd.Timedelta(days=MAX_AGE_DAYS)
    rows = []
    for t in close.columns:
        s = close[t].dropna()
        if not s.empty and s.index[-1] >= cutoff and (s > 0).all():
            rows.append({"ticker": t, **series_metrics(s, adj[t] if t in adj else None),
                         "trend_date": s.index[-1].date()})
    return pd.DataFrame(rows, columns=COLUMNS)


class YahooTrend:
    def __init__(self, chunk: int = 200, pause: float = 1.0):
        self.chunk = chunk
        self.pause = pause

    def trend(self, tickers: list[str]) -> pd.DataFrame:
        import yfinance as yf

        today = dt.date.today()
        start = today - dt.timedelta(days=LOOKBACK_DAYS)
        out = []
        for i in range(0, len(tickers), self.chunk):
            batch = tickers[i:i + self.chunk]
            data = yf.download(batch, start=start.isoformat(), interval="1d", auto_adjust=False,
                               progress=False, threads=True, group_by="column")
            out.append(from_download(data, batch, today))
            if self.pause and i + self.chunk < len(tickers):
                time.sleep(self.pause)
        return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=COLUMNS)


def _numeric(df: pd.DataFrame) -> pd.DataFrame:
    return df.assign(**{m: pd.to_numeric(df[m], errors="coerce") for m in METRICS})


def cached_trend(settings: Settings, tickers: list[str], source: TrendSource,
                 today: dt.date | None = None) -> pd.DataFrame:
    """Trend for ``tickers``, fetching only those not already cached today."""
    today = today or dt.date.today()
    path = settings.cache_dir / "prices" / f"trend_{today.isoformat()}.parquet"
    have = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=COLUMNS)
    missing = sorted(set(tickers) - set(have["ticker"]))
    if missing:
        have = pd.concat([_numeric(have), _numeric(source.trend(missing))], ignore_index=True) \
            .drop_duplicates("ticker", keep="last")
        path.parent.mkdir(parents=True, exist_ok=True)
        have.to_parquet(path, index=False)
    return _numeric(have[have["ticker"].isin(tickers)].reset_index(drop=True))


def refresh(settings: Settings, universe_df: pd.DataFrame, source: TrendSource | None = None,
            *, verbose: bool = True) -> pd.DataFrame:
    source = source or YahooTrend(settings.price_chunk, settings.price_pause)
    try:
        df = cached_trend(settings, universe_df["ticker"].tolist(), source)
    except Exception as exc:  # noqa: BLE001 - context only, so the run carries on without it
        errors.log(settings.data_dir, "trend", "*", exc)
        df = _numeric(pd.DataFrame(columns=COLUMNS))
    df.to_parquet(path(settings), index=False)
    if verbose:
        print(f"Trend: {len(df)}/{len(universe_df)} with recent prices; "
              f"{int(df['momentum_12_1'].notna().sum())} with 12-1 momentum")
    return df


def path(settings: Settings):
    return settings.data_dir / "trend.parquet"


def load(settings: Settings) -> pd.DataFrame:
    return pd.read_parquet(path(settings))

"""Phase 10b — daily closes, volume and splits per ticker, cached as one Parquet each.

Yahoo's ``Close`` is split-adjusted to the day of the download, so a past price needs later
splits undone (:func:`unadjusted`), and ``adj_close`` also counts dividends. A ticker's file
is never refetched, so one download serves the whole backtest; delete the folder to redo it.
Tickers Yahoo has nothing for are retried on the next run, since it reports throttled
tickers as "possibly delisted".
"""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path
from typing import Protocol

import pandas as pd

from .. import errors, trend
from ..config import Settings

COLUMNS = {"Close": "close", "Adj Close": "adj_close", "Volume": "volume", "Stock Splits": "split"}


class HistorySource(Protocol):
    def batch(self, tickers: list[str], start: dt.date) -> dict[str, pd.DataFrame]:
        """One frame per ticker with data, with the ``COLUMNS`` values on a date index."""


def from_download(data: pd.DataFrame | None, batch: list[str]) -> dict[str, pd.DataFrame]:
    """Per-ticker frames from a yfinance download with ``actions=True``."""
    if data is None or data.empty or "Close" not in data:
        return {}
    frame = lambda s: s.to_frame(batch[0]) if isinstance(s, pd.Series) else s
    wide = {new: frame(data[old]) for old, new in COLUMNS.items() if old in data}
    out = {}
    for t in wide["close"].columns:
        df = pd.DataFrame({c: f[t] for c, f in wide.items() if t in f}).reindex(columns=list(COLUMNS.values()))
        df = df.dropna(subset=["close"])
        if not df.empty:
            df["split"] = df["split"].fillna(0.0)
            out[t] = df
    return out


class YahooHistory:
    def __init__(self, timeout: float = 120.0):
        self.timeout = timeout

    def batch(self, tickers: list[str], start: dt.date) -> dict[str, pd.DataFrame]:
        return from_download(trend.download_batch(tickers, start, self.timeout, actions=True), tickers)


def _cache_path(settings: Settings, ticker: str) -> Path:
    return settings.cache_dir / "price_history" / f"{ticker}.parquet"


def fetch(settings: Settings, tickers: list[str], start: dt.date, source: HistorySource | None = None) -> int:
    """Download what isn't cached yet, writing each batch as it arrives; returns tickers fetched."""
    source = source or YahooHistory(settings.trend_batch_timeout)
    todo = [t for t in tickers if not _cache_path(settings, t).exists()]
    fetched = 0
    for attempt in range(settings.trend_retries + 1):
        if attempt and todo:
            time.sleep(settings.trend_retry_pause)
        for i in range(0, len(todo), settings.price_chunk):
            for t, df in source.batch(todo[i:i + settings.price_chunk], start).items():
                path = _cache_path(settings, t)
                path.parent.mkdir(parents=True, exist_ok=True)
                df.to_parquet(path)
                fetched += 1
            if settings.price_pause and i + settings.price_chunk < len(todo):
                time.sleep(settings.price_pause)
        todo = [t for t in todo if not _cache_path(settings, t).exists()]
        if not todo:
            break
    for t in todo:
        errors.log(settings.data_dir, "price-history", t, "no price history from Yahoo")
    return fetched


def load(settings: Settings, tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Cached frames for the tickers that have one."""
    out = {}
    for t in tickers:
        path = _cache_path(settings, t)
        if path.exists():
            df = pd.read_parquet(path)
            df.index = pd.to_datetime(df.index)
            out[t] = df
    return out


def split_factor(splits: pd.Series, after, through=None, *, shares_only: bool = False) -> float:
    """Product of the split ratios after ``after`` and up to ``through``; 1.0 when there are none.
    With ``shares_only``, leaves out the ones :func:`trend.changes_share_count` rejects."""
    hit = splits[splits > 0]
    hit = hit[hit.index > pd.Timestamp(after)]
    if through is not None:
        hit = hit[hit.index <= pd.Timestamp(through)]
    if shares_only:
        hit = hit[[trend.changes_share_count(x) for x in hit]]
    return float(hit.prod()) if len(hit) else 1.0


def unadjusted(df: pd.DataFrame, day) -> float:
    """The close as printed on ``day``: Yahoo's split-adjusted close, with later splits undone."""
    return float(df.loc[pd.Timestamp(day), "close"]) * split_factor(df["split"], day)

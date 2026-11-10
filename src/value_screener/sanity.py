"""Phase 8 — spot-check the screen's P/E for well-known tickers against Yahoo's trailing P/E.

A cheap guard against a broken tag map, a share-count slip or a units error: big, simple
companies should come out within ``sanity_pe_tol`` of Yahoo. A miss says which side
differs. When net income is the gap, it's usually timing: Yahoo takes earnings from the
press release, while SEC's XBRL arrives with the 10-Q days later and the facts cache is
up to ``cache_ttl_days`` old (XOM, Nov 2026: its Q3 10-Q came hours after the cache).
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Protocol

import numpy as np
import pandas as pd

from . import cache, errors
from .config import Settings

FIELDS = ("trailingPE", "netIncomeToCommon", "marketCap")


class InfoSource(Protocol):
    def info(self, ticker: str) -> dict:
        """Yahoo's ``FIELDS`` for one ticker."""


class YahooInfo:
    def info(self, ticker: str) -> dict:
        import yfinance as yf
        got = yf.Ticker(ticker).get_info() or {}
        return {k: got.get(k) for k in FIELDS}


def yahoo_figures(settings: Settings, tickers: list[str], source: InfoSource | None = None,
                  today: dt.date | None = None, pause: float = 0.3) -> dict[str, dict]:
    """Yahoo's figures per ticker, cached for the day."""
    source = source or YahooInfo()
    today = today or dt.date.today()
    out = {}
    for t in tickers:
        path = settings.cache_dir / "yahoo" / f"sanity_{today.isoformat()}_{t}.json.gz"
        hit = cache.read_json(path, 1)
        if hit is None:
            try:
                hit = source.info(t)
            except Exception as exc:  # noqa: BLE001 - one bad ticker shouldn't stop the check
                errors.log(settings.data_dir, "sanity", t, exc)
                continue
            cache.write_json(path, hit)
            if pause:
                time.sleep(pause)
        out[t] = hit
    return out


def _off(ours, theirs) -> float:
    return float(ours) / float(theirs) - 1 if ours and theirs and pd.notna(ours) and float(theirs) > 0 else np.nan


def compare(screen: pd.DataFrame, yahoo: dict[str, dict], tol: float) -> pd.DataFrame:
    """One row per ticker: both P/Es, the gap, whether it's within ``tol``, and why not."""
    rows = []
    by = screen.drop_duplicates("ticker").set_index("ticker")
    for t, y in yahoo.items():
        r = by.loc[t] if t in by.index else pd.Series(dtype=object)
        pe, ni, cap = (r.get(c, np.nan) for c in ("pe", "net_income", "market_cap"))
        off = _off(pe, y.get("trailingPE"))
        ni_off, cap_off = _off(ni, y.get("netIncomeToCommon")), _off(cap, y.get("marketCap"))
        why = []
        if pd.notna(ni_off) and abs(ni_off) > tol:
            as_of = pd.to_datetime(r.get("as_of"), errors="coerce")
            why.append(f"net income {ni_off:+.0%} (ours as of {as_of.date() if pd.notna(as_of) else '?'})")
        if pd.notna(cap_off) and abs(cap_off) > tol:
            why.append(f"market cap {cap_off:+.0%}")
        rows.append({"ticker": t, "pe": pe, "yahoo_pe": y.get("trailingPE"), "diff": off,
                     "ok": bool(pd.notna(off) and abs(off) <= tol),
                     "why": "; ".join(why) if pd.isna(off) or abs(off) > tol else ""})
    return pd.DataFrame(rows)


def run(settings: Settings, screen: pd.DataFrame, source: InfoSource | None = None,
        today: dt.date | None = None) -> pd.DataFrame:
    tickers = list(settings.sanity_tickers)
    return compare(screen, yahoo_figures(settings, tickers, source, today), settings.sanity_pe_tol)

"""Phase 4 — last close per ticker behind a swappable ``PriceSource`` interface.

The default source batch-downloads closes from Yahoo (``yfinance.download``, chunks
of ``price_chunk`` with a pause between). yfinance is unofficial and may break, so a
paid API (FMP, Polygon, ...) can drop in by implementing ``last_close``.

Market cap = price x SEC shares outstanding (the latest cover-page count), checked
against Nasdaq's public screener (every US listing's market cap in one request).
Where the SEC figure is missing or disagrees (multi-class shares, ADR ratios, stale
counts) the Nasdaq figure is used, then Yahoo per ticker. Closes are cached per day.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Protocol

import pandas as pd

from . import cache, errors
from .config import Settings


class PriceSource(Protocol):
    def last_close(self, tickers: list[str]) -> pd.DataFrame:
        """Frame with columns ticker, price, price_date (missing tickers omitted)."""


class YahooPrices:
    def __init__(self, chunk: int = 200, pause: float = 1.0):
        self.chunk = chunk
        self.pause = pause

    def last_close(self, tickers: list[str]) -> pd.DataFrame:
        import yfinance as yf

        out = []
        for i in range(0, len(tickers), self.chunk):
            batch = tickers[i:i + self.chunk]
            data = yf.download(batch, period="5d", interval="1d", auto_adjust=False,
                               progress=False, threads=True, group_by="column")
            out.append(_last_valid(data, batch))
            if self.pause and i + self.chunk < len(tickers):
                time.sleep(self.pause)
        return pd.concat(out, ignore_index=True) if out else _empty()


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=["ticker", "price", "price_date"])


def _last_valid(data: pd.DataFrame, batch: list[str]) -> pd.DataFrame:
    """Latest non-NaN close per ticker from a yfinance download frame."""
    if data is None or data.empty or "Close" not in data:
        return _empty()
    close = data["Close"]
    if isinstance(close, pd.Series):              # single ticker -> Series
        close = close.to_frame(batch[0])
    rows = []
    for t in close.columns:
        s = close[t].dropna()
        if not s.empty and s.iloc[-1] > 0:
            rows.append({"ticker": t, "price": float(s.iloc[-1]),
                         "price_date": pd.Timestamp(s.index[-1]).date()})
    return pd.DataFrame(rows, columns=["ticker", "price", "price_date"])


def cached_last_close(settings: Settings, tickers: list[str], source: PriceSource,
                      today: dt.date | None = None) -> pd.DataFrame:
    """Closes for ``tickers``, fetching only those not already cached today."""
    today = today or dt.date.today()
    path = settings.cache_dir / "prices" / f"close_{today.isoformat()}.parquet"
    have = pd.read_parquet(path) if path.exists() else _empty()
    missing = sorted(set(tickers) - set(have["ticker"]))
    if missing:
        fresh = source.last_close(missing)
        have = pd.concat([have, fresh], ignore_index=True).drop_duplicates("ticker", keep="last")
        path.parent.mkdir(parents=True, exist_ok=True)
        have.to_parquet(path, index=False)
    return have[have["ticker"].isin(tickers)].reset_index(drop=True)


NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true"
# A SEC-share market cap this far from the reference cap is treated as wrong
# (multi-class shares, ADR ratios, stale share counts).
MAX_CAP_RATIO = 1.5


def with_market_cap(closes: pd.DataFrame, fundamentals_df: pd.DataFrame,
                    reference: dict[str, float] | None = None,
                    today: dt.date | None = None, stale_months: int = 15) -> pd.DataFrame:
    """price x SEC shares, plus ``needs_check`` where that number can't be trusted.

    Multi-class companies (e.g. Berkshire's A/B shares) and filers that stopped
    tagging share counts give a wrong or missing market cap, so flag: no shares,
    a share count older than ``stale_months``, a market cap implausibly small
    against revenue (< 1%) or book equity (< 2%), or one more than
    ``MAX_CAP_RATIO``x away from the ``reference`` (Nasdaq) market cap.
    """
    f = fundamentals_df.set_index("ticker")
    col = lambda c: df["ticker"].map(f[c]) if c in f else pd.Series(index=df.index, dtype=float)
    df = closes.copy()
    df["shares_outstanding"] = col("shares_outstanding")
    df["market_cap"] = df["price"] * df["shares_outstanding"]
    df["market_cap_src"] = "sec_shares"
    shares_end = pd.to_datetime(col("shares_outstanding_end"), errors="coerce")
    cutoff = pd.Timestamp(today or dt.date.today()) - pd.DateOffset(months=stale_months)
    rev, eq = pd.to_numeric(col("revenue"), errors="coerce"), pd.to_numeric(col("equity"), errors="coerce")
    ratio = df["market_cap"] / df["ticker"].map(reference or {})
    off = (ratio > MAX_CAP_RATIO) | (ratio < 1 / MAX_CAP_RATIO)
    df["needs_check"] = (df["market_cap"].isna() | (shares_end < cutoff) | off
                         | (df["market_cap"] < 0.01 * rev) | (df["market_cap"] < 0.02 * eq)).fillna(False)
    return df


def parse_nasdaq_screener(payload: dict) -> dict[str, float]:
    """Nasdaq screener JSON -> {TICKER: market cap}; class shares 'BRK/B' -> 'BRK-B'."""
    out: dict[str, float] = {}
    for row in ((payload.get("data") or {}).get("rows") or []):
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym or "^" in sym:                    # preferreds
            continue
        try:
            cap = float(row.get("marketCap") or 0)
        except ValueError:
            continue
        if cap > 0:
            out[sym.replace("/", "-").replace(".", "-")] = cap
    return out


def nasdaq_market_caps(settings: Settings) -> dict[str, float]:
    """All US-listed market caps in one request to Nasdaq's public screener (cached a day)."""
    import requests

    path = settings.cache_dir / "prices" / f"nasdaq_screener_{dt.date.today().isoformat()}.json.gz"
    payload = cache.read_json(path, 1)
    if payload is None:
        try:
            resp = requests.get(NASDAQ_SCREENER_URL, timeout=60, headers={
                "User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - fall back to SEC shares / Yahoo
            errors.log(settings.data_dir, "prices-nasdaq", "*", exc)
            return {}
        cache.write_json(path, payload)
    return parse_nasdaq_screener(payload)


def yahoo_market_caps(settings: Settings, tickers: list[str], *, pause: float = 0.3,
                      max_failures: int = 10) -> dict[str, float]:
    """Yahoo market cap per ticker (one cached info call each) - last-resort fallback.

    Stops after ``max_failures`` consecutive errors: Yahoo answers bursts with 401
    "Invalid Crumb", and hammering it only extends the block.
    """
    import yfinance as yf

    out: dict[str, float] = {}
    failures = 0
    for t in tickers:
        path = settings.cache_dir / "yahoo" / f"mcap_{t}.json.gz"
        hit = cache.read_json(path, settings.cache_ttl_days)
        if hit is None:
            if failures >= max_failures:
                errors.log(settings.data_dir, "prices-mcap", t, "skipped: Yahoo is refusing requests")
                continue
            try:
                info = yf.Ticker(t).get_info() or {}
                hit = {"market_cap": info.get("marketCap")}
                failures = 0
            except Exception as exc:  # noqa: BLE001
                failures += 1
                errors.log(settings.data_dir, "prices-mcap", t, exc)
                continue
            cache.write_json(path, hit)
            time.sleep(pause)
        if hit.get("market_cap"):
            out[t] = float(hit["market_cap"])
    return out


def refresh(settings: Settings, universe_df: pd.DataFrame, fundamentals_df: pd.DataFrame,
            source: PriceSource | None = None, *, reference_caps=None, market_caps=None,
            verbose: bool = True) -> pd.DataFrame:
    reference_caps = reference_caps or nasdaq_market_caps
    market_caps = market_caps or yahoo_market_caps
    source = source or YahooPrices(settings.price_chunk, settings.price_pause)
    tickers = universe_df["ticker"].tolist()
    try:
        closes = cached_last_close(settings, tickers, source)
    except Exception as exc:  # noqa: BLE001 - the whole price layer failed
        errors.log(settings.data_dir, "prices", "*", exc)
        closes = _empty()
    for t in sorted(set(tickers) - set(closes["ticker"])):
        errors.log(settings.data_dir, "prices", t, "no recent close")
    ref = reference_caps(settings)
    df = with_market_cap(closes, fundamentals_df, ref, stale_months=settings.stale_months)

    # Flagged caps: Nasdaq's figure first, then Yahoo; otherwise leave it missing.
    check = df["needs_check"]
    from_ref = check & df["ticker"].isin(ref)
    df.loc[from_ref, "market_cap"] = df.loc[from_ref, "ticker"].map(ref)
    df.loc[from_ref, "market_cap_src"] = "nasdaq"
    rest = df.loc[check & ~from_ref, "ticker"].tolist()
    yahoo = market_caps(settings, rest) if rest else {}
    from_yahoo = df["ticker"].isin(yahoo) & check & ~from_ref
    df.loc[from_yahoo, "market_cap"] = df.loc[from_yahoo, "ticker"].map(yahoo)
    df.loc[from_yahoo, "market_cap_src"] = "yahoo"
    untrusted = check & ~from_ref & ~from_yahoo
    df.loc[untrusted, "market_cap"] = float("nan")
    df.loc[untrusted, "market_cap_src"] = "untrusted"

    df.to_parquet(settings.data_dir / "prices.parquet", index=False)
    if verbose:
        print(f"Prices: {len(df)}/{len(tickers)} closes; "
              f"{int(df['market_cap'].notna().sum())} with market cap; "
              f"{int(check.sum())} SEC-share caps flagged -> {int(from_ref.sum())} from Nasdaq, "
              f"{int(from_yahoo.sum())} from Yahoo, {int(untrusted.sum())} left missing")
    return df


def load(settings: Settings) -> pd.DataFrame:
    return pd.read_parquet(settings.data_dir / "prices.parquet")

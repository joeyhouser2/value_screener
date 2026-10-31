"""Phase 1 — the universe: every NYSE + NASDAQ common stock, mapped to its SEC CIK.

Sources (both cached for ``cache_ttl_days``):
- NASDAQ Trader symbol directory: ``nasdaqlisted.txt`` (NASDAQ) and ``otherlisted.txt``
  (NYSE, NYSE American, Arca, ...).
- SEC ``company_tickers_exchange.json`` for ticker -> CIK.

Tickers use the SEC / Yahoo class-share form (``BRK-B``, not ``BRK.B``).
"""

from __future__ import annotations

import io
import re

import pandas as pd
import requests

from . import cache
from .config import Settings
from .sec import SecClient

NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# otherlisted.txt exchange codes.
OTHER_EXCHANGES = {"N": "NYSE", "A": "NYSE American", "P": "NYSE Arca", "Z": "Cboe BZX",
                   "V": "IEX", "M": "NYSE Chicago", "TXSE": "Texas"}

# Security names that aren't common equity. LP "Common Units" are equity and kept;
# ADRs ("American Depositary Shares") are kept; fractional preferred depositary
# shares are caught by "preferred"/"preference".
_NOT_COMMON = re.compile(
    r"warrants?\b|\brights?\b|preferred|preference|\bnotes?\b|debentures?|\d%|"
    r"\bETNs?\b|exchange[- ]traded notes?|\bfund\b|municipal|"
    r"\b(?:income|bond|dividend|credit|opportunities)\s+trust\b",
    re.IGNORECASE,
)
_UNITS = re.compile(r"\bunits?\b", re.IGNORECASE)
_COMMON_UNITS = re.compile(r"common units|limited partnership units", re.IGNORECASE)
_BAD_SYMBOL = re.compile(r"[$=+^#]")
# Trailing share-class boilerplate dropped from display names.
_NAME_SUFFIX = re.compile(
    r"\s*-?\s*(?:class [a-z] )?(?:new )?(?:common stock|ordinary shares|common shares|"
    r"common units|capital stock)\b.*$|\s*-?\s*american depositary (?:shares|receipts).*$",
    re.IGNORECASE)


def normalize_ticker(symbol: str) -> str:
    return str(symbol).strip().upper().replace(".", "-")


def clean_name(name: str) -> str:
    name = str(name).strip()
    cleaned = _NAME_SUFFIX.sub("", name).rstrip(" -,")
    return cleaned or name


def is_common(symbol: str, name: str) -> bool:
    if _BAD_SYMBOL.search(str(symbol)):
        return False
    name = str(name)
    if _NOT_COMMON.search(name):
        return False
    if _UNITS.search(name) and not _COMMON_UNITS.search(name):
        return False
    return True


def _read_pipe(text: str) -> pd.DataFrame:
    body = "\n".join(line for line in text.splitlines()
                     if line.strip() and not line.startswith("File Creation Time"))
    return pd.read_csv(io.StringIO(body), sep="|", dtype=str, keep_default_na=False)


def parse_nasdaq_listed(text: str) -> pd.DataFrame:
    df = _read_pipe(text)
    df = df[(df["Test Issue"] != "Y") & (df["ETF"] != "Y")]
    return pd.DataFrame({"symbol": df["Symbol"], "security_name": df["Security Name"],
                         "exchange": "NASDAQ"})


def parse_other_listed(text: str) -> pd.DataFrame:
    df = _read_pipe(text)
    df = df[(df["Test Issue"] != "Y") & (df["ETF"] != "Y")]
    return pd.DataFrame({"symbol": df["ACT Symbol"], "security_name": df["Security Name"],
                         "exchange": df["Exchange"].map(OTHER_EXCHANGES).fillna(df["Exchange"])})


def parse_cik_map(payload: dict) -> dict[str, int]:
    """SEC company_tickers_exchange.json -> {TICKER: cik}."""
    cols = payload["fields"]
    i_cik, i_tic = cols.index("cik"), cols.index("ticker")
    out: dict[str, int] = {}
    for row in payload["data"]:
        tic, cik = row[i_tic], row[i_cik]
        if tic and cik is not None:
            out.setdefault(normalize_ticker(tic), int(cik))
    return out


def build_universe(nasdaq_text: str, other_text: str, cik_map: dict[str, int],
                   exchanges=("NASDAQ", "NYSE")) -> pd.DataFrame:
    """Pure core: filter to common stocks on ``exchanges`` and attach CIKs."""
    raw = pd.concat([parse_nasdaq_listed(nasdaq_text), parse_other_listed(other_text)],
                    ignore_index=True)
    raw = raw[raw["exchange"].isin(exchanges)]
    keep = [is_common(s, n) for s, n in zip(raw["symbol"], raw["security_name"])]
    df = raw[keep].copy()
    df["ticker"] = df["symbol"].map(normalize_ticker)
    df["name"] = df["security_name"].map(clean_name)
    df["cik"] = pd.array([cik_map.get(t) for t in df["ticker"]], dtype="Int64")
    df["has_cik"] = df["cik"].notna()
    df = df.drop_duplicates("ticker").sort_values("ticker").reset_index(drop=True)
    return df[["ticker", "name", "exchange", "cik", "has_cik", "security_name"]]


def _download_text(url: str, settings: Settings, name: str) -> str:
    path = settings.cache_dir / "symbols" / name
    hit = cache.read_text(path, settings.cache_ttl_days)
    if hit is not None:
        return hit
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    cache.write_text(path, resp.text)
    return resp.text


def refresh(settings: Settings, client: SecClient, *, verbose: bool = True) -> pd.DataFrame:
    """Download (or reuse cached) sources, build the universe, write universe.parquet."""
    nasdaq = _download_text(NASDAQ_URL, settings, "nasdaqlisted.txt")
    other = _download_text(OTHER_URL, settings, "otherlisted.txt")
    cik_map = parse_cik_map(client.tickers_exchange())
    df = build_universe(nasdaq, other, cik_map, settings.exchanges)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(settings.data_dir / "universe.parquet", index=False)
    if verbose:
        n_no = int((~df["has_cik"]).sum())
        by_ex = df["exchange"].value_counts().to_dict()
        print(f"Universe: {len(df)} common stocks {by_ex}; {n_no} without a CIK "
              f"(foreign/ADR filers, kept and flagged)")
    return df


def load(settings: Settings) -> pd.DataFrame:
    return pd.read_parquet(settings.data_dir / "universe.parquet")

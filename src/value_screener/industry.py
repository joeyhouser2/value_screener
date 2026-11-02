"""Phase 2 — industry labels: SEC SIC code rolled up to readable groups and sectors.

SIC comes from the SEC submissions API (one cached call per CIK). Tickers with no
SIC (no CIK, or a blank SIC) fall back to Yahoo's sector/industry. Banks, insurers,
REITs and utilities are flagged in ``special`` because they need different metrics.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import yaml

from . import cache, errors
from .config import Settings
from .sec import SecClient

ANNUAL_FORMS = ("10-K", "20-F", "40-F", "10-K/A", "20-F/A", "40-F/A")
UNCLASSIFIED = ("Unclassified", "Other", None)
# Placeholder SIC codes that say nothing about the business: 0 (none), 8880 (ADRs).
NON_INDUSTRY_SIC = {0, 8880}


def load_groups(config_dir: Path) -> dict:
    return yaml.safe_load((config_dir / "sic_groups.yaml").read_text(encoding="utf-8"))


def classify_sic(sic, groups_cfg: dict):
    """(group, sector, special) for a SIC code, or None if the code is missing."""
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return None
    if code in NON_INDUSTRY_SIC:
        return None
    for rule in groups_cfg["groups"]:
        if any(lo <= code <= hi for lo, hi in rule["ranges"]):
            return rule["group"], rule["sector"], rule.get("special")
    return UNCLASSIFIED


def classify_yahoo(industry: str | None, sector: str | None, groups_cfg: dict):
    """(group, sector, special) from Yahoo's labels, or None when Yahoo has nothing."""
    by_name = {r["group"]: (r["group"], r["sector"], r.get("special"))
               for r in groups_cfg["groups"]}
    text = (industry or "").lower()
    for keyword, group in groups_cfg.get("yahoo_industry_keywords", []):
        if keyword.lower() in text and group in by_name:
            return by_name[group]
    our_sector = groups_cfg.get("yahoo_sectors", {}).get(sector or "")
    if our_sector:
        return f"Other {our_sector}", our_sector, None
    return None


def summarize_submissions(payload: dict) -> dict:
    """The slice of a submissions payload we keep in the cache."""
    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    annual = next((f for f in forms if f in ANNUAL_FORMS), None)
    return {"sic": payload.get("sic") or None,
            "sic_description": payload.get("sicDescription") or None,
            "entity_type": payload.get("entityType"),
            "sec_name": payload.get("name"),
            "fiscal_year_end": payload.get("fiscalYearEnd"),
            "filer_category": payload.get("category"),
            "annual_form": annual}


def _yahoo_profile(ticker: str, settings: Settings) -> dict:
    path = settings.cache_dir / "yahoo" / f"profile_{ticker}.json.gz"
    hit = cache.read_json(path, settings.cache_ttl_days)
    if hit is not None:
        return hit
    import yfinance as yf
    info = yf.Ticker(ticker).get_info() or {}
    prof = {"sector": info.get("sector"), "industry": info.get("industry")}
    cache.write_json(path, prof)
    return prof


def label(universe_df: pd.DataFrame, subs: dict[int, dict], groups_cfg: dict,
          yahoo: dict[str, dict] | None = None) -> pd.DataFrame:
    """Pure core: attach SIC info, group, sector and special flag to each ticker."""
    yahoo = yahoo or {}
    rows = []
    for t, cik in zip(universe_df["ticker"], universe_df["cik"]):
        s = subs.get(int(cik), {}) if pd.notna(cik) else {}
        hit = classify_sic(s.get("sic"), groups_cfg)
        source = "sic"
        if hit is None and t in yahoo:
            hit = classify_yahoo(yahoo[t].get("industry"), yahoo[t].get("sector"), groups_cfg)
            source = "yahoo"
        if hit is None:
            hit, source = UNCLASSIFIED, None
        override = groups_cfg.get("ticker_overrides", {}).get(t)
        if override:
            hit = next((r["group"], r["sector"], r.get("special")) for r in groups_cfg["groups"]
                       if r["group"] == override)
            source = "override"
        group, sector, special = hit
        rows.append({"ticker": t, "sic": s.get("sic"), "sic_description": s.get("sic_description"),
                     "annual_form": s.get("annual_form"), "entity_type": s.get("entity_type"),
                     "industry_group": group, "sector": sector, "special": special,
                     "industry_source": source})
    return pd.DataFrame(rows)


def refresh(settings: Settings, client: SecClient, universe_df: pd.DataFrame,
            *, verbose: bool = True) -> pd.DataFrame:
    groups_cfg = load_groups(settings.config_dir)
    first_ticker = universe_df.dropna(subset=["cik"]).drop_duplicates("cik").set_index("cik")["ticker"]

    def fetch(cik):
        try:
            return int(cik), client.submissions(cik, summarize_submissions) or {}
        except Exception as exc:  # noqa: BLE001 - logged, run continues
            errors.log(settings.data_dir, "industry", first_ticker.get(cik, str(cik)), exc)
            return int(cik), {}

    with ThreadPoolExecutor(settings.sec_workers) as pool:
        subs = dict(pool.map(fetch, first_ticker.index))

    yahoo: dict[str, dict] = {}
    if settings.yf_industry_fallback:
        no_sic = [t for t, c in zip(universe_df["ticker"], universe_df["cik"])
                  if pd.isna(c) or classify_sic(subs.get(int(c), {}).get("sic"), groups_cfg) is None]
        for t in no_sic:
            try:
                yahoo[t] = _yahoo_profile(t, settings)
            except Exception as exc:  # noqa: BLE001
                errors.log(settings.data_dir, "industry-yahoo", t, exc)

    df = label(universe_df, subs, groups_cfg, yahoo)
    df.to_parquet(settings.data_dir / "industry.parquet", index=False)
    if verbose:
        print(f"Industry: {df['industry_group'].nunique()} groups; "
              f"{(df['industry_source'] == 'yahoo').sum()} via Yahoo fallback; "
              f"{(df['industry_group'] == 'Unclassified').sum()} unclassified; "
              f"special: {df['special'].value_counts().to_dict()}")
    return df


def load(settings: Settings) -> pd.DataFrame:
    return pd.read_parquet(settings.data_dir / "industry.parquet")

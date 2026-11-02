"""Phase 5 — valuation metrics, industry-relative percentiles, quality filter, labels.

Every valuation metric is turned into a percentile **within the stock's industry
group** (0 = cheapest peer, 1 = most expensive). If fewer than ``min_peers`` stocks
in the group have that metric, the sector is used instead (then the whole market).
Industries get their own metric sets:

- banks / specialty finance / insurers: P/B, P/E
- REITs: P/FFO (net income + D&A as an FFO proxy), P/B
- everyone else: P/E, EV/EBITDA, P/S, FCF yield

The composite is the mean of the available percentiles; it is then ranked again
within the industry to give ``composite_pct``. Labels:

- ``Undervalued``: composite_pct <= undervalued_pct AND passes the quality filter
- ``Overvalued``:  composite_pct >= overvalued_pct
- ``Fair``: otherwise (``cheap_fails_quality`` marks cheap names that failed quality)
- ``Insufficient data``: fewer than ``min_metrics`` metrics, no market cap, or stale

Negative earnings / book / EBITDA make a multiple meaningless, so it is treated as
missing — never as "cheap".
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import normalized, survival
from .config import Settings

# Metrics where a LOWER value is cheaper; the rest (yields) are cheaper when higher.
LOWER_IS_CHEAPER = {"pe", "ps", "pb", "ev_ebitda", "p_ffo", "peg", "price_to_graham"}
METRIC_SETS = {
    "bank": ["pb", "pe"],
    "insurer": ["pb", "pe"],
    "reit": ["p_ffo", "pb"],
    None: ["pe", "ev_ebitda", "ps", "fcf_yield"],
}
MAX_FLOW_LAG_DAYS = 120     # about one missed quarterly filing
ALL_SCORED = sorted({m for ms in METRIC_SETS.values() for m in ms})
LABELS = ("Undervalued", "Fair", "Overvalued", "Insufficient data")


def _pos(s: pd.Series) -> pd.Series:
    return s.where(s > 0)


def compute_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Add multiples, yields, Graham number, ROE and debt/equity."""
    df = df.copy()
    num = lambda c: pd.to_numeric(df[c], errors="coerce") if c in df else pd.Series(np.nan, index=df.index)

    def current(c):
        """A flow, unless its tag stopped being filed long before the company's latest
        period: LEN's operating income ended in 2021, so it can't pair with 2026 revenue."""
        if f"{c}_end" not in df or "as_of" not in df:
            return num(c)
        lag = pd.to_datetime(df["as_of"]) - pd.to_datetime(df[f"{c}_end"])
        return num(c).where(~(lag > pd.Timedelta(days=MAX_FLOW_LAG_DAYS)))

    mcap, ni, rev, eq = num("market_cap"), num("net_income"), num("revenue"), num("equity")
    debt, cash = num("total_debt").fillna(0.0), num("cash").fillna(0.0)
    # An old D&A only approximates; dropping it would cost REITs their P/FFO and label.
    da, oi = num("d_and_a"), current("operating_income")
    ocf, capex = num("operating_cash_flow"), num("capex").fillna(0.0)
    shares, price = num("shares_outstanding"), num("price")

    df["pe"] = mcap / _pos(ni)
    df["ps"] = mcap / _pos(rev)
    df["pb"] = mcap / _pos(eq)
    df["enterprise_value"] = mcap + debt - cash
    # Many filers (XOM, DHI) never tag operating income: pretax + interest stands in.
    df["ebit"] = oi.fillna(current("pretax_income") + current("interest_expense").fillna(0.0))
    df["ebitda"] = df["ebit"] + da
    df["ev_ebitda"] = _pos(df["enterprise_value"]) / _pos(df["ebitda"])
    df["fcf"] = ocf - capex
    df["fcf_yield"] = df["fcf"] / _pos(mcap)
    df["dividend_yield"] = num("dividends").fillna(0.0) / _pos(mcap)
    df["p_ffo"] = mcap / _pos(ni + da)
    growth = ni / _pos(num("net_income_prev")) - 1
    df["earnings_growth"] = growth.where(ni > 0)
    df["peg"] = df["pe"] / _pos(df["earnings_growth"] * 100)
    # Per-share values on the share count implied by market cap / price, so they stay
    # consistent when the cap came from Yahoo (multi-class companies).
    implied = (mcap / _pos(price)).fillna(shares)
    eps, bvps = ni / _pos(implied), eq / _pos(implied)
    df["graham_number"] = np.sqrt(22.5 * _pos(eps) * _pos(bvps))
    df["price_to_graham"] = price / df["graham_number"]
    df["roe"] = ni / _pos(eq)
    df["debt_to_equity"] = debt / _pos(eq)
    return df


def _peer_pct(df: pd.DataFrame, values: pd.Series, lower_is_cheaper: bool,
              min_peers: int) -> tuple[pd.Series, pd.Series]:
    """Percentile (0 = cheapest) within industry, else sector, else market; plus the level."""
    v = values.where(np.isfinite(values))
    cheap_first = v if lower_is_cheaper else -v
    pct = pd.Series(np.nan, index=df.index)
    level = pd.Series(None, index=df.index, dtype=object)
    for col, name in (("industry_group", "industry"), ("sector", "sector"), (None, "market")):
        todo = v.notna() & pct.isna()
        if not todo.any():
            break
        keys = df[col].fillna("?") if col else pd.Series("all", index=df.index)
        counts = v.notna().groupby(keys).transform("sum")
        ok = todo & ((counts >= min_peers) | (col is None))
        if not ok.any():
            continue
        ranked = cheap_first.where(v.notna()).groupby(keys).rank(pct=True, method="average")
        # rank(pct) is in (0, 1]; shift so the cheapest peer is ~0 and the dearest 1.
        n = counts.astype(float)
        scaled = ((ranked * n) - 1) / (n - 1).where(n > 1, 1)
        pct[ok], level[ok] = scaled[ok], name
    return pct, level


def score(df: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """Percentiles per metric, composite, quality filter and labels."""
    df = df.copy()
    special = df["special"].where(df["special"].isin(["bank", "insurer", "reit"]), None)
    levels = pd.DataFrame(index=df.index)
    for m in ALL_SCORED:
        uses = special.map(lambda s: m in METRIC_SETS.get(s, METRIC_SETS[None]))
        vals = df[m].where(uses)
        df[f"{m}_pct"], levels[m] = _peer_pct(df, vals, m in LOWER_IS_CHEAPER, settings.min_peers)

    pcts = df[[f"{m}_pct" for m in ALL_SCORED]]
    df["n_metrics"] = pcts.notna().sum(axis=1)
    df["composite"] = pcts.mean(axis=1)
    order = {"industry": 0, "sector": 1, "market": 2}
    df["peer_level"] = levels.apply(
        lambda r: max((x for x in r if isinstance(x, str)), key=order.get, default=None), axis=1)
    df["composite_pct"], _ = _peer_pct(df, df["composite"], True, settings.min_peers)

    # Quality (value-trap guard).
    de_pct, _ = _peer_pct(df, df["debt_to_equity"], True, settings.min_peers)
    financial = special.isin(["bank", "insurer", "reit"])
    checks = {
        "loss-making": ~(df["net_income"] > 0),
        "negative FCF": ~financial & ~(df["fcf"] > 0),
        "high debt": de_pct >= settings.quality_max_de_pct,
        "ROE <= 0": ~(df["roe"] > 0),
    }
    fails = pd.DataFrame(checks).fillna(False).astype(bool)
    df["quality_pass"] = ~fails.any(axis=1)
    df["quality_issues"] = fails.apply(lambda r: ", ".join(k for k, bad in r.items() if bad), axis=1)

    insufficient = (df["n_metrics"] < settings.min_metrics) | df["market_cap"].isna() \
        | df["stale"].fillna(True).astype(bool)
    cheap = df["composite_pct"] <= settings.undervalued_pct
    label = np.select(
        [insufficient, cheap & df["quality_pass"], df["composite_pct"] >= settings.overvalued_pct],
        ["Insufficient data", "Undervalued", "Overvalued"], default="Fair")
    df["label"] = label
    df["cheap_fails_quality"] = ~insufficient & cheap & ~df["quality_pass"]
    return df


SCREEN_COLUMNS = [
    "ticker", "name", "exchange", "industry_group", "sector", "special", "label",
    "composite_pct", "composite", "n_metrics", "peer_level", "quality_pass", "quality_issues",
    "cheap_fails_quality", "price", "price_date", "market_cap",
    "pe", "ps", "pb", "ev_ebitda", "fcf_yield", "p_ffo", "dividend_yield", "peg",
    "earnings_growth", "price_to_graham", "graham_number", "roe", "debt_to_equity",
    *[f"{m}_pct" for m in ALL_SCORED],
    "pe10", "ev_midcycle_ebit", "norm_fcf_yield", *[f"{m}_pct" for m in normalized.PEER_METRICS],
    "margin_position", "industry_cycle_position", "cyclical", "median_margin", "margin_vol",
    "loss_years", "years_history", "share_cagr", "norm_earnings", "midcycle_ebit", "norm_fcf",
    "f_score", "f_score_n", "f_score_fy", "altman_z", "altman_model", "altman_zone",
    "interest_coverage", "net_cash", "coverage_ok", "tangible_book", "p_tbv", "p_tbv_pct",
    "ncav", "p_ncav",
    "revenue", "net_income", "fcf", "ebit", "ebitda", "equity", "total_debt", "cash",
    "shares_outstanding", "as_of", "stale", "sic", "sic_description", "industry_source", "cik",
]


def build_screen(universe_df, industry_df, fundamentals_df, prices_df,
                 settings: Settings, history_df=None) -> pd.DataFrame:
    """Pure core: join the phase outputs, compute metrics, score and label."""
    df = (universe_df[["ticker", "name", "exchange", "cik"]]
          .merge(industry_df, on="ticker", how="left")
          .merge(fundamentals_df, on="ticker", how="left")
          .merge(prices_df[["ticker", "price", "price_date", "market_cap"]], on="ticker", how="left"))
    if "stale" not in df:
        df["stale"] = True
    df = normalized.add_normalized(score(compute_metrics(df), settings), history_df, settings)
    df = survival.add_survival(df, history_df, settings)
    order = {lab: i for i, lab in enumerate(LABELS)}
    df = df.sort_values(["label", "composite_pct"], key=lambda s: s.map(order) if s.name == "label" else s)
    return df.reindex(columns=SCREEN_COLUMNS).reset_index(drop=True)


def refresh(settings: Settings, universe_df, industry_df, fundamentals_df, prices_df,
            history_df=None, *, verbose: bool = True) -> pd.DataFrame:
    import datetime as dt

    df = build_screen(universe_df, industry_df, fundamentals_df, prices_df, settings, history_df)
    df.to_parquet(settings.data_dir / "screen.parquet", index=False)
    hist = settings.data_dir / "history"
    hist.mkdir(parents=True, exist_ok=True)
    df.to_parquet(hist / f"screen_{dt.date.today().isoformat()}.parquet", index=False)
    if verbose:
        print("Labels: " + ", ".join(f"{k} {v}" for k, v in df["label"].value_counts().items()))
    return df


def load(settings: Settings) -> pd.DataFrame:
    return pd.read_parquet(settings.data_dir / "screen.parquet")

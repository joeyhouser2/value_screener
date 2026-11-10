"""Phase 9b — through-the-cycle valuation from the annual history (:mod:`history`).

TTM multiples look cheapest at peak earnings and go missing at the trough, so these
metrics judge a company on its last ``normalized_window`` fiscal years instead:

- ``pe10``: market cap / mean earnings, each year restated to today's share count.
  Reported EPS can't be averaged: XBRL years older than the comparatives in the latest
  10-K are never restated for later splits (AAPL FY2011 $27.68, FY2012 $6.31).
  This equals price / mean split-adjusted EPS, so a company that diluted while earnings
  stayed flat looks cheaper than its current EPS justifies: read it with ``share_cagr``.
  Averages the years available, down to ``pe10_min_years`` for young companies: check
  ``years_history``, as 3 years rarely span a cycle. Only ``normalized_min_years`` or more
  count for ``pe10_pct``, so a short average never moves a long-history peer's rank.
- ``ev_midcycle_ebit``: EV / (median EBIT margin x TTM revenue)
- ``norm_fcf_yield``: mean free cash flow / market cap
- ``margin_position``: percentile of the current margin within the company's own
  fiscal-year margins (0 = below every year, 1 = above every year)
- ``cyclical``: a cyclical industry (``cyclical: true`` in ``sic_groups.yaml``) or a
  profitable company with volatile margins
- ``industry_cycle_position``: median ``margin_position`` of the industry

Share-count jumps that look like splits are removed by ratio alone, since XBRL here has
no split events, so a real offering that exactly doubles the share count passes as a
split. Fewer than ``normalized_min_years`` of data makes a metric missing, and negative
normalized earnings are missing, never "cheap". Figures are nominal, not inflation-adjusted.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import errors, history, industry, peers
from .config import Settings

# Share-count ratios treated as splits (reverse splits are the reciprocals). 3-for-2
# is left out: a 1.5x jump is more often a real offering, and treating an offering as a
# split would understate the P/E.
SPLIT_RATIOS = np.array([2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50, 100], dtype=float)
SPLIT_TOL = 0.07          # annual buybacks / dilution drift the observed ratio (AAPL 2018: 3.81)
MAX_REAL_RATIO = 5.0      # bigger non-split jumps are taken as tagging / unit errors
FINANCIALS = ("bank", "insurer")
COMPANY_COLUMNS = ["years_history", "norm_earnings", "median_margin", "norm_fcf", "margin_vol",
                   "loss_years", "share_cagr", "margin_position"]
PEER_METRICS = {"pe10": True, "ev_midcycle_ebit": True, "norm_fcf_yield": False}  # lower is cheaper?


def _is_split(ratio: float) -> bool:
    r = ratio if ratio >= 1 else 1 / ratio
    return bool(np.any(np.abs(r / SPLIT_RATIOS - 1) <= SPLIT_TOL))


def share_index(shares: np.ndarray) -> np.ndarray:
    """Share count on a split-free scale, latest year = 1.0 (gaps carry forward).

    Ratio jumps that look like splits or data errors contribute nothing; everything
    else is a real change. ``earnings / index`` restates a year to today's shares.
    """
    idx = np.ones(len(shares))
    last = np.nan
    for i, s in enumerate(shares):
        step = 1.0
        if s > 0:
            if last > 0:
                r = s / last
                if not _is_split(r) and 1 / MAX_REAL_RATIO <= r <= MAX_REAL_RATIO:
                    step = r
            last = s
        idx[i] = (idx[i - 1] if i else 1.0) * step
    return idx / idx[-1]


def _ebit(g: pd.DataFrame, special) -> pd.Series:
    """Operating income, else pretax + interest; for banks and insurers, interest is an
    operating cost, so pretax income, else net income."""
    col = lambda c: g[c] if c in g else pd.Series(np.nan, index=g.index)
    if special in FINANCIALS:
        return col("pretax_income").fillna(col("net_income"))
    return col("operating_income").fillna(col("pretax_income") + col("interest_expense").fillna(0.0))


def company_metrics(g: pd.DataFrame, *, special=None, current_margin: float = np.nan,
                    window: int = 10, min_years: int = 5, pe_min_years: int | None = None) -> dict:
    """Normalized figures for one company's fiscal years (``g`` as from ``history.wide``)."""
    g = g.sort_values("fy_end").tail(window)
    col = lambda c: g[c] if c in g else pd.Series(np.nan, index=g.index)
    ni, rev = col("net_income"), col("revenue")
    out: dict = dict.fromkeys(COMPANY_COLUMNS, np.nan)
    out["years_history"] = int(ni.notna().sum())
    out["loss_years"] = int((ni < 0).sum())

    shares = col("diluted_shares").to_numpy(dtype=float)
    idx = share_index(shares) if (shares > 0).any() else np.ones(len(g))
    if out["years_history"] >= (pe_min_years or min_years):
        out["norm_earnings"] = float((ni.to_numpy(dtype=float) / idx)[ni.notna()].mean())
    if len(g) >= 2 and idx[0] > 0:
        out["share_cagr"] = float((1 / idx[0]) ** (1 / (len(g) - 1)) - 1)

    margins = (_ebit(g, special) / rev.where(rev > 0)).dropna()
    if len(margins) >= min_years:
        out["median_margin"] = float(margins.median())
        out["margin_vol"] = float(margins.clip(-1, 1).std())   # one absurd year can't dominate
        cur = current_margin if pd.notna(current_margin) else margins.iloc[-1]
        out["margin_position"] = float(((margins < cur).sum() + 0.5 * (margins == cur).sum())
                                       / len(margins))
    fcf = (col("operating_cash_flow") - col("capex")).dropna()
    if len(fcf) >= min_years and special not in FINANCIALS:
        out["norm_fcf"] = float(fcf.mean())
    return out


def yearly_figures(g: pd.DataFrame, *, special=None, window: int = 10) -> pd.DataFrame:
    """Revenue, EBIT margin and split-adjusted EPS per fiscal year, for charting one company.

    EPS is on today's share scale, for the reason given above for ``pe10``."""
    g = g.sort_values("fy_end").tail(window)
    col = lambda c: g[c] if c in g else pd.Series(np.nan, index=g.index)
    rev = col("revenue")
    shares = col("diluted_shares").to_numpy(dtype=float)
    eps = np.full(len(g), np.nan)
    if (shares > 0).any():
        eps = col("net_income").to_numpy(dtype=float) / share_index(shares) / shares[shares > 0][-1]
    return pd.DataFrame({"fy_end": g["fy_end"].to_numpy(), "revenue": rev.to_numpy(dtype=float),
                         "margin": (_ebit(g, special) / rev.where(rev > 0)).to_numpy(dtype=float), "eps": eps})


def company_table(history_df: pd.DataFrame, df: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """Normalized figures per CIK; ``df`` supplies ``special`` and the TTM margin."""
    ctx = df.dropna(subset=["cik"]).drop_duplicates("cik").copy()
    ctx["cik"] = ctx["cik"].astype("int64")
    margin = ctx["ebit"] / ctx["revenue"].where(ctx["revenue"] > 0)
    # Banks and insurers use a different EBIT in the history: fall back to their latest FY.
    margin = margin.where(~ctx["special"].isin(FINANCIALS))
    special = dict(zip(ctx["cik"], ctx["special"]))
    current = dict(zip(ctx["cik"], margin))
    rows = []
    for cik, g in history.wide(history_df).groupby("cik"):
        try:
            rows.append({"cik": cik, **company_metrics(
                g, special=special.get(cik), current_margin=current.get(cik, np.nan),
                window=settings.normalized_window, min_years=settings.normalized_min_years,
                pe_min_years=settings.pe10_min_years)})
        except Exception as exc:  # noqa: BLE001 - logged, run continues
            errors.log(settings.data_dir, "normalized", str(cik), exc)
    return pd.DataFrame(rows, columns=["cik", *COMPANY_COLUMNS])


def add_normalized(df: pd.DataFrame, history_df: pd.DataFrame | None, settings: Settings) -> pd.DataFrame:
    """Add the through-the-cycle columns to the screen (missing when no history)."""
    df = df.copy()
    table = pd.DataFrame(columns=["cik", *COMPANY_COLUMNS], dtype=float)
    if history_df is not None and len(history_df):
        table = company_table(history_df, df, settings)
    df = df.merge(table.astype({"cik": "float64"}), on="cik", how="left") if "cik" in df else df
    for c in COMPANY_COLUMNS:
        df[c] = pd.to_numeric(df[c], errors="coerce") if c in df else np.nan

    financial = df["special"].isin(FINANCIALS)
    pos = lambda s: s.where(s > 0)
    df["pe10"] = df["market_cap"] / pos(df["norm_earnings"])
    df["midcycle_ebit"] = (df["median_margin"] * df["revenue"]).where(~financial)
    df["ev_midcycle_ebit"] = pos(df["enterprise_value"]) / pos(df["midcycle_ebit"])
    df["norm_fcf_yield"] = df["norm_fcf"] / pos(df["market_cap"])
    for m, lower in PEER_METRICS.items():
        ranked = df[m].where(df["years_history"] >= settings.normalized_min_years) if m == "pe10" else df[m]
        df[f"{m}_pct"], _ = peers.peer_pct(df, ranked, lower, settings.min_peers)

    groups = industry.load_groups(settings.config_dir)["groups"]
    cyclical_groups = {r["group"] for r in groups if r.get("cyclical")}
    # Not for financials and REITs: their margins move with rates and revenue tagging.
    by_margins = (df["margin_vol"] >= settings.cyclical_margin_vol) & (df["median_margin"] > 0) \
        & (df["loss_years"] <= df["years_history"] / 2) & ~df["special"].isin([*FINANCIALS, "reit"])
    df["cyclical"] = df["industry_group"].isin(cyclical_groups) | by_margins

    position = df["margin_position"].groupby(df["industry_group"])
    df["industry_cycle_position"] = position.transform("median").where(
        position.transform("count") >= settings.min_peers)
    return df

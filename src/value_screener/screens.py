"""Phase 9e — the deep value and cyclical trough screens; ``label`` is untouched.

A screen is a cheapness gate plus checks that the company can last. ``*_reasons`` says
which gate a stock passed and, when it then failed, why, so near misses can be read too.

- ``deep_value``: cheap on assets or sales (P/TBV or EV/sales in the industry's cheapest
  ``deep_value_pct``, or P/NCAV under 1) with a sound F-score, and P/TBV under
  ``deep_value_max_p_tbv``. That ceiling is absolute: the cheapest of an expensive industry
  isn't deep value, and low EV/sales alone mostly finds thin margins (MCK, CAH).
- ``cyclical_trough``: cyclical, ``trough_min_years`` of history, margins near the low of
  their own range, and cheap on through-the-cycle earnings. Its F-score bar is lower
  because the F-score rewards rising margins and returns, which a trough doesn't have.
  Nothing here tells a cyclical low from a permanent decline: that takes reading the filings.
- ``turning``: revenue above a year ago and price above its 200-day average. A hint only,
  and "both" because either one alone is true of two thirds of the market. Blank when a
  missing figure could still decide it, so no data doesn't read as "not turning".

Checks shared by both:

- F-score, scaled to the tests that could run (6 of 9 equals 4 of 6), with at least
  ``screen_min_f_tests`` run, so a stock can't pass on no evidence.
- Interest coverage not failed. An Altman distress score counts only when coverage doesn't
  clear the company, since Z'' also flags healthy names (see :mod:`survival`).
- Debt known, and no serial dilution: ``share_cagr`` flatters P/E10 otherwise.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import normalized, peers, survival
from .config import Settings

SCREENS = {"Deep value": "deep_value", "Cyclical trough": "cyclical_trough"}
NO_EV_SALES = (*normalized.FINANCIALS, "reit")
FULL_F_TESTS = 9


def _join(flags: pd.DataFrame) -> pd.Series:
    names = list(flags.columns)
    return pd.Series([", ".join(n for n, on in zip(names, row) if on) for row in flags.to_numpy()],
                     index=flags.index, dtype=object)


def _reasons(gates: pd.DataFrame, fails: pd.DataFrame) -> pd.Series:
    """Gates passed, then what failed; blank for a stock that passed no gate."""
    cheap, failed = _join(gates), _join(fails)
    out = cheap.where(failed == "", cheap + "; failed: " + failed)
    return out.where(gates.any(axis=1), "")


def add_screens(df: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    df = df.copy()
    num = lambda c: pd.to_numeric(df[c], errors="coerce") if c in df else pd.Series(np.nan, index=df.index)
    flag = lambda c: df[c].fillna(False).astype(bool) if c in df else pd.Series(False, index=df.index)
    special = df["special"] if "special" in df else pd.Series(None, index=df.index, dtype=object)
    group = df["industry_group"] if "industry_group" in df else pd.Series(None, index=df.index, dtype=object)
    no_ev = special.isin(NO_EV_SALES) | group.isin(survival.CLIENT_FUND_GROUPS)

    ev, revenue, mcap = num("enterprise_value"), num("revenue"), num("market_cap")
    df["ev_sales"] = (ev.where(ev > 0) / revenue.where(revenue > 0)).where(~no_ev)
    df["ev_sales_pct"], _ = peers.peer_pct(df, df["ev_sales"], True, settings.min_peers)

    coverage = df["coverage_ok"].astype("boolean") if "coverage_ok" in df else pd.Series(pd.NA, index=df.index, dtype="boolean")
    zone = df["altman_zone"] if "altman_zone" in df else pd.Series(None, index=df.index, dtype=object)
    f, tests = num("f_score"), num("f_score_n")
    f_of_full = f * FULL_F_TESTS / tests.where(tests > 0)
    too_few = ~(tests >= settings.screen_min_f_tests)
    usable = (~flag("stale")) & mcap.notna() & (mcap >= settings.screen_min_market_cap)
    survive = {
        "too small or stale": ~usable,
        "too few F-score tests": too_few,
        "interest coverage": coverage.fillna(False).astype(bool).eq(False) & coverage.notna(),
        "Altman distress": (zone == "distress") & ~coverage.fillna(False).astype(bool),
        "debt unknown": flag("debt_unknown") & ~no_ev,
        "dilution": num("share_cagr") > settings.max_share_cagr,
    }

    ncav = num("p_ncav")
    gates = pd.DataFrame({
        "P/TBV": num("p_tbv_pct") <= settings.deep_value_pct,
        "P/NCAV": (ncav < 1) & (ncav >= settings.min_p_ncav),
        "EV/sales": df["ev_sales_pct"] <= settings.deep_value_pct,
    })
    fails = pd.DataFrame({**survive, "F-score": ~too_few & (f_of_full < settings.deep_value_min_f),
                          "no asset backing": ~(num("p_tbv") <= settings.deep_value_max_p_tbv)})
    df["deep_value"] = gates.any(axis=1) & ~fails.any(axis=1)
    df["deep_value_reasons"] = _reasons(gates, fails)

    at_trough = flag("cyclical") & (num("years_history") >= settings.trough_min_years) \
        & (num("margin_position") <= settings.trough_max_margin_position)
    gates = pd.DataFrame({
        "P/E10": at_trough & (num("pe10_pct") <= settings.trough_pct),
        "EV/mid-cycle EBIT": at_trough & ~no_ev & (num("ev_midcycle_ebit_pct") <= settings.trough_pct),
    })
    fails = pd.DataFrame({**survive, "F-score": ~too_few & (f_of_full < settings.trough_min_f)})
    df["cyclical_trough"] = gates.any(axis=1) & ~fails.any(axis=1)
    df["cyclical_trough_reasons"] = _reasons(gates, fails)

    prev, vs_200d = num("revenue_prev"), num("price_vs_200d")
    rev_up = (revenue > prev).astype("boolean").mask(revenue.isna() | prev.isna())
    above = (vs_200d > 0).astype("boolean").mask(vs_200d.isna())
    df["turning"] = rev_up & above
    return df


def cycle_table(df: pd.DataFrame) -> pd.DataFrame:
    """One row per industry with a cycle position, lowest margins against own history first."""
    g = df[df["industry_cycle_position"].notna()].groupby("industry_group")
    out = pd.DataFrame({
        "cycle_position": g["industry_cycle_position"].first(),
        "stocks": g.size(),
        "with_history": g["margin_position"].count(),
        "cyclical": g["cyclical"].sum().astype(int),
        "deep_value": g["deep_value"].sum().astype(int),
        "cyclical_trough": g["cyclical_trough"].sum().astype(int),
        "turning": g["turning"].sum().astype(int),
    })
    return out.sort_values("cycle_position").reset_index()

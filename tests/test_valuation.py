"""Phase 5 — metrics, peer percentiles, quality filter and labels. Synthetic data."""

import numpy as np
import pandas as pd
import pytest

from value_screener import valuation as v
from value_screener.config import Settings

S = Settings(min_peers=8)


def _co(ticker, group="Software & IT Services", sector="Technology", special=None, **kw):
    base = dict(ticker=ticker, name=ticker, exchange="NASDAQ", cik=1, industry_group=group,
                sector=sector, special=special, market_cap=1000.0, price=10.0,
                shares_outstanding=100.0, net_income=50.0, net_income_prev=40.0, revenue=500.0,
                equity=400.0, total_debt=100.0, cash=50.0, operating_income=80.0, d_and_a=20.0,
                operating_cash_flow=90.0, capex=30.0, dividends=10.0, stale=False)
    base.update(kw)
    return base


def _industry(n=10, **cheap_kw):
    """n identical-ish peers with rising market caps; peer 0 is the cheapest."""
    rows = [_co(f"P{i}", market_cap=1000.0 * (1 + i)) for i in range(n)]
    return pd.DataFrame(rows)


def _run(df):
    return v.score(v.compute_metrics(df), S).set_index("ticker")


def test_metrics():
    m = v.compute_metrics(pd.DataFrame([_co("A")])).iloc[0]
    assert m["pe"] == pytest.approx(20)
    assert m["ps"] == pytest.approx(2)
    assert m["pb"] == pytest.approx(2.5)
    assert m["ev_ebitda"] == pytest.approx((1000 + 100 - 50) / 100)
    assert m["fcf_yield"] == pytest.approx(0.06)
    assert m["earnings_growth"] == pytest.approx(0.25)
    assert m["peg"] == pytest.approx(20 / 25)
    assert m["price_to_graham"] == pytest.approx(10 / np.sqrt(22.5 * 0.5 * 4.0))


def test_negative_earnings_is_missing_not_cheap():
    m = v.compute_metrics(pd.DataFrame([_co("L", net_income=-5.0)])).iloc[0]
    assert np.isnan(m["pe"]) and np.isnan(m["peg"]) and np.isnan(m["price_to_graham"])
    assert m["roe"] < 0                                     # still informative for quality


def test_cheapest_peer_is_undervalued_dearest_overvalued():
    out = _run(_industry())
    assert out.loc["P0", "composite_pct"] == pytest.approx(0.0)
    assert out.loc["P9", "composite_pct"] == pytest.approx(1.0)
    assert out.loc["P0", "label"] == "Undervalued"
    assert out.loc["P9", "label"] == "Overvalued"
    assert out.loc["P5", "label"] == "Fair"
    assert (out["peer_level"] == "industry").all()


def test_cheap_but_failing_quality_is_not_undervalued():
    df = _industry()
    df.loc[0, ["total_debt", "cash"]] = [5000.0, 4950.0]   # same EV, debt/equity 12.5x
    out = _run(df)
    assert out.loc["P0", "composite_pct"] == pytest.approx(0.0)
    assert out.loc["P0", "label"] == "Fair"
    assert out.loc["P0", "cheap_fails_quality"]
    assert out.loc["P0", "quality_issues"] == "high debt"


def test_small_industry_falls_back_to_sector():
    df = pd.concat([_industry(),
                    pd.DataFrame([_co("TINY", group="Tiny Niche", market_cap=500.0)])])
    out = _run(df)
    assert out.loc["TINY", "peer_level"] == "sector"
    assert out.loc["TINY", "pe_pct"] == pytest.approx(0.0)          # cheapest in sector


def test_banks_use_book_and_earnings_only():
    banks = pd.DataFrame([_co(f"B{i}", group="Banks", sector="Financials", special="bank",
                              market_cap=1000.0 * (1 + i)) for i in range(9)])
    out = _run(banks)
    assert out["ev_ebitda_pct"].isna().all() and out["ps_pct"].isna().all()
    assert out["pb_pct"].notna().all() and (out["n_metrics"] == 2).all()
    # FCF isn't a bank quality test.
    assert out.loc["B0", "label"] == "Undervalued"


def test_reits_use_p_ffo():
    reits = pd.DataFrame([_co(f"R{i}", group="REITs", sector="Real Estate", special="reit",
                              market_cap=1000.0 * (1 + i)) for i in range(9)])
    out = _run(reits)
    assert out["p_ffo_pct"].notna().all() and out["pe_pct"].isna().all()


def test_insufficient_data_cases():
    df = pd.concat([_industry(), pd.DataFrame([
        _co("NOCAP", market_cap=np.nan),
        _co("STALE", stale=True),
        _co("THIN", net_income=-1.0, revenue=np.nan, operating_income=np.nan,
            operating_cash_flow=np.nan),
    ])])
    out = _run(df)
    for t in ("NOCAP", "STALE", "THIN"):
        assert out.loc[t, "label"] == "Insufficient data"


def test_build_screen_joins_and_sorts():
    uni = pd.DataFrame({"ticker": ["A", "B"], "name": ["A", "B"], "exchange": "NYSE", "cik": [1, 2]})
    ind = pd.DataFrame({"ticker": ["A", "B"], "industry_group": "X", "sector": "Y", "special": None,
                        "sic": None, "sic_description": None, "industry_source": "sic"})
    fund = pd.DataFrame([{k: val for k, val in _co(t).items()
                          if k not in ("name", "exchange", "cik", "industry_group", "sector",
                                       "special", "market_cap", "price")} for t in ("A", "B")])
    px = pd.DataFrame({"ticker": ["A", "B"], "price": [10.0, 20.0], "price_date": None,
                       "market_cap": [1000.0, 2000.0]})
    out = v.build_screen(uni, ind, fund, px, Settings(min_peers=2))
    assert list(out.columns) == v.SCREEN_COLUMNS
    assert out["ticker"].tolist() == ["A", "B"]

"""Phase 9e — deep value and cyclical trough screens on hand-built rows."""

import datetime as dt

import numpy as np
import pandas as pd

from value_screener import screens as sc, valuation
from value_screener.config import Settings

S = Settings(min_peers=8)


def _row(**kw):
    base = dict(
        ticker="X", special=None, industry_group="Machinery", sector="Industrials", stale=False,
        market_cap=1e9, enterprise_value=np.nan, revenue=2e9, revenue_prev=2.1e9, price_vs_200d=-0.1,
        f_score=7.0, f_score_n=9.0, coverage_ok=True, altman_zone="safe", debt_unknown=False,
        share_cagr=0.0, p_tbv=1.0, p_tbv_pct=0.1, p_ncav=np.nan, cyclical=True, years_history=10.0,
        margin_position=0.1, pe10_pct=0.1, ev_midcycle_ebit_pct=0.1)
    base.update(kw)
    return base


def _run(*rows):
    df = pd.DataFrame(list(rows)).astype({"coverage_ok": "boolean"})
    return sc.add_screens(df, S)


def _one(**kw):
    return _run(_row(**kw)).iloc[0]


def test_both_screens_pass_and_say_why():
    r = _one()
    assert r["deep_value"] and r["cyclical_trough"]
    assert "P/TBV" in r["deep_value_reasons"] and "failed" not in r["deep_value_reasons"]
    assert r["cyclical_trough_reasons"] == "P/E10, EV/mid-cycle EBIT"


def test_no_gate_means_no_reason():
    r = _one(p_tbv_pct=0.9, cyclical=False)
    assert not r["deep_value"] and r["deep_value_reasons"] == ""
    assert not r["cyclical_trough"] and r["cyclical_trough_reasons"] == ""


def test_near_miss_names_what_failed():
    r = _one(f_score=2.0)
    assert not r["deep_value"] and r["deep_value_reasons"] == "P/TBV; failed: F-score"
    assert r["cyclical_trough_reasons"].endswith("failed: F-score")


def test_f_score_is_scaled_to_the_tests_that_ran():
    assert _one(f_score=6.0, f_score_n=9.0)["deep_value"]
    assert _one(f_score=4.0, f_score_n=6.0)["deep_value"]          # 6 of 9
    assert not _one(f_score=5.0, f_score_n=9.0)["deep_value"]
    assert not _one(f_score=3.0, f_score_n=6.0)["deep_value"]      # 4.5 of 9


def test_a_spin_off_since_the_last_balance_sheet_fails_both_screens():
    spun = _one(spin_off_date=dt.date(2026, 10, 1), equity_end=pd.Timestamp("2026-06-30"))
    assert not spun["deep_value"] and not spun["cyclical_trough"]
    assert "failed: spin-off since last balance sheet" in spun["deep_value_reasons"]
    reported = _one(spin_off_date=dt.date(2026, 10, 1), equity_end=pd.Timestamp("2026-12-31"))
    assert reported["deep_value"] and reported["cyclical_trough"]
    assert _one(spin_off_date=None, equity_end=pd.Timestamp("2026-06-30"))["deep_value"]


def test_trough_has_the_lower_f_bar():
    r = _one(f_score=4.0)
    assert r["cyclical_trough"] and not r["deep_value"]


def test_too_few_tests_cannot_pass():
    r = _one(f_score=5.0, f_score_n=5.0)
    assert not r["deep_value"] and "too few F-score tests" in r["deep_value_reasons"]
    assert "too few" in _one(f_score=np.nan, f_score_n=np.nan)["deep_value_reasons"]


def test_failed_coverage_fails():
    r = _one(coverage_ok=False, altman_zone="safe")
    assert not r["cyclical_trough"] and "interest coverage" in r["cyclical_trough_reasons"]


def test_distress_counts_unless_coverage_clears_it():
    assert _one(altman_zone="distress", coverage_ok=True)["cyclical_trough"]
    r = _one(altman_zone="distress", coverage_ok=pd.NA)
    assert not r["cyclical_trough"] and "Altman distress" in r["cyclical_trough_reasons"]


def test_unjudged_coverage_and_altman_do_not_fail():
    assert _one(altman_zone=None, coverage_ok=pd.NA)["cyclical_trough"]


def test_debt_unknown_and_dilution_fail():
    assert "debt unknown" in _one(debt_unknown=True)["deep_value_reasons"]
    assert not _one(debt_unknown=True)["cyclical_trough"]
    r = _one(share_cagr=0.08)
    assert not r["deep_value"] and "dilution" in r["cyclical_trough_reasons"]
    assert _one(share_cagr=np.nan)["cyclical_trough"]              # no share history: no verdict


def test_stale_or_tiny_excluded():
    assert "too small or stale" in _one(stale=True)["deep_value_reasons"]
    assert not _one(market_cap=1e7)["cyclical_trough"]
    assert not _one(market_cap=np.nan)["deep_value"]


def test_deep_value_needs_asset_backing():
    # MCK: EV/sales looks cheap, but tangible book is negative (P/TBV missing).
    rows = [_row(ticker=f"P{i}", enterprise_value=1e9 * (i + 1), p_tbv_pct=0.9) for i in range(10)]
    rows[0].update(p_tbv=np.nan)
    rows[1].update(p_tbv=5.0)
    rows[2].update(p_tbv=2.0, enterprise_value=1.5e9)
    out = _run(*rows).set_index("ticker")
    assert out.loc["P0", "ev_sales_pct"] == 0
    assert not out.loc["P0", "deep_value"] and out.loc["P0", "deep_value_reasons"] == "EV/sales; failed: no asset backing"
    assert not out.loc["P1", "deep_value"]
    assert out.loc["P2", "deep_value"]


def test_p_ncav_gate_has_a_floor():
    gate = dict(p_tbv_pct=0.9)
    assert _one(p_ncav=0.5, **gate)["deep_value"]
    assert _one(p_ncav=1.5, **gate)["deep_value_reasons"] == ""
    assert _one(p_ncav=0.0002, **gate)["deep_value_reasons"] == ""  # a data error, not a bargain


def test_trough_gates():
    assert not _one(years_history=6.0)["cyclical_trough"]
    assert not _one(margin_position=0.5)["cyclical_trough"]
    assert not _one(cyclical=False)["cyclical_trough"]
    assert _one(pe10_pct=np.nan)["cyclical_trough_reasons"] == "EV/mid-cycle EBIT"
    assert _one(pe10_pct=0.5, ev_midcycle_ebit_pct=0.5)["cyclical_trough_reasons"] == ""


def test_ev_gates_skip_financials_and_client_money():
    bank = _one(special="bank", p_tbv_pct=0.9, enterprise_value=1e9)
    assert np.isnan(bank["ev_sales"]) and bank["deep_value_reasons"] == ""
    broker = _one(industry_group="Capital Markets", pe10_pct=0.9, enterprise_value=1e9)
    assert np.isnan(broker["ev_sales"]) and broker["cyclical_trough_reasons"] == ""


def test_debt_unknown_does_not_fail_a_bank():
    assert _one(special="bank", debt_unknown=True)["deep_value"]


def test_turning_needs_both_revenue_and_price():
    assert _one(revenue=2.2e9, price_vs_200d=0.1)["turning"]
    assert not _one(revenue=2.2e9, price_vs_200d=-0.1)["turning"]
    assert not _one(revenue=2.0e9, price_vs_200d=0.1)["turning"]


def test_turning_is_blank_only_when_missing_data_could_decide_it():
    assert pd.isna(_one(revenue=2.2e9, price_vs_200d=np.nan)["turning"])
    assert pd.isna(_one(revenue=np.nan, price_vs_200d=0.1)["turning"])
    assert not _one(revenue=2.0e9, price_vs_200d=np.nan)["turning"]      # revenue already says no
    assert not _one(revenue=np.nan, price_vs_200d=-0.1)["turning"]


def test_cycle_table_sorted_lowest_first_and_skips_small_industries():
    df = pd.DataFrame({
        "industry_group": ["A", "A", "B", "B", "C"], "industry_cycle_position": [0.8, 0.8, 0.2, 0.2, np.nan],
        "margin_position": [0.7, 0.9, 0.1, np.nan, 0.5], "cyclical": [True, False, True, True, True],
        "deep_value": [True, False, False, False, True], "cyclical_trough": [False, False, True, False, True],
        "turning": pd.array([True, pd.NA, False, pd.NA, True], dtype="boolean")})
    out = sc.cycle_table(df)
    assert out["industry_group"].tolist() == ["B", "A"]
    assert out.iloc[0].to_dict() == {"industry_group": "B", "cycle_position": 0.2, "stocks": 2, "with_history": 1,
                                     "cyclical": 2, "deep_value": 0, "cyclical_trough": 1, "turning": 0}
    assert out.iloc[1]["turning"] == 1


def test_missing_columns_no_crash():
    out = sc.add_screens(pd.DataFrame({"ticker": ["X"], "market_cap": [1e9], "industry_group": ["A"],
                                       "sector": ["B"]}), S).iloc[0]
    assert not out["deep_value"] and not out["cyclical_trough"] and pd.isna(out["turning"])


def test_new_columns_reach_the_screen_and_its_snapshot(tmp_path):
    s = Settings(data_dir=tmp_path)
    uni = pd.DataFrame({"ticker": ["AAA"], "name": ["A"], "exchange": "NYSE", "cik": [1.0]})
    ind = pd.DataFrame({"ticker": ["AAA"], "industry_group": "Machinery", "sector": "Industrials", "special": None})
    fund = pd.DataFrame({"ticker": ["AAA"], "revenue": [10.0], "net_income": [1.0], "stale": False})
    px = pd.DataFrame({"ticker": ["AAA"], "price": [1.0], "price_date": dt.date(2026, 10, 30), "market_cap": [100.0]})
    valuation.refresh(s, uni, ind, fund, px, verbose=False)
    snapshot = next((tmp_path / "history").glob("screen_*.parquet"))
    new = {"deep_value", "deep_value_reasons", "cyclical_trough", "cyclical_trough_reasons", "turning",
           "pe10", "margin_position", "f_score", "altman_zone", "p_tbv", "ev_sales", "off_52w_high"}
    assert new <= set(pd.read_parquet(snapshot).columns)

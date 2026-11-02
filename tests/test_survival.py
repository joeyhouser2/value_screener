"""Phase 9c — F-score, Altman Z, coverage and asset values against hand-computed figures."""

import numpy as np
import pandas as pd
import pytest

from value_screener import survival as s
from value_screener.config import load_settings

S = load_settings()
AS_OF = pd.Timestamp("2025-12-31")


def _years(**cols):
    n = len(next(iter(cols.values())))
    return pd.DataFrame({"fy_end": pd.date_range(end="2025-12-31", periods=n, freq="YE"), **cols})


# Improving on all nine tests.
GOOD = dict(
    net_income=[5.0, 8.0, 12.0], operating_cash_flow=[9.0, 10.0, 15.0],
    assets=[100.0, 100.0, 100.0], total_debt=[40.0, 30.0, 20.0],
    assets_current=[30.0, 30.0, 40.0], liabilities_current=[20.0, 20.0, 20.0],
    diluted_shares=[100.0, 100.0, 99.0], gross_profit=[30.0, 30.0, 45.0],
    revenue=[100.0, 100.0, 110.0])


def test_all_nine_pass():
    r = s.f_score(_years(**GOOD))
    assert r["f_score"] == 9 and r["f_score_n"] == 9


def test_all_nine_fail():
    t = s.f_tests(_years(
        net_income=[8.0, 8.0, -2.0], operating_cash_flow=[9.0, 9.0, -3.0],
        assets=[100.0, 100.0, 100.0], total_debt=[20.0, 20.0, 40.0],
        assets_current=[40.0, 40.0, 30.0], liabilities_current=[20.0, 20.0, 20.0],
        diluted_shares=[100.0, 100.0, 120.0], gross_profit=[40.0, 40.0, 30.0],
        revenue=[100.0, 100.0, 90.0]))
    assert sum(t.values()) == 0 and not any(np.isnan(v) for v in t.values())


def test_each_test_reads_its_own_inputs():
    # NI 12 on opening assets 100 = 12%; last year 8 on 100 = 8%.
    t = s.f_tests(_years(**{**GOOD, "net_income": [5.0, 13.0, 12.0]}))
    assert t["roa"] == 1 and t["roa_up"] == 0
    t = s.f_tests(_years(**{**GOOD, "operating_cash_flow": [9.0, 10.0, 11.0]}))
    assert t["cfo"] == 1 and t["accruals"] == 0


def test_missing_input_is_not_a_failure():
    r = s.f_score(_years(**{k: v for k, v in GOOD.items() if k != "gross_profit"}))
    assert r["f_score"] == 8 and r["f_score_n"] == 8
    t = s.f_tests(_years(**{k: v for k, v in GOOD.items() if k != "gross_profit"}))
    assert np.isnan(t["margin_up"])


def test_debt_free_passes_leverage():
    assert s.f_tests(_years(**{**GOOD, "total_debt": [0.0, 0.0, 0.0]}))["leverage"] == 1


def test_a_split_is_not_issuance():
    assert s.f_tests(_years(**{**GOOD, "diluted_shares": [100.0, 100.0, 400.0]}))["no_issuance"] == 1
    assert s.f_tests(_years(**{**GOOD, "diluted_shares": [100.0, 100.0, 125.0]}))["no_issuance"] == 0
    assert s.f_tests(_years(**{**GOOD, "diluted_shares": [100.0, 100.0, 100.2]}))["no_issuance"] == 1


def test_a_gap_in_fiscal_years_blocks_year_on_year_tests():
    g = _years(**GOOD)
    g.loc[2, "fy_end"] = pd.Timestamp("2026-12-31")
    t = s.f_tests(g)
    assert t["cfo"] == 1 and t["accruals"] == 1
    assert np.isnan(t["roa"]) and np.isnan(t["roa_up"]) and np.isnan(t["margin_up"])


def test_banks_skip_current_ratio_and_gross_margin():
    t = s.f_tests(_years(**GOOD), special="bank")
    assert np.isnan(t["liquidity"]) and np.isnan(t["margin_up"]) and t["roa"] == 1


def _row(**kw):
    end = kw.pop("end", AS_OF)
    base = dict(
        ticker="X", cik=1.0, special=None, sic=3500, as_of=AS_OF, market_cap=600.0,
        assets=1000.0, assets_current=400.0, liabilities_current=200.0, liabilities=500.0,
        retained_earnings=300.0, equity=500.0, ebit=100.0, revenue=1200.0,
        goodwill=50.0, intangibles=30.0, interest_expense=20.0, cash=10.0, total_debt=200.0)
    for c in ("assets", "assets_current", "liabilities_current", "liabilities", "retained_earnings",
              "equity", "goodwill", "intangibles", "interest_expense", "cash", "total_debt",
              "liabilities_and_equity", "equity_incl_nci"):
        base[f"{c}_end"] = end
    base.update(kw)
    return base


def _run(*rows, history=None):
    df = pd.DataFrame(list(rows))
    df["industry_group"] = "Machinery"
    df["sector"] = "Industrials"
    return s.add_survival(df, history, S)


def test_altman_z_for_a_manufacturer():
    r = _run(_row()).iloc[0]
    # 1.2(.2) + 1.4(.3) + 3.3(.1) + 0.6(600/500) + 1.0(1.2) = 2.91
    assert r["altman_z"] == pytest.approx(2.91)
    assert r["altman_model"] == "Z" and r["altman_zone"] == "grey"


def test_altman_z2_for_a_non_manufacturer_uses_its_own_zones():
    r = _run(_row(sic=7370)).iloc[0]
    # 6.56(.2) + 3.26(.3) + 6.72(.1) + 1.05(500/500) = 4.012: revenue and market cap play no part
    assert r["altman_z"] == pytest.approx(4.012)
    assert r["altman_model"] == "Z''" and r["altman_zone"] == "safe"
    # About 1.80 is grey on Z'', while the same weak company on the Z scale is distress.
    weak = _run(_row(sic=7370, retained_earnings=-120.0, equity=100.0)).iloc[0]
    assert weak["altman_z"] == pytest.approx(1.312 - 0.3912 + 0.672 + 0.21)
    assert weak["altman_zone"] == "grey"
    assert _run(_row(retained_earnings=-120.0, equity=100.0, market_cap=100.0, revenue=100.0))\
        .iloc[0]["altman_zone"] == "distress"


def test_altman_na_for_financials_and_unclassified_balance_sheets():
    df = _run(_row(special="bank"), _row(special="insurer"), _row(special="reit"),
              _row(assets_current=np.nan))
    assert df["altman_z"].isna().all() and df["altman_zone"].isna().all()
    assert df["altman_model"].isna().all() and df["ncav"].isna().all()


def test_interest_coverage_and_net_cash():
    r = _run(_row()).iloc[0]
    assert r["interest_coverage"] == 5 and not r["net_cash"] and r["coverage_ok"]
    r = _run(_row(ebit=30.0)).iloc[0]
    assert r["interest_coverage"] == 1.5 and not r["coverage_ok"]
    r = _run(_row(ebit=-30.0, cash=500.0)).iloc[0]
    assert r["net_cash"] and r["coverage_ok"]                        # net cash passes on a loss
    r = _run(_row(interest_expense=np.nan)).iloc[0]
    assert pd.isna(r["coverage_ok"])                                 # debt, but no interest tag
    assert pd.isna(_run(_row(special="bank", cash=500.0)).iloc[0]["coverage_ok"])


def test_unknown_debt_is_not_net_cash():
    r = _run(_row(cash=300.0, total_debt=200.0)).iloc[0]
    assert r["net_cash"]
    assert not _run(_row(cash=300.0, total_debt=np.nan, debt_unknown=True)).iloc[0]["net_cash"]
    # No debt tag at all: nothing reported, so net cash stands.
    assert _run(_row(cash=300.0, total_debt=np.nan, total_debt_end=pd.NaT)).iloc[0]["net_cash"]


def test_stale_interest_is_not_used():
    r = _run(_row(interest_expense_end=pd.Timestamp("2021-12-31"))).iloc[0]
    assert np.isnan(r["interest_coverage"])


def test_tangible_book_and_ncav():
    r = _run(_row()).iloc[0]
    assert r["tangible_book"] == 420 and r["p_tbv"] == pytest.approx(600 / 420)
    assert r["ncav"] == -100 and np.isnan(r["p_ncav"])               # negative is missing, not cheap
    r = _run(_row(assets_current=900.0, market_cap=300.0)).iloc[0]
    assert r["ncav"] == 400 and r["p_ncav"] == 0.75


def test_negative_tangible_book_is_missing():
    r = _run(_row(goodwill=600.0)).iloc[0]
    assert r["tangible_book"] == -130 and np.isnan(r["p_tbv"])


def test_balance_sheet_tags_must_share_a_filing():
    old = pd.Timestamp("2021-12-31")
    assert np.isnan(_run(_row(assets_current_end=old)).iloc[0]["ncav"])
    assert np.isnan(_run(_row(assets_current_end=old)).iloc[0]["altman_z"])
    # All old relative to the income statement: nothing is current.
    r = _run(_row(end=old)).iloc[0]
    assert np.isnan(r["tangible_book"]) and np.isnan(r["altman_z"])


def test_goodwill_from_the_last_10k_counts_but_an_ancient_tag_is_written_off():
    assert _run(_row(goodwill_end=AS_OF - pd.Timedelta(days=270))).iloc[0]["tangible_book"] == 420
    assert _run(_row(goodwill_end=pd.Timestamp("2020-12-31"))).iloc[0]["tangible_book"] == 470


def test_liabilities_derived_when_not_tagged():
    r = _run(_row(liabilities=np.nan, liabilities_and_equity=1000.0, equity_incl_nci=520.0)).iloc[0]
    assert r["ncav"] == 400 - 480
    r = _run(_row(liabilities=np.nan, liabilities_and_equity=1000.0, equity_incl_nci=np.nan)).iloc[0]
    assert r["ncav"] == 400 - 500


def _long(cik, wide):
    out = wide.melt(id_vars="fy_end", var_name="concept", value_name="val").assign(cik=cik)
    return out


def test_f_score_joins_on_cik_and_goes_missing_when_annuals_stop():
    h = _long(1, _years(**GOOD))
    df = _run(_row(), _row(cik=2.0), history=h)
    assert df["f_score"].iloc[0] == 9 and np.isnan(df["f_score"].iloc[1])
    assert df["f_score_fy"].iloc[0] == AS_OF
    later = _run(_row(as_of=AS_OF + pd.Timedelta(days=800),
                      end=AS_OF + pd.Timedelta(days=800)), history=h).iloc[0]
    assert np.isnan(later["f_score"])


def test_no_history_no_crash():
    r = _run(_row()).iloc[0]
    assert np.isnan(r["f_score"]) and r["altman_z"] == pytest.approx(2.91)


def test_missing_columns_no_crash():
    df = s.add_survival(pd.DataFrame({"ticker": ["X"], "market_cap": [1.0], "industry_group": ["A"],
                                      "sector": ["B"]}), None, S)
    assert np.isnan(df["altman_z"].iloc[0]) and pd.isna(df["coverage_ok"].iloc[0])


def test_balance_sheet_total_stands_in_when_assets_tag_lags():
    # LEN: the latest 10-Q tags liabilities + equity and equity, but not assets or liabilities.
    prior = AS_OF - pd.Timedelta(days=92)
    r = _run(_row(assets=900.0, assets_end=prior, liabilities=450.0, liabilities_end=prior,
                  liabilities_and_equity=1000.0, equity_incl_nci=np.nan,
                  assets_current=np.nan)).iloc[0]
    assert r["tangible_book"] == 420 and r["p_tbv"] == pytest.approx(600 / 420)
    # An older total doesn't replace a current assets tag.
    r = _run(_row(liabilities_and_equity=5.0, liabilities_and_equity_end=prior)).iloc[0]
    assert r["altman_z"] == pytest.approx(2.91)


def test_brokers_get_no_altman_or_ncav():
    df = pd.DataFrame([_row(assets_current=900.0)])
    df["industry_group"], df["sector"] = "Capital Markets", "Financials"
    r = s.add_survival(df, None, S).iloc[0]
    assert np.isnan(r["altman_z"]) and np.isnan(r["ncav"]) and r["tangible_book"] == 420

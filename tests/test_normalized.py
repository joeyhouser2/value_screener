"""Phase 9b — split-safe normalized earnings, cycle position, cyclical flag."""

import numpy as np
import pandas as pd
import pytest

from value_screener import normalized as n
from value_screener.config import load_settings


def _hist(ni, rev=None, shares=None, **extra):
    years = len(ni)
    d = {"fy_end": pd.date_range("2014-12-31", periods=years, freq="YE"), "net_income": ni}
    d["revenue"] = rev if rev is not None else [100.0] * years
    if shares is not None:
        d["diluted_shares"] = shares
    return pd.DataFrame({**d, **extra})


def test_split_jumps_removed_but_real_changes_kept():
    idx = n.share_index(np.array([100.0, 95.0, 361.0, 400.0]))   # 4-for-1 net of a 5% buyback
    assert idx[-1] == 1.0
    assert idx[2] / idx[1] == 1.0                                # the split contributes nothing
    assert idx[1] / idx[0] == pytest.approx(0.95)                # buyback is real
    assert idx[3] / idx[2] == pytest.approx(400 / 361)           # dilution is real


def test_reverse_split_and_unit_error_ignored_gaps_carry():
    assert list(n.share_index(np.array([1000.0, 100.0]))) == [1.0, 1.0]        # 1-for-10
    assert list(n.share_index(np.array([1e9, 1e6]))) == [1.0, 1.0]             # thousands vs units
    idx = n.share_index(np.array([100.0, np.nan, 110.0]))
    assert idx[0] == pytest.approx(100 / 110) and idx[1] == idx[0]


def test_norm_earnings_immune_to_split_and_restated_for_dilution():
    # Same earnings across a 10-for-1 split: real share count never changed.
    m = n.company_metrics(_hist([10.0] * 6, shares=[1, 1, 1, 10, 10, 10]))
    assert m["norm_earnings"] == pytest.approx(10)
    assert m["share_cagr"] == pytest.approx(0)
    # Real dilution: mean EPS x today's shares, so flat earnings normalize above today's 12.
    m = n.company_metrics(_hist([12.0] * 6, shares=[100, 120, 120, 144, 144, 144]))
    assert m["norm_earnings"] == pytest.approx((12 / 100 + 12 / 120 * 2 + 12 / 144 * 3) / 6 * 144)
    assert m["norm_earnings"] == pytest.approx(13.68)
    assert m["share_cagr"] == pytest.approx(1.44 ** (1 / 5) - 1)


def test_min_years_and_window():
    assert np.isnan(n.company_metrics(_hist([10.0] * 4))["norm_earnings"])
    m = n.company_metrics(_hist([1000.0] * 2 + [10.0] * 10))            # oldest 2 outside window
    assert m["norm_earnings"] == 10 and m["years_history"] == 10


def test_margin_position_trough_vs_peak():
    h = _hist([1.0] * 10, operating_income=[10.0, 20, 30, 5, -5, 15, 25, 35, 12, 8])
    assert n.company_metrics(h, current_margin=-0.20)["margin_position"] == 0
    assert n.company_metrics(h, current_margin=0.50)["margin_position"] == 1
    assert n.company_metrics(h, current_margin=0.05)["margin_position"] == pytest.approx(0.15)
    assert n.company_metrics(h)["margin_position"] == pytest.approx(0.25)    # latest FY (8%)
    assert n.company_metrics(h)["median_margin"] == pytest.approx(0.135)


def test_ebit_fallback_and_financials():
    h = _hist([1.0] * 5, pretax_income=[8.0] * 5, interest_expense=[2.0] * 5)
    assert n.company_metrics(h)["median_margin"] == pytest.approx(0.10)         # 8 + 2
    bank = n.company_metrics(h.assign(operating_cash_flow=5.0, capex=1.0), special="bank")
    assert bank["median_margin"] == pytest.approx(0.08)                         # no add-back
    assert np.isnan(bank["norm_fcf"])


def test_empty_columns_and_absurd_margin_year():
    one = pd.DataFrame({"fy_end": pd.to_datetime(["2025-12-31"])})
    assert n.company_metrics(one)["years_history"] == 0
    h = _hist([1.0] * 6, rev=[1.0, 100, 100, 100, 100, 100],
              operating_income=[-500.0, 10, 10, 10, 10, 10])
    assert n.company_metrics(h)["margin_vol"] < 0.5           # clipped at -100%


def _screen(rows):
    d = {"special": None, "sector": "S", "ebit": 10.0, "revenue": 100.0, "market_cap": 500.0,
         "enterprise_value": 600.0, "industry_group": "Software & IT Services"}
    return pd.DataFrame([{**d, **r} for r in rows])


def _long(cik, years=6, ni=10.0, oi=10.0):
    ends = pd.date_range("2020-12-31", periods=years, freq="YE")
    return pd.DataFrame([{"cik": cik, "fy_end": e, "concept": c, "val": v}
                         for e in ends for c, v in
                         (("net_income", ni), ("revenue", 100.0), ("operating_income", oi))])


def test_add_normalized_ratios_share_classes_and_no_history(tmp_path):
    s = load_settings(data_dir=tmp_path)
    df = _screen([{"ticker": "A", "cik": 1}, {"ticker": "A2", "cik": 1, "market_cap": 1000.0},
                  {"ticker": "NEW", "cik": 2}, {"ticker": "LOSS", "cik": 3}])
    hist = pd.concat([_long(1), _long(3, ni=-5.0)])
    out = n.add_normalized(df, hist, s).set_index("ticker")
    assert out.loc["A", "pe10"] == 50 and out.loc["A2", "pe10"] == 100      # same CIK, own cap
    assert out.loc["A", "ev_midcycle_ebit"] == 60                            # 600 / (10% x 100)
    assert np.isnan(out.loc["NEW", "pe10"]) and np.isnan(out.loc["LOSS", "pe10"])
    empty = n.add_normalized(df, None, s)
    assert empty["pe10"].isna().all() and len(empty) == 4


def test_cyclical_flag_and_industry_position(tmp_path):
    s = load_settings(data_dir=tmp_path)
    rows = [{"ticker": f"H{i}", "cik": i, "industry_group": "Homebuilders"} for i in range(8)]
    rows += [{"ticker": "SW", "cik": 100}, {"ticker": "SWV", "cik": 101}, {"ticker": "BIO", "cik": 102}]
    hist = [_long(i) for i in [*range(8), 100]]
    wavy = _long(101)
    wavy.loc[wavy["concept"] == "operating_income", "val"] = [2.0, 30.0, 4.0, 28.0, 3.0, 25.0]
    burn = _long(102, ni=-50.0)
    burn.loc[burn["concept"] == "operating_income", "val"] = [-90.0, -10.0, -80.0, -5.0, -70.0, -20.0]
    out = n.add_normalized(_screen(rows), pd.concat([*hist, wavy, burn]), s).set_index("ticker")
    assert out.loc["H0", "cyclical"]                     # listed industry
    assert out.loc["SWV", "cyclical"]                    # volatile but profitable
    assert not out.loc["SW", "cyclical"]
    assert not out.loc["BIO", "cyclical"]                # volatile because it burns cash
    assert out.loc["H0", "industry_cycle_position"] == 0.5
    assert np.isnan(out.loc["SW", "industry_cycle_position"])   # under min_peers


def test_volatile_bank_margins_do_not_make_it_cyclical(tmp_path):
    s = load_settings(data_dir=tmp_path)
    wavy = _long(1)
    wavy.loc[wavy["concept"] == "operating_income", "val"] = [2.0, 30.0, 4.0, 28.0, 3.0, 25.0]
    wavy.loc[wavy["concept"] == "net_income", "val"] = [2.0, 30.0, 4.0, 28.0, 3.0, 25.0]
    out = n.add_normalized(_screen([{"ticker": "B", "cik": 1, "special": "bank",
                                     "industry_group": "Banks"}]), wavy, s)
    assert not out["cyclical"].iloc[0]

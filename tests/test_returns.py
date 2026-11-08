"""Phase 10d — backtest returns on hand-built panels and prices. No network."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from value_screener.backtest import returns as rt
from value_screener.config import Settings

S = Settings()
Q1, Q2 = dt.date(2020, 3, 31), dt.date(2020, 6, 30)
DAYS = pd.bdate_range("2020-03-02", "2020-07-31")


def _panel(rows):
    df = pd.DataFrame(rows, columns=["ticker", "market_cap", "pe", "deep_value", "sector"])
    return df.assign(stale=False, cyclical_trough=False, turning=True)


def _prices(**paths):
    """Each ticker: price 100 until Q1's next close, then a straight line to the given end."""
    adj = pd.DataFrame(index=DAYS)
    for t, end in {"SPY": 110, **paths}.items():
        adj[t] = np.where(DAYS <= pd.Timestamp("2020-04-01"), 100.0,
                          np.linspace(100, end, len(DAYS))) if end else np.nan
    return adj


def _volume(adj, amount=5e6):
    return pd.DataFrame(amount, index=adj.index, columns=adj.columns)


def test_costs_by_size_and_unknown_pays_most():
    c = rt.cost_bps(pd.Series([20e9, 5e9, 1e9, 100e6, np.nan]), S)
    assert c.tolist() == [10, 30, 75, 150, 150]


def test_a_quarter_buys_at_the_next_close_and_pays_to_get_in():
    adj = _prices(AAA=120, BBB=100)
    panels = {Q1: _panel([("AAA", 20e9, 10, True, "Energy"), ("BBB", 1e9, 10, True, "Energy")])}
    per, nav = rt.simulate(rt.Variant("x", "deep_value"), S, panels, adj, _volume(adj), Q1)
    row = per.iloc[0]
    assert row["start"] == pd.Timestamp("2020-04-01") and row["stocks"] == 2
    entry, exit_ = adj.loc["2020-04-01"], adj.loc[DAYS[-2]]    # the cache's last bar is skipped
    assert row["gross"] == pytest.approx(((exit_ / entry - 1)[["AAA", "BBB"]]).mean())
    assert row["cost"] == pytest.approx(0.5 * 10e-4 + 0.5 * 75e-4)
    assert row["net"] == pytest.approx((1 - row["cost"]) * (1 + row["gross"]) - 1)
    assert nav.iloc[-1] == pytest.approx(1 + row["net"])


def test_rebalancing_pays_only_for_what_changes():
    adj = _prices(AAA=120, BBB=100, CCC=100)
    p1 = _panel([("AAA", 20e9, 10, True, "Energy"), ("BBB", 20e9, 10, True, "Energy")])
    p2 = _panel([("AAA", 20e9, 10, True, "Energy"), ("BBB", 20e9, 10, False, "Energy"), ("CCC", 20e9, 10, True, "Energy")])
    per, _ = rt.simulate(rt.Variant("x", "deep_value"), S, {Q1: p1, Q2: p2}, adj, _volume(adj), Q1)
    q2 = per.iloc[1]
    a, b = adj.loc[q2["start"], "AAA"] / 100, 1.0           # growth since entry
    held_a, held_b = a / (a + b), b / (a + b)
    traded = abs(0.5 - held_a) + held_b + 0.5               # trim AAA, sell BBB, buy CCC
    assert q2["turnover"] == pytest.approx(traded / 2)
    assert q2["cost"] == pytest.approx(traded * 10e-4)


def test_thin_stocks_cant_be_bought_and_the_baseline_is_the_cheapest_tenth():
    adj = _prices(**{f"T{i}": 100 for i in range(20)})
    vol = _volume(adj)
    vol["T0"] = 1e5
    p = _panel([(f"T{i}", 1e9, 5 + i, True, "Energy") for i in range(20)])
    got = rt.picks(p, rt.Variant("x", "deep_value"), S, vol.iloc[-1])
    assert "T0" not in got and len(got) == 19
    base = rt.picks(p, rt.Variant(rt.BASELINE, baseline=True), S, vol.iloc[-1])
    assert list(base) == ["T1", "T2"]                         # 10% of 19 eligible, cheapest P/E


def test_a_longer_hold_averages_the_cohorts():
    adj = _prices(AAA=100, BBB=100)
    p1 = _panel([("AAA", 20e9, 10, True, "Energy"), ("BBB", 20e9, 10, False, "Energy")])
    p2 = _panel([("AAA", 20e9, 10, False, "Energy"), ("BBB", 20e9, 10, True, "Energy")])
    per, _ = rt.simulate(rt.Variant("x", "deep_value", hold=2), S, {Q1: p1, Q2: p2}, adj, _volume(adj), Q1)
    assert per["stocks"].tolist() == [1, 2]
    assert per.iloc[1]["turnover"] == pytest.approx(0.5)


def test_a_stock_that_stops_trading_holds_its_last_price():
    adj = _prices(AAA=120)
    adj.loc[adj.index > pd.Timestamp("2020-05-15"), "AAA"] = np.nan
    adj = adj.ffill()
    per, _ = rt.simulate(rt.Variant("x", "deep_value"), S, {Q1: _panel([("AAA", 20e9, 10, True, "Energy")])},
                         adj, _volume(adj), Q1)
    assert per.iloc[0]["gross"] == pytest.approx(adj.loc["2020-05-15", "AAA"] / 100 - 1)


def test_a_sector_left_out():
    adj = _prices(AAA=100, BBB=100)
    p = _panel([("AAA", 20e9, 10, True, "Energy"), ("BBB", 20e9, 10, True, "Financials")])
    assert list(rt.picks(p, rt.Variant("x", "deep_value", drop_sectors=("Financials",)), S, _volume(adj).iloc[-1])) == ["AAA"]


def test_block_bootstrap_t():
    rng = np.random.default_rng(1)
    assert rt.block_t(0.02 + rng.normal(0, 0.01, 40), 4) > 5
    assert abs(rt.block_t(rng.normal(0, 0.01, 40), 4)) < 3
    assert np.isnan(rt.block_t(np.array([0.01]), 4))


def test_yearly_and_annualized():
    per = pd.DataFrame({"date": [dt.date(2019, 12, 31), dt.date(2020, 3, 31)],
                        "start": pd.to_datetime(["2020-01-01", "2020-04-01"]),
                        "stop": pd.to_datetime(["2020-04-01", "2021-01-01"]), "net": [0.10, -0.10]})
    assert rt.yearly(per).to_dict() == {2020: pytest.approx(1.1 * 0.9 - 1)}
    assert rt.annualized(per) == pytest.approx((1.1 * 0.9) ** (365.25 / 366) - 1)


def _main(spy, ew, cheap):
    return {"excess_SPY": spy, f"excess_{rt.UNIVERSE}": ew, f"excess_{rt.BASELINE}": cheap}


def test_verdict():
    large = _main(0.01, 0.01, 0)
    assert rt.verdict(_main(0.05, 0.04, 0.02), large, [0.01, -0.01, 0.02], 0.01, S)[0] == "pass"
    assert rt.verdict(_main(0.05, 0.02, 0.02), large, [0.01, -0.01, 0.02], 0.01, S)[0] == "inconclusive"
    outcome, checks = rt.verdict(_main(0.05, 0.04, -0.01), large, [0.01, 0.01, 0.02], 0.01, S)
    assert outcome == "fail" and not checks["beats cheapest 10%"]
    assert rt.verdict(_main(0.05, 0.04, 0.02), large, [0.01, -0.01, -0.02], 0.01, S)[0] == "fail"
    assert rt.verdict(_main(0.05, 0.04, 0.02), large, [0.01, 0.01, 0.02], 0.05, S)[0] == "fail"   # drag
    assert rt.verdict(_main(0.05, 0.04, 0.02), _main(-0.01, 0.01, 0), [0.01, 0.01, 0.02], 0.01, S)[0] == "fail"


def test_a_spliced_price_series_is_left_out_of_that_quarter():
    adj = _prices(AAA=110, BBB=100)
    adj.loc[adj.index >= pd.Timestamp("2020-05-15"), "BBB"] *= 300        # old shares spliced to new
    p = _panel([("AAA", 20e9, 10, True, "Energy"), ("BBB", 20e9, 10, True, "Energy")])
    per, _ = rt.simulate(rt.Variant("x", "deep_value"), S, {Q1: p}, adj, _volume(adj), Q1)
    assert per.iloc[0]["stocks"] == 1 and per.iloc[0]["spliced"] == 1
    assert per.iloc[0]["gross"] == pytest.approx(adj.loc[DAYS[-2], "AAA"] / 100 - 1)


def test_prices_stop_at_the_stocks_last_bar(tmp_path):
    s = Settings(data_dir=tmp_path)
    d = tmp_path / "cache" / "price_history"
    d.mkdir(parents=True)
    def frame(days):
        return pd.DataFrame({"close": 10.0, "adj_close": 10.0, "volume": 1e6, "split": 0.0}, index=days)
    frame(DAYS).to_parquet(d / "SPY.parquet")
    frame(DAYS[:-3]).to_parquet(d / "AAA.parquet")
    adj, _ = rt.load_prices(s, ["AAA", "SPY"])
    assert adj.index.max() == DAYS[-4]
    assert rt.market_days(adj)[-1] == DAYS[-5]


def test_an_adjusted_close_below_zero_is_left_out():
    adj = _prices(AAA=110, BBB=100)
    adj.loc[adj.index >= pd.Timestamp("2020-06-15"), "BBB"] = -5.0
    p = _panel([("AAA", 20e9, 10, True, "Energy"), ("BBB", 20e9, 10, True, "Energy")])
    per, _ = rt.simulate(rt.Variant("x", "deep_value"), S, {Q1: p}, adj, _volume(adj), Q1)
    assert per.iloc[0]["stocks"] == 1 and per.iloc[0]["spliced"] == 1

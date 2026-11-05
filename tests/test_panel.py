"""Phase 10b — point-in-time replay on hand-built filings and prices. Fake source, no network."""

import datetime as dt
import gzip
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from value_screener import cache, fundamentals as f
from value_screener.backtest import panel, price_history as ph
from value_screener.config import CONFIG_DIR, Settings

TAGS = f.load_tags(CONFIG_DIR)
CIK = 1234


def r(start, end, val, filed, form="10-K", accn="a"):
    return [start, end, val, filed, form, accn]


def _slim(**tags):
    return {"entity": "X", "taxonomies": ["us-gaap"], "tags": tags}


def _prices(start="2019-01-01", end="2019-12-31", close=10.0, splits=None):
    idx = pd.bdate_range(start, end)
    df = pd.DataFrame({"close": close, "adj_close": close, "volume": 1000.0, "split": 0.0}, index=idx)
    for day, ratio in (splits or {}).items():
        df.loc[pd.Timestamp(day), "split"] = ratio
    return df


def test_a_figure_filed_a_day_later_is_not_visible():
    slim = _slim(Revenues=[r("2018-01-01", "2018-12-31", 100, "2019-02-15"),
                           r("2019-01-01", "2019-12-31", 200, "2020-02-15")])
    day_before = f.extract(panel.as_of(slim, dt.date(2020, 2, 14)), TAGS, today=dt.date(2020, 2, 14))
    on_the_day = f.extract(panel.as_of(slim, dt.date(2020, 2, 15)), TAGS, today=dt.date(2020, 2, 15))
    assert day_before["revenue"] == 100 and on_the_day["revenue"] == 200


def test_a_restatement_is_undone_before_its_filing():
    slim = _slim(Revenues=[r("2018-01-01", "2018-12-31", 100, "2019-02-15"),
                           r("2018-01-01", "2018-12-31", 90, "2019-08-01", form="10-K/A")])
    assert f.extract(panel.as_of(slim, dt.date(2019, 3, 1)), TAGS, today=dt.date(2019, 3, 1))["revenue"] == 100
    assert f.extract(panel.as_of(slim, dt.date(2019, 9, 1)), TAGS, today=dt.date(2019, 9, 1))["revenue"] == 90


def test_real_filings_micron_mid_2019_pairs_trailing_revenue_with_trailing_earnings():
    # Tags changed that year. Revenue once read $30.4B, from FY2018, beside falling earnings.
    with gzip.open(Path(__file__).parent / "fixtures" / "facts" / "mu.json.gz", "rt", encoding="utf-8") as fh:
        slim = json.load(fh)
    day = dt.date(2019, 6, 28)
    row = f.extract(panel.as_of(slim, day), TAGS, today=day)
    assert row["revenue"] == 26_976_000_000 and row["revenue_end"] == pd.Timestamp("2019-05-30")
    assert row["net_income_end"] == row["revenue_end"]


def test_as_of_drops_rows_with_no_filing_date_and_empty_tags():
    slim = _slim(Revenues=[r("2018-01-01", "2018-12-31", 100, None)], Assets=[r(None, "2020-12-31", 5, "2021-01-01")])
    assert panel.as_of(slim, dt.date(2020, 1, 1))["tags"] == {}


def test_replay_of_a_company_with_nothing_filed_yet_is_none(tmp_path):
    path = tmp_path / "c.json.gz"
    cache.write_json(path, _slim(Revenues=[r("2018-01-01", "2018-12-31", 100, "2019-02-15")]))
    assert panel._replay_company([path], TAGS, dt.date(2019, 1, 1), 15, 15) is None
    row, _ = panel._replay_company([path], TAGS, dt.date(2019, 3, 1), 15, 15)
    assert row["revenue"] == 100 and not row["stale"]


def test_predecessor_filings_are_cut_too(tmp_path):
    old, new = tmp_path / "old.json.gz", tmp_path / "new.json.gz"
    cache.write_json(old, _slim(Revenues=[r("2017-01-01", "2017-12-31", 70, "2018-02-15")]))
    cache.write_json(new, _slim(Revenues=[r("2018-01-01", "2018-12-31", 100, "2019-02-15")]))
    row, _ = panel._replay_company([old, new], TAGS, dt.date(2018, 6, 1), 15, 15)
    assert row["revenue"] == 70


def test_market_cap_uses_the_printed_close_and_shares_known_then():
    # 4-for-1 in June. Yahoo's closes are adjusted to today: $25 now was printed as $100 in May.
    df = _prices(close=25.0, splits={"2019-06-10": 4.0})
    frames = {"AAA": df}
    fund = pd.DataFrame({"ticker": ["AAA"], "shares_outstanding": [1_000.0],
                         "shares_outstanding_end": [pd.Timestamp("2019-04-30")]})
    s = Settings()
    may = dt.date(2019, 5, 15)
    before = panel.past_market_caps(panel.past_closes(frames, may), fund, frames, s, may)
    assert before.loc[0, "price"] == 100 and before.loc[0, "market_cap"] == 100_000
    # After the split the printed price is $25, and the 1,000 shares known are now 4,000.
    july = dt.date(2019, 7, 15)
    after = panel.past_market_caps(panel.past_closes(frames, july), fund, frames, s, july)
    assert after.loc[0, "price"] == 25 and after.loc[0, "market_cap"] == 100_000


def test_share_count_after_the_split_is_not_multiplied_again():
    df = _prices(close=25.0, splits={"2019-06-10": 4.0})
    fund = pd.DataFrame({"ticker": ["AAA"], "shares_outstanding": [4_000.0],
                         "shares_outstanding_end": [pd.Timestamp("2019-07-01")]})
    july = dt.date(2019, 7, 15)
    caps = panel.past_market_caps(panel.past_closes({"AAA": df}, july), fund, {"AAA": df}, Settings(), july)
    assert caps.loc[0, "market_cap"] == 100_000


def test_a_spin_off_restates_the_price_but_not_the_share_count():
    # Yahoo lists Corteva's October 2026 spin-off as a 6.665 "split".
    df = _prices(close=12.0, splits={"2019-06-10": 6.665})
    fund = pd.DataFrame({"ticker": ["AAA"], "shares_outstanding": [1_000.0],
                         "shares_outstanding_end": [pd.Timestamp("2019-04-30")]})
    frames = {"AAA": df}
    before, after = dt.date(2019, 5, 15), dt.date(2019, 7, 15)
    assert panel.past_market_caps(panel.past_closes(frames, before), fund, frames, Settings(), before).loc[0, "market_cap"] \
        == pytest.approx(12.0 * 6.665 * 1_000)
    assert panel.past_market_caps(panel.past_closes(frames, after), fund, frames, Settings(), after).loc[0, "market_cap"] == 12_000


@pytest.mark.parametrize("ratio", [2.0, 1.5, 0.1, 0.4, 1.02])
def test_real_splits_and_stock_dividends_change_the_share_count(ratio):
    splits = pd.Series([ratio], index=[pd.Timestamp("2019-06-10")])
    assert ph.split_factor(splits, pd.Timestamp("2019-01-01"), shares_only=True) == pytest.approx(ratio)


@pytest.mark.parametrize("ratio", [6.665, 1.243, 1.437])
def test_spin_off_ratios_leave_the_share_count_alone(ratio):
    splits = pd.Series([ratio], index=[pd.Timestamp("2019-06-10")])
    assert ph.split_factor(splits, pd.Timestamp("2019-01-01"), shares_only=True) == 1.0
    assert ph.split_factor(splits, pd.Timestamp("2019-01-01")) == pytest.approx(ratio)


def _fund(**kw):
    end = pd.Timestamp("2019-05-30")
    return pd.DataFrame([{"ticker": "AAA", "shares_outstanding": 100.0, "shares_outstanding_end": pd.Timestamp("2019-04-30"),
                          "net_income": 1_000.0, "net_income_end": end, "eps_diluted": 10.0, "eps_diluted_end": end,
                          "foreign_filer": False, **kw}])


def _cap(fund):
    frames, day = {"AAA": _prices()}, dt.date(2019, 7, 15)
    return panel.past_market_caps(panel.past_closes(frames, day), fund, frames, Settings(), day).loc[0, "market_cap"]


@pytest.mark.parametrize("change", [dict(shares_outstanding=np.nan),                                       # per class
                                    dict(shares_outstanding_end=pd.Timestamp("2017-01-31")),               # stale
                                    dict(shares_outstanding=np.nan, net_income=-1_000.0, eps_diluted=-10.0)])  # a real loss
def test_net_income_over_eps_fills_a_missing_or_stale_share_count(change):
    assert _cap(_fund(**change)) == 1_000      # $10 x 1,000 / 10


@pytest.mark.parametrize("shares", [120.0, 180.0, 1_300.0])
def test_net_income_over_eps_does_not_overrule_a_fresh_count(shares):
    assert _cap(_fund(shares_outstanding=shares)) == shares * 10


@pytest.mark.parametrize("filed", [103_000.0, 97_000_000.0])
def test_a_count_scaled_a_thousand_or_a_million_times_too_big_is_scaled_back(filed):
    scale = 1_000 if filed < 1e6 else 1_000_000
    # Works on a small EPS too: Fortinet's was $0.24.
    assert _cap(_fund(shares_outstanding=filed, net_income=10.0, eps_diluted=0.10)) == pytest.approx(filed / scale * 10)


def test_a_fresh_count_under_half_of_net_income_over_eps_gives_no_cap():
    assert np.isnan(_cap(_fund(shares_outstanding=40.0)))      # may be one class of several


def test_a_split_during_the_trailing_year_makes_net_income_over_eps_unusable():
    # Booking's 25-for-1: trailing EPS mixes both sides, and implied shares come out 14x too few.
    frames, day = {"AAA": _prices(splits={"2019-06-10": 25.0})}, dt.date(2019, 7, 15)
    caps = lambda fund: panel.past_market_caps(panel.past_closes(frames, day), fund, frames, Settings(), day).loc[0, "market_cap"]
    assert caps(_fund(shares_outstanding=2_500.0, shares_outstanding_end=pd.Timestamp("2019-07-01"))) == 25_000
    assert np.isnan(caps(_fund(shares_outstanding=np.nan)))


@pytest.mark.parametrize("change", [dict(eps_diluted=0.10, net_income=10.0), dict(eps_diluted_end=pd.Timestamp("2018-12-31"))])
def test_net_income_over_eps_is_not_trusted_on_small_eps_or_mismatched_periods(change):
    assert np.isnan(_cap(_fund(shares_outstanding=np.nan, **change)))


def test_a_cap_absurdly_large_against_revenue_and_equity_is_blanked():
    assert np.isnan(_cap(_fund(revenue=0.5, equity=0.5)))      # cap $1,000
    assert _cap(_fund(revenue=2.0, equity=0.5)) == 1_000


def test_a_foreign_filer_gets_no_market_cap():
    assert np.isnan(_cap(_fund(foreign_filer=True)))


def test_the_latest_annual_form_decides_who_is_foreign():
    rows = [r("2015-01-01", "2015-12-31", 1, "2016-03-01", form="20-F/A"), r("2016-01-01", "2016-12-31", 1, "2017-03-01", form="10-K"),
            r("2017-01-01", "2017-03-31", 1, "2017-05-01", form="10-Q")]
    slim = _slim(Revenues=rows)
    assert panel.is_foreign_filer(panel.as_of(slim, dt.date(2016, 6, 1)))
    assert not panel.is_foreign_filer(slim)
    assert not panel.is_foreign_filer(_slim(Revenues=rows[2:]))


def test_a_reverse_split_divides():
    df = _prices(close=50.0, splits={"2019-06-10": 0.1})
    assert ph.unadjusted(df, "2019-05-15") == pytest.approx(5.0)
    assert ph.unadjusted(df, "2019-07-15") == 50


def test_an_old_share_count_gives_no_market_cap():
    df = _prices()
    fund = pd.DataFrame({"ticker": ["AAA"], "shares_outstanding": [1_000.0],
                         "shares_outstanding_end": [pd.Timestamp("2017-01-31")]})
    day = dt.date(2019, 7, 15)
    caps = panel.past_market_caps(panel.past_closes({"AAA": df}, day), fund, {"AAA": df}, Settings(), day)
    assert np.isnan(caps.loc[0, "market_cap"])


def test_no_close_near_the_date_means_not_in_the_universe():
    frames = {"NEW": _prices(start="2019-06-03"), "GONE": _prices(end="2019-03-01"), "OK": _prices()}
    got = panel.past_closes(frames, dt.date(2019, 5, 1))
    assert got["ticker"].tolist() == ["OK"]


def test_a_weekend_date_uses_the_friday_close():
    got = panel.past_closes({"OK": _prices()}, dt.date(2019, 6, 30))     # a Sunday
    assert got.loc[0, "price_date"] == dt.date(2019, 6, 28)


def test_past_trend_sees_nothing_after_the_date():
    df = _prices(close=10.0)
    df.loc["2019-08-01":, "close"] = 1000.0
    out = panel.past_trend({"AAA": df}, dt.date(2019, 7, 15))
    assert out.loc[0, "off_52w_high"] == 0 and out.loc[0, "trend_date"] == dt.date(2019, 7, 15)


def test_past_trend_sees_a_spin_off_only_once_it_has_happened():
    df = _prices(splits={"2019-06-10": 6.665})
    assert pd.isna(panel.past_trend({"AAA": df}, dt.date(2019, 5, 15)).loc[0, "spin_off_date"])
    assert panel.past_trend({"AAA": df}, dt.date(2019, 7, 15)).loc[0, "spin_off_date"] == dt.date(2019, 6, 10)


class _Source:
    def __init__(self, frames, fail_first=()):
        self.frames, self.requests, self.fail = frames, [], set(fail_first)

    def batch(self, tickers, start):
        self.requests.append(list(tickers))
        out = {t: self.frames[t] for t in tickers if t in self.frames and t not in self.fail}
        self.fail = set()
        return out


def _settings(tmp_path):
    return Settings(data_dir=tmp_path, trend_retry_pause=0, price_pause=0, price_chunk=2)


def test_price_history_is_cached_and_failures_retried(tmp_path):
    s = _settings(tmp_path)
    source = _Source({"AAA": _prices(), "BBB": _prices()}, fail_first=["AAA"])
    assert ph.fetch(s, ["AAA", "BBB", "NOPE"], dt.date(2019, 1, 1), source) == 2
    assert set(ph.load(s, ["AAA", "BBB", "NOPE"])) == {"AAA", "BBB"}
    source.requests.clear()
    ph.fetch(s, ["AAA", "BBB"], dt.date(2019, 1, 1), source)
    assert source.requests == []
    assert "NOPE" in (tmp_path / "errors.csv").read_text(encoding="utf-8")


def test_from_download_reads_a_yfinance_frame():
    idx = pd.bdate_range("2019-01-01", periods=2)
    fields = {"Close": [10.0, 11.0], "Adj Close": [9.0, 10.0], "Volume": [5.0, 6.0], "Stock Splits": [0.0, 2.0]}
    data = pd.concat({k: pd.DataFrame({"AAA": v, "BBB": [np.nan, np.nan]}, index=idx) for k, v in fields.items()}, axis=1)
    out = ph.from_download(data, ["AAA", "BBB"])
    assert list(out) == ["AAA"] and out["AAA"]["split"].tolist() == [0.0, 2.0]


def test_quarter_ends():
    assert panel.quarter_ends("2019-01-01", dt.date(2019, 10, 1)) == [
        dt.date(2019, 3, 31), dt.date(2019, 6, 30), dt.date(2019, 9, 30)]


def _screen(rows):
    base = dict(label="Fair", deep_value=False, cyclical_trough=False, **dict.fromkeys(panel.GATE_FIELDS, 1.0))
    return pd.DataFrame([{**base, **row} for row in rows])


def test_compare_counts_what_differs():
    live = _screen([dict(ticker="A", market_cap=100.0, deep_value=True), dict(ticker="B", market_cap=100.0),
                    dict(ticker="C", market_cap=100.0), dict(ticker="D", market_cap=100.0)])
    replay = _screen([dict(ticker="A", market_cap=101.0, deep_value=True), dict(ticker="B", market_cap=300.0, label="Overvalued"),
                      dict(ticker="C", market_cap=np.nan, pe=9.0)])
    out = panel.compare(replay, live)
    assert out["common"] == 3 and out["live_not_replayed"] == ["D"]
    assert (out["caps_compared"], out["caps_close"], out["caps_lost"]) == (2, 1, 1)
    assert out["caps_wrong"].index.tolist() == ["B"]
    assert out["label_match"] == pytest.approx(2 / 3) and out["fields"]["pe"] == pytest.approx(2 / 3)
    assert out["deep_value"]["both"] == 1

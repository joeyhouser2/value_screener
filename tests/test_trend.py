"""Phase 9d — price trend from synthetic closes. Fake source, no network."""

import datetime as dt
import threading
import time

import numpy as np
import pandas as pd
import pytest
import yfinance

from value_screener import errors, trend, valuation
from value_screener.config import Settings

TODAY = dt.date(2026, 10, 30)


def _closes(values, end="2026-10-30"):
    return pd.Series(values, index=pd.bdate_range(end=end, periods=len(values)), dtype=float)


def test_off_high_and_200_day_average():
    s = _closes([100.0] * 199 + [120.0, 90.0])
    m = trend.series_metrics(s)
    assert m["off_52w_high"] == pytest.approx(90 / 120 - 1)
    assert m["price_vs_200d"] == pytest.approx(90 / ((100 * 198 + 120 + 90) / 200) - 1)


def test_a_high_older_than_a_year_does_not_count():
    idx = pd.bdate_range(end="2026-10-30", periods=300)
    s = pd.Series(100.0, index=idx)
    s.iloc[0] = 500.0                                     # about 14 months ago
    assert trend.series_metrics(s)["off_52w_high"] == 0


def test_momentum_runs_from_12_months_to_1_month_ago():
    idx = pd.bdate_range(end="2026-10-30", periods=300)
    s = pd.Series(100.0, index=idx)
    s[idx >= "2025-10-30"] = 50.0
    s[idx >= "2026-10-01"] = 200.0                        # the latest month is skipped
    s[idx >= "2026-09-30"] = 200.0
    m = trend.series_metrics(s)
    assert m["momentum_12_1"] == pytest.approx(200 / 50 - 1)


def test_momentum_skips_the_latest_month():
    idx = pd.bdate_range(end="2026-10-30", periods=300)
    s = pd.Series(100.0, index=idx)
    s[idx > "2026-09-30"] = 1000.0
    assert trend.series_metrics(s)["momentum_12_1"] == 0


def test_momentum_counts_dividends_but_levels_do_not():
    idx = pd.bdate_range(end="2026-10-30", periods=300)
    close = pd.Series(100.0, index=idx)
    adj = pd.Series(100.0, index=idx)
    adj[idx < "2026-01-15"] = 90.0                        # a dividend adjusts earlier prices down
    m = trend.series_metrics(close, adj)
    assert m["momentum_12_1"] == pytest.approx(100 / 90 - 1)
    assert m["off_52w_high"] == 0


def test_negative_adjusted_prices_give_no_momentum():
    idx = pd.bdate_range(end="2026-10-30", periods=300)
    close, adj = pd.Series(100.0, index=idx), pd.Series(100.0, index=idx)
    adj[idx < "2026-01-15"] = -2.0
    m = trend.series_metrics(close, adj)
    assert np.isnan(m["momentum_12_1"]) and m["off_52w_high"] == 0


def test_young_listing_gets_what_its_history_supports():
    m = trend.series_metrics(_closes([10.0, 20.0, 15.0]))
    assert m["off_52w_high"] == pytest.approx(-0.25)
    assert np.isnan(m["price_vs_200d"]) and np.isnan(m["momentum_12_1"])


def _download(**tickers):
    idx = pd.bdate_range(end="2026-10-30", periods=len(next(iter(tickers.values()))))
    close = pd.DataFrame(tickers, index=idx, dtype=float)
    return pd.concat({"Adj Close": close, "Close": close}, axis=1)


def test_download_frame_parsed_per_ticker():
    out = trend.from_download(_download(AAA=[1.0, 2.0], BBB=[4.0, 2.0]), ["AAA", "BBB"], TODAY).set_index("ticker")
    assert out.loc["AAA", "off_52w_high"] == 0 and out.loc["BBB", "off_52w_high"] == -0.5
    assert out.loc["AAA", "trend_date"] == TODAY


def test_single_ticker_download_and_empty_download():
    data = pd.DataFrame({"Close": [1.0, 2.0]}, index=pd.bdate_range(end="2026-10-30", periods=2))
    assert trend.from_download(data, ["AAA"], TODAY)["ticker"].tolist() == ["AAA"]
    assert trend.from_download(pd.DataFrame(), ["AAA"], TODAY).empty


def test_all_missing_and_delisted_tickers_omitted():
    data = _download(AAA=[1.0, 2.0], BBB=[np.nan, np.nan])
    assert trend.from_download(data, ["AAA", "BBB"], TODAY)["ticker"].tolist() == ["AAA"]
    later = dt.date(2026, 12, 1)
    assert trend.from_download(data, ["AAA"], later).empty


class _Fake:
    def __init__(self):
        self.requests = []

    def trend(self, tickers):
        self.requests.append(list(tickers))
        return pd.DataFrame([{"ticker": t, "off_52w_high": -0.1, "momentum_12_1": 0.2,
                              "price_vs_200d": 0.05, "trend_date": TODAY} for t in tickers])


def test_daily_cache_only_fetches_new_tickers(tmp_path):
    s, src = Settings(data_dir=tmp_path), _Fake()
    trend.cached_trend(s, ["AAA"], src, today=TODAY)
    out = trend.cached_trend(s, ["AAA", "BBB"], src, today=TODAY)
    assert src.requests == [["AAA"], ["BBB"]] and set(out["ticker"]) == {"AAA", "BBB"}


def test_first_run_of_the_day_is_numeric(tmp_path):
    out = trend.cached_trend(Settings(data_dir=tmp_path), ["AAA"], _Fake(), today=TODAY)
    assert all(out[m].dtype == float for m in trend.METRICS)


class _Broken:
    def trend(self, tickers):
        raise RuntimeError("Yahoo is down")


def test_failure_is_logged_and_the_run_continues(tmp_path):
    s = Settings(data_dir=tmp_path)
    out = trend.refresh(s, pd.DataFrame({"ticker": ["AAA"]}), _Broken(), verbose=False)
    assert out.empty and trend.path(s).exists()
    assert "Yahoo is down" in errors.errors_path(tmp_path).read_text(encoding="utf-8")


class _Partial(_Fake):
    def trend(self, tickers):
        return super().trend([t for t in tickers if t != "BAD"])


def test_tickers_without_trend_are_logged(tmp_path):
    s = Settings(data_dir=tmp_path)
    out = trend.refresh(s, pd.DataFrame({"ticker": ["AAA", "BAD"]}), _Partial(), verbose=False)
    assert out["ticker"].tolist() == ["AAA"]
    log = errors.errors_path(tmp_path).read_text(encoding="utf-8")
    assert "trend,BAD,no recent price data" in log and ",AAA," not in log


def _recent(*tickers):
    """A download frame ending today, which is what YahooTrend measures staleness against."""
    close = pd.DataFrame({t: [1.0, 2.0] for t in tickers}, index=pd.bdate_range(end=dt.date.today(), periods=2))
    return pd.concat({"Adj Close": close, "Close": close}, axis=1)


def _yahoo(monkeypatch, responses):
    """yfinance.download returning each of ``responses`` in turn; records the tickers asked for."""
    asked, queue = [], iter(responses)

    def download(batch, **kwargs):
        asked.append(list(batch))
        out = next(queue)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(yfinance, "download", download)
    return asked


def test_failed_tickers_are_retried(monkeypatch):
    asked = _yahoo(monkeypatch, [_recent("AAA"), _recent("BBB")])
    out = trend.YahooTrend(chunk=2, pause=0, retries=2, retry_pause=0).trend(["AAA", "BBB"])
    assert asked == [["AAA", "BBB"], ["BBB"]] and set(out["ticker"]) == {"AAA", "BBB"}


def test_retries_are_limited(monkeypatch):
    asked = _yahoo(monkeypatch, [pd.DataFrame()] * 3)
    out = trend.YahooTrend(chunk=2, pause=0, retries=2, retry_pause=0).trend(["AAA"])
    assert len(asked) == 3 and out.empty


def test_a_batch_that_raises_does_not_lose_the_others(monkeypatch):
    _yahoo(monkeypatch, [RuntimeError("boom"), _recent("BBB"), _recent("AAA")])
    out = trend.YahooTrend(chunk=1, pause=0, retries=1, retry_pause=0).trend(["AAA", "BBB"])
    assert set(out["ticker"]) == {"AAA", "BBB"}


def test_a_stalled_batch_is_abandoned(monkeypatch):
    release = threading.Event()

    def stall(batch, **kwargs):
        if batch == ["AAA"]:
            release.wait(30)
            return pd.DataFrame()
        return _recent("BBB")

    monkeypatch.setattr(yfinance, "download", stall)
    began = time.monotonic()
    out = trend.YahooTrend(chunk=1, pause=0, retries=0, batch_timeout=0.1).trend(["AAA", "BBB"])
    release.set()
    assert time.monotonic() - began < 5 and out["ticker"].tolist() == ["BBB"]


def _screen(trend_df):
    uni = pd.DataFrame({"ticker": ["AAA", "BBB"], "name": ["A", "B"], "exchange": "NYSE", "cik": [1.0, 2.0]})
    ind = pd.DataFrame({"ticker": ["AAA", "BBB"], "industry_group": "Software & IT Services",
                        "sector": "Technology", "special": None})
    fund = pd.DataFrame({"ticker": ["AAA", "BBB"], "revenue": [10.0, 20.0], "net_income": [1.0, 2.0],
                         "stale": False})
    px = pd.DataFrame({"ticker": ["AAA", "BBB"], "price": [1.0, 2.0],
                       "price_date": TODAY, "market_cap": [100.0, 200.0]})
    return valuation.build_screen(uni, ind, fund, px, Settings(), trend_df=trend_df).set_index("ticker")


def test_trend_reaches_the_screen_without_changing_labels():
    t = pd.DataFrame({"ticker": ["AAA"], "off_52w_high": [-0.3], "momentum_12_1": [0.1],
                      "price_vs_200d": [0.2], "trend_date": [TODAY]})
    with_t, without = _screen(t), _screen(None)
    assert with_t.loc["AAA", "off_52w_high"] == -0.3 and np.isnan(with_t.loc["BBB", "off_52w_high"])
    assert np.isnan(without.loc["AAA", "off_52w_high"])
    assert with_t["label"].to_dict() == without["label"].to_dict()
    assert _screen(pd.DataFrame(columns=trend.COLUMNS))["label"].to_dict() == without["label"].to_dict()

"""Phase 4 — price interface, daily cache, market cap. Fake source, no network."""

import datetime as dt

import numpy as np
import pytest
import pandas as pd

from value_screener import prices
from value_screener.config import Settings


class FakeSource:
    def __init__(self, table):
        self.table = table
        self.requests: list[list[str]] = []

    def last_close(self, tickers):
        self.requests.append(list(tickers))
        return pd.DataFrame([{"ticker": t, "price": self.table[t], "price_date": dt.date(2026, 10, 30)}
                             for t in tickers if t in self.table])


def test_daily_cache_only_fetches_new_tickers(tmp_path):
    s = Settings(data_dir=tmp_path)
    src = FakeSource({"AAA": 10.0, "BBB": 20.0, "CCC": 30.0})
    day = dt.date(2026, 10, 31)
    prices.cached_last_close(s, ["AAA", "BBB"], src, today=day)
    out = prices.cached_last_close(s, ["AAA", "BBB", "CCC"], src, today=day)
    assert src.requests == [["AAA", "BBB"], ["CCC"]]
    assert set(out["ticker"]) == {"AAA", "BBB", "CCC"}


def test_market_cap_is_price_times_sec_shares():
    closes = pd.DataFrame({"ticker": ["AAA", "BBB"], "price": [10.0, 20.0]})
    fund = pd.DataFrame({"ticker": ["AAA", "BBB"], "shares_outstanding": [1e6, None]})
    out = prices.with_market_cap(closes, fund).set_index("ticker")
    assert out.loc["AAA", "market_cap"] == 1e7
    assert np.isnan(out.loc["BBB", "market_cap"])


def test_last_valid_handles_gaps_and_single_ticker():
    idx = pd.to_datetime(["2026-10-29", "2026-10-30"])
    multi = pd.concat({"Close": pd.DataFrame({"AAA": [1.0, np.nan], "BBB": [2.0, 3.0]}, index=idx)},
                      axis=1)
    out = prices._last_valid(multi, ["AAA", "BBB"]).set_index("ticker")
    assert out.loc["AAA", "price"] == 1.0 and out.loc["BBB", "price"] == 3.0
    single = pd.DataFrame({"Close": [5.0, 6.0]}, index=idx)
    assert prices._last_valid(single, ["ZZZ"])["ticker"].tolist() == ["ZZZ"]


def test_missing_prices_logged_not_fatal(tmp_path):
    s = Settings(data_dir=tmp_path)
    uni = pd.DataFrame({"ticker": ["AAA", "GONE"]})
    fund = pd.DataFrame({"ticker": ["AAA", "GONE"], "shares_outstanding": [100.0, 5.0]})
    out = prices.refresh(s, uni, fund, FakeSource({"AAA": 2.0}), reference_caps=lambda s: {},
                         market_caps=lambda s, t: {},
                         verbose=False)
    assert out["ticker"].tolist() == ["AAA"]
    assert "GONE" in (tmp_path / "errors.csv").read_text()


def test_implausible_or_stale_share_counts_flagged():
    closes = pd.DataFrame({"ticker": ["OK", "STALE", "TINY", "NONE"], "price": 10.0})
    fund = pd.DataFrame({"ticker": ["OK", "STALE", "TINY", "NONE"],
                         "shares_outstanding": [1e6, 1e6, 1e3, None],
                         "shares_outstanding_end": pd.to_datetime(
                             ["2026-09-30", "2011-02-17", "2026-09-30", None]),
                         "revenue": [5e6, 5e6, 5e9, 1e6], "equity": [1e6, 1e6, 1e9, 1e6]})
    out = prices.with_market_cap(closes, fund, today=dt.date(2026, 10, 31)).set_index("ticker")
    assert out["needs_check"].to_dict() == {"OK": False, "STALE": True, "TINY": True, "NONE": True}


def test_flagged_caps_replaced_from_yahoo(tmp_path):
    s = Settings(data_dir=tmp_path)
    uni = pd.DataFrame({"ticker": ["BRK-B", "OK"]})
    fund = pd.DataFrame({"ticker": ["BRK-B", "OK"], "shares_outstanding": [9e5, 1e6],
                         "shares_outstanding_end": pd.to_datetime(["2011-02-17", dt.date.today()]),
                         "revenue": [3e11, 1e6], "equity": [7e11, 1e6]})
    out = prices.refresh(s, uni, fund, FakeSource({"BRK-B": 480.0, "OK": 10.0}),
                         reference_caps=lambda s: {}, market_caps=lambda s, t: {"BRK-B": 1.0e12}, verbose=False).set_index("ticker")
    assert out.loc["BRK-B", "market_cap"] == 1.0e12 and out.loc["BRK-B", "market_cap_src"] == "yahoo"
    assert out.loc["OK", "market_cap"] == 1e7 and out.loc["OK", "market_cap_src"] == "sec_shares"


def test_reference_cap_flags_disagreement_and_replaces_it(tmp_path):
    s = Settings(data_dir=tmp_path)
    uni = pd.DataFrame({"ticker": ["ADR", "OK", "NOFIX"]})
    fund = pd.DataFrame({"ticker": ["ADR", "OK", "NOFIX"], "shares_outstanding": [1e9, 1e6, None],
                         "shares_outstanding_end": pd.to_datetime([dt.date.today()] * 3),
                         "revenue": 1e6, "equity": 1e6})
    out = prices.refresh(s, uni, fund, FakeSource({"ADR": 10.0, "OK": 10.0, "NOFIX": 1.0}),
                         reference_caps=lambda s: {"ADR": 5e8, "OK": 1.1e7},
                         market_caps=lambda s, t: {}, verbose=False).set_index("ticker")
    assert out.loc["ADR", "market_cap"] == 5e8 and out.loc["ADR", "market_cap_src"] == "nasdaq"
    assert out.loc["OK", "market_cap"] == 1e7 and out.loc["OK", "market_cap_src"] == "sec_shares"
    assert np.isnan(out.loc["NOFIX", "market_cap"]) and out.loc["NOFIX", "market_cap_src"] == "untrusted"


def test_parse_nasdaq_screener():
    payload = {"data": {"rows": [
        {"symbol": "BRK/B", "marketCap": "1195732510627.00"},
        {"symbol": "F", "marketCap": "48410950785.00"},
        {"symbol": "ABC^A", "marketCap": "100.00"},
        {"symbol": "NOCAP", "marketCap": ""},
    ]}}
    assert prices.parse_nasdaq_screener(payload) == {"BRK-B": 1195732510627.0, "F": 48410950785.0}


def test_yahoo_fallback_stops_after_repeated_refusals(tmp_path, monkeypatch):
    import yfinance

    calls = []

    class Boom:
        def __init__(self, t):
            calls.append(t)

        def get_info(self):
            raise RuntimeError("HTTP Error 401: Invalid Crumb")

    monkeypatch.setattr(yfinance, "Ticker", Boom)
    out = prices.yahoo_market_caps(Settings(data_dir=tmp_path), [f"T{i}" for i in range(20)],
                                   pause=0, max_failures=3)
    assert out == {} and len(calls) == 3


class _OneClose:
    def last_close(self, tickers):
        return pd.DataFrame({"ticker": tickers, "price": 10.0, "price_date": dt.date(2026, 11, 2)})


def test_first_run_of_the_day_keeps_numeric_market_caps(tmp_path):
    # No close cache yet: an empty frame joins the fresh closes, and values only turn
    # numeric again after a parquet round trip. Later columns divide by market cap.
    s = Settings(data_dir=tmp_path)
    uni = pd.DataFrame({"ticker": ["A", "B"]})
    fund = pd.DataFrame({"ticker": ["A", "B"], "shares_outstanding": [5.0, None]})
    out = prices.refresh(s, uni, fund, _OneClose(), reference_caps=lambda _: {},
                    market_caps=lambda *_: {}, verbose=False)
    assert out["price"].dtype == float and out["market_cap"].dtype == float
    assert out.set_index("ticker").loc["A", "market_cap"] == 50


def _snapshot(tmp_path, day, rows):
    hist = tmp_path / "history"
    hist.mkdir(exist_ok=True)
    pd.DataFrame(rows, columns=["ticker", "price", "market_cap"]).to_parquet(hist / f"screen_{day}.parquet")


def test_yahoo_refusing_carries_the_last_snapshot_cap_moved_with_the_price(tmp_path):
    s = Settings(data_dir=tmp_path)
    _snapshot(tmp_path, "2026-11-01", [("OLD", 10.0, 5e8), ("SPLIT", 100.0, 1e9), ("NEW", 5.0, None)])
    _snapshot(tmp_path, "2026-11-05", [("OLD", 20.0, 9e8)])                   # the latest wins
    _snapshot(tmp_path, "2026-11-13", [("OLD", 1.0, 1.0)])                    # today's own: ignored
    _snapshot(tmp_path, "2026-08-01", [("STALE", 10.0, 3e8)])                # past the window
    tickers = ["OLD", "SPLIT", "NEW", "STALE"]
    uni = pd.DataFrame({"ticker": tickers})
    fund = pd.DataFrame({"ticker": tickers, "shares_outstanding": None})       # every SEC cap flagged
    out = prices.refresh(s, uni, fund, FakeSource({"OLD": 22.0, "SPLIT": 30.0, "NEW": 5.0, "STALE": 10.0}),
                         reference_caps=lambda s: {}, market_caps=lambda s, t: {}, verbose=False,
                         today=dt.date(2026, 11, 13)).set_index("ticker")
    assert out.loc["OLD", "market_cap"] == pytest.approx(9e8 * 22 / 20) and out.loc["OLD", "market_cap_src"] == "carried"
    for t in ("SPLIT", "NEW", "STALE"):                  # price fell 70%: maybe a split; no cap; too old
        assert np.isnan(out.loc[t, "market_cap"]) and out.loc[t, "market_cap_src"] == "untrusted"


def test_yahoo_and_nasdaq_still_come_before_a_carried_cap(tmp_path):
    s = Settings(data_dir=tmp_path)
    _snapshot(tmp_path, "2026-11-05", [("N", 10.0, 1e9), ("Y", 10.0, 1e9)])
    uni = pd.DataFrame({"ticker": ["N", "Y"]})
    fund = pd.DataFrame({"ticker": ["N", "Y"], "shares_outstanding": None})
    out = prices.refresh(s, uni, fund, FakeSource({"N": 10.0, "Y": 10.0}), reference_caps=lambda s: {"N": 2e9},
                         market_caps=lambda s, t: {"Y": 3e9}, verbose=False, today=dt.date(2026, 11, 13))
    assert out.set_index("ticker")["market_cap_src"].to_dict() == {"N": "nasdaq", "Y": "yahoo"}

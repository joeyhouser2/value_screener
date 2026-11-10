"""Phase 8 — P/E spot check against Yahoo, with Yahoo mocked."""

import datetime as dt

import pandas as pd

from value_screener import sanity
from value_screener.config import Settings

SCREEN = pd.DataFrame({"ticker": ["AAA", "BBB", "CCC"], "pe": [20.0, 21.0, None],
                       "net_income": [10e9, 8e9, -1e9], "market_cap": [200e9, 168e9, 50e9],
                       "as_of": ["2026-09-30", "2026-06-30", "2026-09-30"]})
YAHOO = {"AAA": {"trailingPE": 20.5, "netIncomeToCommon": 9.8e9, "marketCap": 201e9},
         "BBB": {"trailingPE": 17.0, "netIncomeToCommon": 9.9e9, "marketCap": 168e9},
         "CCC": {"trailingPE": None, "netIncomeToCommon": -1e9, "marketCap": 50e9}}


def test_within_tolerance_and_why_a_miss_misses():
    r = sanity.compare(SCREEN, YAHOO, 0.10).set_index("ticker")
    assert r.loc["AAA", "ok"] and r.loc["AAA", "why"] == ""
    assert not r.loc["BBB", "ok"]
    assert r.loc["BBB", "why"].startswith("net income -19%") and "2026-06-30" in r.loc["BBB", "why"]
    assert not r.loc["CCC", "ok"]                       # no P/E on either side can't pass


class FakeSource:
    def __init__(self):
        self.calls = []

    def info(self, ticker):
        self.calls.append(ticker)
        if ticker == "BAD":
            raise RuntimeError("Invalid Crumb")
        return YAHOO[ticker]


def test_yahoo_is_cached_for_the_day_and_a_bad_ticker_is_logged(tmp_path):
    s = Settings(data_dir=tmp_path, sanity_tickers=("AAA", "BAD"))
    src, day = FakeSource(), dt.date(2026, 11, 9)
    got = sanity.yahoo_figures(s, ["AAA", "BAD"], src, day, pause=0)
    assert list(got) == ["AAA"] and (tmp_path / "errors.csv").exists()
    sanity.yahoo_figures(s, ["AAA"], src, day, pause=0)
    assert src.calls == ["AAA", "BAD"]                  # the second run used the cache

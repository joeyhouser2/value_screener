"""Phase 10a — forward tracking on hand-built snapshots and prices. Fake source, no network."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from value_screener import errors, track
from value_screener.config import Settings

COHORT = dt.date(2026, 1, 2)      # a Friday
TODAY = dt.date(2026, 3, 2)


def _settings(tmp_path):
    return Settings(data_dir=tmp_path, screen_min_market_cap=50e6)


def _snapshot(tmp_path, date, deep=("AAA", "BBB"), trough=("CCC",), extra=()):
    tickers = ["AAA", "BBB", "CCC", *extra, *[f"N{i:02d}" for i in range(17)]]
    df = pd.DataFrame({"ticker": tickers, "market_cap": 1e9, "stale": False,
                       "pe": [100.0 + i for i in range(len(tickers))]})
    df["deep_value"] = df["ticker"].isin(deep)
    df["cyclical_trough"] = df["ticker"].isin(trough)
    (tmp_path / "history").mkdir(exist_ok=True)
    df.to_parquet(tmp_path / "history" / f"screen_{date.isoformat()}.parquet", index=False)
    return df


def _closes(start="2026-01-02", end="2026-02-27", **paths):
    """Business-day closes: each ticker as (first, last), linear between them."""
    idx = pd.bdate_range(start, end)
    return pd.DataFrame({t: np.linspace(a, b, len(idx)) for t, (a, b) in paths.items()}, index=idx)


class _Source:
    def __init__(self, frame):
        self.frame, self.requests = frame, []

    def closes(self, tickers, start):
        self.requests.append(list(tickers))
        return self.frame[[t for t in tickers if t in self.frame]]


ALL_UP = dict(SPY=(100.0, 110.0), RSP=(100.0, 105.0), IWD=(100.0, 102.0),
              AAA=(10.0, 20.0), BBB=(10.0, 10.0), CCC=(10.0, 15.0))


def _row(out, screen):
    return out[out["screen"] == screen].iloc[0]


def test_returns_are_equal_weight_from_the_first_close_after_the_snapshot(tmp_path):
    s = _settings(tmp_path)
    _snapshot(tmp_path, COHORT)
    # Jan 2 itself must not count: the first close after the snapshot is Jan 5.
    frame = _closes(**ALL_UP)
    frame.loc["2026-01-05", ["AAA", "BBB", "SPY"]] = [10.0, 10.0, 100.0]
    frame.loc["2026-01-02", "AAA"] = 1.0
    out = track.refresh(s, _Source(frame), TODAY)
    deep = _row(out, "Deep value")
    assert deep["ret"] == pytest.approx((1.0 + 0.0) / 2)      # AAA +100%, BBB flat
    assert deep["SPY"] == pytest.approx(0.10)
    assert deep["excess_vs_spy"] == pytest.approx(0.40)
    assert _row(out, "Cyclical trough")["stocks"] == 1
    pos = track.load_positions(s)
    assert pos["entry_date"].iloc[0] == pd.Timestamp("2026-01-05")


def test_todays_bar_is_ignored(tmp_path):
    s = _settings(tmp_path)
    _snapshot(tmp_path, COHORT)
    frame = _closes(end="2026-03-02", **ALL_UP)
    frame.loc["2026-03-02", "AAA"] = 999.0                   # market still open
    out = track.refresh(s, _Source(frame), TODAY)
    assert _row(out, "Deep value")["ret"] < 1


def test_a_cohort_with_no_close_yet_is_pending(tmp_path):
    s = _settings(tmp_path)
    _snapshot(tmp_path, COHORT)
    out = track.refresh(s, _Source(_closes(end="2026-01-02", **ALL_UP)), COHORT + dt.timedelta(days=1))
    deep = _row(out, "Deep value")
    assert pd.isna(deep["ret"]) and pd.isna(deep["days"]) and deep["stocks"] == 2 and deep["gone"] == 0


def test_members_are_frozen_when_the_snapshot_changes(tmp_path):
    s = _settings(tmp_path)
    _snapshot(tmp_path, COHORT)
    track.refresh(s, _Source(_closes(**ALL_UP)), TODAY)
    _snapshot(tmp_path, COHORT, deep=("CCC",))               # rerun or rule change
    track.refresh(s, _Source(_closes(**ALL_UP)), TODAY)
    pos = track.load_positions(s)
    assert set(pos[pos["screen"] == "Deep value"]["ticker"]) == {"AAA", "BBB"}


def test_cohorts_are_spaced_out():
    days = [COHORT + dt.timedelta(days=7 * i) for i in range(30)]
    out = track.new_cohorts(days, set(), 91)
    assert out[0] == COHORT and len(out) == 3
    assert all((b - a).days >= 91 for a, b in zip(out, out[1:]))
    assert track.new_cohorts(days, {days[-1]}, 91) == []


def test_snapshots_without_screen_columns_are_skipped(tmp_path):
    s = _settings(tmp_path)
    _snapshot(tmp_path, COHORT)
    old = pd.DataFrame({"ticker": ["AAA"], "market_cap": [1e9], "pe": [10.0], "stale": [False]})
    old.to_parquet(tmp_path / "history" / "screen_2025-06-01.parquet", index=False)
    assert list(track.snapshots(s)) == [COHORT]


def test_baseline_is_the_cheapest_tenth_with_real_earnings(tmp_path):
    s = _settings(tmp_path)
    df = _snapshot(tmp_path, COHORT)
    df.loc[0, "pe"] = np.nan                                 # a loss isn't "cheapest"
    df.loc[1, "stale"] = True
    df.loc[2, "market_cap"] = 1e6
    df.loc[3, "pe"] = 1.0
    df.loc[4, "pe"] = 2.0
    out = track.members(df, s)
    assert out[out["screen"] == track.BASELINE]["ticker"].tolist() == [df.loc[3, "ticker"], df.loc[4, "ticker"]]


def test_a_stock_that_vanishes_keeps_its_return_and_is_flagged(tmp_path):
    s = _settings(tmp_path)
    _snapshot(tmp_path, COHORT)
    track.refresh(s, _Source(_closes(**ALL_UP)), TODAY)
    frame = _closes(end="2026-03-20", **{k: v for k, v in ALL_UP.items() if k != "AAA"})
    out = track.refresh(s, _Source(frame), dt.date(2026, 3, 23))
    deep = _row(out, "Deep value")
    pos = track.load_positions(s).set_index(["screen", "ticker"]).loc[("Deep value", "AAA")]
    assert pos["gone"] and pos["ret"] == pytest.approx(20 / 10.2439 - 1, abs=0.01)      # entry Jan 5, not Jan 2
    assert deep["gone"] == 1 and deep["ret"] == pytest.approx(pos["ret"] / 2)
    assert deep["ret_if_gone_lost"] == pytest.approx(-0.5)
    assert "track,AAA," in errors.errors_path(tmp_path).read_text(encoding="utf-8")


def test_a_stock_that_stops_trading_is_flagged_though_old_prices_remain(tmp_path):
    s = _settings(tmp_path)
    _snapshot(tmp_path, COHORT)
    frame = _closes(**ALL_UP)
    frame.loc["2026-01-20":, "CCC"] = np.nan
    track.refresh(s, _Source(frame), TODAY)
    pos = track.load_positions(s)
    assert pos[pos["ticker"] == "CCC"]["gone"].all() and not pos[pos["ticker"] == "BBB"]["gone"].any()


def test_same_day_rerun_uses_the_cache(tmp_path):
    s = _settings(tmp_path)
    _snapshot(tmp_path, COHORT)
    source = _Source(_closes(**{**ALL_UP, **{f"N{i:02d}": (1.0, 2.0) for i in range(17)}}))
    first = track.refresh(s, source, TODAY)
    second = track.refresh(s, source, TODAY)
    assert len(source.requests) == 1 and first["ret"].tolist() == second["ret"].tolist()


class _Broken:
    def closes(self, tickers, start):
        raise RuntimeError("Yahoo is down")


def test_yahoo_down_keeps_the_record_and_the_run(tmp_path):
    s = _settings(tmp_path)
    _snapshot(tmp_path, COHORT)
    track.refresh(s, _Source(_closes(**ALL_UP)), TODAY)
    before = track.load_positions(s)
    out = track.refresh(s, _Broken(), dt.date(2026, 3, 3))
    pd.testing.assert_frame_equal(track.load_positions(s), before)
    assert len(out) and "Yahoo is down" in errors.errors_path(tmp_path).read_text(encoding="utf-8")


def test_no_snapshots_yet(tmp_path):
    (tmp_path / "history").mkdir()
    assert track.refresh(_settings(tmp_path), _Source(pd.DataFrame()), TODAY).empty

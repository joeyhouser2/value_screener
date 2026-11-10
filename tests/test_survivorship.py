"""Phase 10c — survivorship measured from hand-built EDGAR index rows. No network."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from value_screener.backtest import survivorship as sv

INDEX = """Description:           Master Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    March 31, 2015

Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
10-K             Acme  Widgets Inc                                             1001        2015-03-02  edgar/data/1001/a.txt
25-NSE           Acme  Widgets Inc                                             1001        2015-03-20  edgar/data/1001/b.txt
8-K              Acme  Widgets Inc                                             1001        2015-03-21  edgar/data/1001/c.txt
"""


def test_form_index_keeps_the_wanted_forms_and_names_with_double_spaces():
    rows = sv.parse_form_index(INDEX)
    assert [r["form"] for r in rows] == ["10-K", "25-NSE"]
    assert rows[0] == {"form": "10-K", "name": "Acme  Widgets Inc", "cik": 1001, "filed": "2015-03-02"}


def _forms(*rows):
    df = pd.DataFrame(rows, columns=["form", "name", "cik", "filed"])
    df["filed"] = pd.to_datetime(df["filed"])
    return df


FORMS = _forms(
    ("10-K", "Gone Co", 1, "2013-03-01"), ("10-K", "Gone Co", 1, "2014-03-01"), ("25-NSE", "Gone Co", 1, "2014-09-01"),
    ("10-K", "Bought Co", 2, "2013-03-01"), ("DEFM14A", "Bought Co", 2, "2014-01-15"), ("25-NSE", "Bought Co", 2, "2014-04-01"),
    ("10-K", "Live Co", 3, "2013-03-01"), ("25-NSE", "Live Co", 3, "2014-04-01"),                   # in today's universe
    ("20-F", "Foreign Co", 4, "2013-03-01"), ("25-NSE", "Foreign Co", 4, "2014-04-01"),             # no 10-K
    ("10-K", "OTC Co", 5, "2013-03-01"), ("25-NSE", "OTC Co", 5, "2014-04-01"), ("10-K", "OTC Co", 5, "2016-03-01"),
    ("10-K", "Old Co", 6, "2011-03-01"), ("25-NSE", "Old Co", 6, "2014-04-01"),                     # no 10-K in 2 years
)


def test_missing_companies_and_why_they_left():
    m = sv.missing_companies(FORMS, known_ciks={3}).set_index("cik")
    assert sorted(m.index) == [1, 2, 5]
    assert m.loc[2, "acquired"] and not m.loc[1, "acquired"]
    assert m.loc[5, "still_filing"] and not m.loc[1, "still_filing"]
    assert m.loc[1, "delisted"] == pd.Timestamp("2014-09-01") and m.loc[1, "first_10k"] == pd.Timestamp("2013-03-01")


def test_a_reorganization_into_a_new_cik_is_not_a_failure():
    forms = pd.concat([FORMS, _forms(("10-K", "Gone Holdings Inc", 7, "2014-08-01"),     # new registrant
                                     ("10-K", "Gone Fishing Corp", 8, "2011-03-01"))])   # same word, too old
    m = sv.missing_companies(forms, known_ciks={3, 7, 8}, today_names=pd.Series({7: "Gone Holdings Inc", 8: "Gone Fishing"})).set_index("cik")
    assert m.loc[1, "successor"] == 7 and m.loc[1, "reorganized"]
    assert not m.loc[2, "reorganized"] and pd.isna(m.loc[2, "successor"])


def test_a_merger_filing_long_before_the_delisting_still_counts():
    forms = pd.concat([FORMS, _forms(("DEFM14A", "Gone Co", 1, "2012-06-01"))])        # 27 months: regulators
    assert sv.missing_companies(forms, known_ciks={3}).set_index("cik").loc[1, "acquired"]


FLOATS =pd.DataFrame({"cik": [1, 1, 2, 5], "end": pd.to_datetime(["2012-06-30", "2013-06-30", "2013-06-30", "2011-06-30"]),
                       "float": [1e9, 400e6, 20e6, 5e9]})


def test_the_hole_at_a_date_is_who_was_listed_then_with_the_float_known_then():
    m = sv.missing_companies(FORMS, known_ciks={3})
    hole = sv.hole_at(m, FLOATS, dt.date(2013, 12, 31)).set_index("cik")
    assert sorted(hole.index) == [1, 2, 5]
    assert hole.loc[1, "float"] == 400e6 and hole.loc[1, "size"] == "$300M-2B"
    assert np.isnan(hole.loc[5, "float"])                       # its float is over 15 months old
    assert sorted(sv.hole_at(m, FLOATS, dt.date(2014, 6, 30))["cik"]) == [1]    # 2 and 5 delisted by then
    assert sv.hole_at(m, FLOATS, dt.date(2013, 1, 31)).empty                     # no 10-K filed yet


def test_hit_rates_by_size():
    screen = pd.DataFrame({"market_cap": [100e6, 200e6, 3e9, np.nan], "deep_value": [True, False, True, True],
                           "cyclical_trough": [False, False, pd.NA, False]})
    r = sv.hit_rates(screen)
    assert r.loc["$50-300M", "deep_value"] == 0.5 and r.loc["$2-10B", "deep_value"] == 1.0
    assert r.loc["$2-10B", "cyclical_trough"] == 0.0 and r["stocks"].sum() == 3


def test_erase_loss():
    # 90 held returning 10% with 3 points of excess; 10 missing names.
    loss = sv.erase_loss(0.03, 90, 10, 0.10)
    adjusted = (90 * 0.10 + 10 * -loss) / 100
    assert adjusted == pytest.approx(0.10 - 0.03)
    assert sv.erase_loss(0.03, 99, 1, 0.10) > 1                 # even total losses can't erase it
    assert np.isnan(sv.erase_loss(0.03, 90, 0, 0.10))


def test_quarters_stop_at_today():
    assert sv.quarters(2026, dt.date(2026, 5, 1)) == [(2026, 1), (2026, 2)]


def test_a_blank_check_company_is_not_a_failure():
    forms = pd.concat([FORMS, _forms(("10-K", "Rocket Acquisition Corp", 9, "2013-03-01"),
                                     ("25-NSE", "Rocket Acquisition Corp", 9, "2014-04-01"))])
    m = sv.missing_companies(forms, known_ciks={3}).set_index("cik")
    assert m.loc[9, "spac"] and not m.loc[9, "failed"]
    assert m.loc[1, "failed"] and not m.loc[2, "failed"]


def test_failure_drag_counts_failures_of_the_coming_year_at_the_panel_hit_rate(tmp_path):
    from value_screener.backtest import panel
    from value_screener.config import Settings
    s = Settings(data_dir=tmp_path)
    start = dt.date(2013, 12, 31)
    path = panel.panel_path(s, start)
    path.parent.mkdir(parents=True)
    # Half the $300M-2B panel stocks are deep value.
    pd.DataFrame({"market_cap": [500e6] * 4, "deep_value": [True, True, False, False],
                  "cyclical_trough": [False] * 4}).to_parquet(path)
    m = sv.missing_companies(FORMS, known_ciks={3})
    summ = sv.summary(s, m, FLOATS, [start])
    drag = sv.failure_drag(s, m, FLOATS, summ).iloc[0]
    # Gone Co ($400M float) fails in 2014; Bought Co was acquired; OTC Co has no recent float.
    assert drag["year"] == 2014 and drag["failures"] == 1
    assert drag["deep_value_failures"] == 0.5
    full = summ.iloc[0]["deep_value_held"] + summ.iloc[0]["deep_value_missing"]
    assert drag["deep_value_drag"] == pytest.approx(0.5 / full)

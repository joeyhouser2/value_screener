"""Phase 2 — SIC roll-up, Yahoo fallback, submissions summary. No network."""

import pandas as pd
import pytest

from value_screener import industry as ind
from value_screener.config import CONFIG_DIR

CFG = ind.load_groups(CONFIG_DIR)


@pytest.mark.parametrize("sic, group, special", [
    (6021, "Banks", "bank"),          # national commercial banks
    (6798, "REITs", "reit"),
    (6331, "Insurance", "insurer"),   # fire, marine & casualty
    (6324, "Managed Care", None),     # hospital & medical service plans, not an insurer set
    (4911, "Utilities", "utility"),
    (4922, "Midstream & Pipelines", None),
    (3674, "Semiconductors", None),
    (7372, "Software & IT Services", None),
    (2834, "Pharmaceuticals", None),
    (2836, "Biotech", None),
    (2844, "Household & Personal Products", None),
    (1311, "Oil & Gas Producers", None),
    (5812, "Restaurants", None),
    (5331, "Retail", None),
    (3711, "Autos & Parts", None),
    (6770, "Shell Companies & SPACs", None),
])
def test_sic_rollup(sic, group, special):
    g, _sector, sp = ind.classify_sic(sic, CFG)
    assert (g, sp) == (group, special)


def test_missing_and_unknown_sic():
    assert ind.classify_sic(None, CFG) is None
    assert ind.classify_sic("", CFG) is None
    assert ind.classify_sic(9999, CFG)[0] == "Unclassified"


def test_yahoo_fallback():
    assert ind.classify_yahoo("Banks - Regional", "Financial Services", CFG)[0] == "Banks"
    assert ind.classify_yahoo("REIT - Retail", "Real Estate", CFG)[2] == "reit"
    assert ind.classify_yahoo("Auto Parts", "Consumer Cyclical", CFG)[:2] == (
        "Other Consumer Discretionary", "Consumer Discretionary")
    assert ind.classify_yahoo(None, None, CFG) is None


def test_summarize_submissions_finds_annual_form():
    payload = {"sic": "6021", "sicDescription": "National Commercial Banks", "name": "BANK",
               "entityType": "operating", "fiscalYearEnd": "1231",
               "filings": {"recent": {"form": ["8-K", "10-Q", "10-K", "10-Q"]}}}
    s = ind.summarize_submissions(payload)
    assert s["sic"] == "6021" and s["annual_form"] == "10-K"
    assert ind.summarize_submissions({"sic": ""})["sic"] is None


def test_label_uses_sic_then_yahoo():
    uni = pd.DataFrame({"ticker": ["BAC", "FOO", "BAR"],
                        "cik": pd.array([70858, None, 5], dtype="Int64")})
    subs = {70858: {"sic": "6021"}, 5: {"sic": None}}
    yahoo = {"FOO": {"industry": "Semiconductors", "sector": "Technology"}}
    out = ind.label(uni, subs, CFG, yahoo).set_index("ticker")
    assert out.loc["BAC", "industry_group"] == "Banks" and out.loc["BAC", "industry_source"] == "sic"
    assert out.loc["FOO", "industry_group"] == "Semiconductors"
    assert out.loc["FOO", "industry_source"] == "yahoo"
    assert out.loc["BAR", "industry_group"] == "Unclassified"


def test_group_names_with_commas_survive_yaml():
    assert ind.classify_sic(2080, CFG)[0] == "Food, Beverage & Tobacco"
    assert ind.classify_sic(7011, CFG)[0] == "Leisure, Travel & Hotels"


def test_placeholder_sics_fall_back():
    assert ind.classify_sic("0000", CFG) is None
    assert ind.classify_sic(8880, CFG) is None
    assert ind.classify_sic(7510, CFG)[0] == "Transportation"
    assert ind.classify_sic(7500, CFG)[0] == "Consumer Services"


def test_ticker_override_beats_the_sic_code():
    cfg = {"groups": [{"group": "Insurance Brokers", "sector": "Financials", "ranges": [[6400, 6411]]},
                      {"group": "Insurance", "sector": "Financials", "special": "insurer", "ranges": [[6300, 6399]]}],
           "ticker_overrides": {"ACT": "Insurance"}}
    uni = pd.DataFrame({"ticker": ["ACT", "AON"], "cik": [1, 2]})
    subs = {1: {"sic": "6411"}, 2: {"sic": "6411"}}
    out = ind.label(uni, subs, cfg).set_index("ticker")
    assert out.loc["ACT", "special"] == "insurer" and out.loc["ACT", "industry_source"] == "override"
    assert out.loc["AON", "industry_group"] == "Insurance Brokers" and out.loc["AON", "industry_source"] == "sic"

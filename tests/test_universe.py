"""Phase 1 — universe parsing and filtering, no network."""

import pandas as pd

from value_screener import universe as u

NASDAQ = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
GOOGL|Alphabet Inc. - Class A Common Stock|Q|N|N|100|N|N
GOOGN|Alphabet Inc. - Depositary Shares representing a 1/20th Interest in a Share of Series B Mandatory Convertible Preferred Stock|Q|N|N|100|N|N
QQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N
ZXZZT|NASDAQ TEST STOCK|G|Y|N|100|N|N
AACIU|Armada Acquisition Corp. III - Units|G|N|N|100|N|N
AACIW|Armada Acquisition Corp. III - Warrant|G|N|N|100|N|N
AACPR|Apogee Acquisition Corp - Rights|G|N|N|100|N|N
JMIA|Jumia Technologies AG - American Depositary Shares, each representing two ordinary shares|G|N|N|100|N|N
File Creation Time: 1030202618:01|||||||
"""

OTHER = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
BRK.B|Berkshire Hathaway Inc. New Common Stock|N|BRK.B|N|40|N|BRK.B
BAC|Bank of America Corporation Common Stock|N|BAC|N|100|N|BAC
BAC$L|Bank of America Corporation Non Cumulative Perpetual Conv Pfd Ser L|N|BACpL|N|100|N|BAC-L
EPD|Enterprise Products Partners L.P. Common Units|N|EPD|N|100|N|EPD
ADC$A|Agree Realty Corporation Depositary Shares, each representing 1/1,000th of a 4.250% Series A Cumulative Redeemable Preferred Stock|N|ADCpA|N|100|N|ADC-A
ADX|Adams Diversified Equity Fund Inc.|N|ADX|N|100|N|ADX
BTZ|BlackRock Credit Allocation Income Trust|N|BTZ|N|100|N|BTZ
SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY
IMO|Imperial Oil Limited Common Stock|A|IMO|N|100|N|IMO
XYZN|XYZ Corp 6.5% Senior Notes due 2030|N|XYZN|N|100|N|XYZN
File Creation Time: 1030202618:01||||||||
"""

CIKS = {"fields": ["cik", "name", "ticker", "exchange"],
        "data": [[320193, "Apple Inc.", "AAPL", "Nasdaq"],
                 [1067983, "BERKSHIRE HATHAWAY INC", "BRK-B", "NYSE"],
                 [70858, "BANK OF AMERICA CORP", "BAC", "NYSE"],
                 [1652044, "Alphabet Inc.", "GOOGL", "Nasdaq"]]}


def _build(exchanges=("NASDAQ", "NYSE")):
    return u.build_universe(NASDAQ, OTHER, u.parse_cik_map(CIKS), exchanges)


def test_keeps_only_common_stock():
    assert set(_build()["ticker"]) == {"AAPL", "GOOGL", "JMIA", "BRK-B", "BAC", "EPD"}


def test_exchanges_setting_controls_nyse_american():
    assert "IMO" not in set(_build()["ticker"])
    assert "IMO" in set(_build(("NASDAQ", "NYSE", "NYSE American"))["ticker"])


def test_class_shares_use_dash_and_map_to_cik():
    df = _build().set_index("ticker")
    assert df.loc["BRK-B", "cik"] == 1067983
    assert df.loc["AAPL", "exchange"] == "NASDAQ" and df.loc["BAC", "exchange"] == "NYSE"


def test_missing_cik_is_kept_and_flagged():
    df = _build().set_index("ticker")
    assert not df.loc["JMIA", "has_cik"] and pd.isna(df.loc["JMIA", "cik"])
    assert not df.loc["EPD", "has_cik"]


def test_clean_names():
    df = _build().set_index("ticker")
    assert df.loc["AAPL", "name"] == "Apple Inc."
    assert df.loc["GOOGL", "name"] == "Alphabet Inc."
    assert df.loc["JMIA", "name"] == "Jumia Technologies AG"
    assert df.loc["EPD", "name"] == "Enterprise Products Partners L.P."

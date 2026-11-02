"""Phase 9a — fiscal-year history: tag merging, restatements, alignment, derivations."""

import pandas as pd

from value_screener import fundamentals as f
from value_screener import history as h
from value_screener.config import CONFIG_DIR

TAGS = f.load_tags(CONFIG_DIR)


def r(start, end, val, filed=None, form="10-K", accn="a"):
    return [start, end, val, filed or end, form, accn]


def fy(year, val, filed=None, **kw):
    return r(f"{year}-01-01", f"{year}-12-31", val, filed or f"{year + 1}-02-15", **kw)


def bs(year, val, filed=None):
    return r(None, f"{year}-12-31", val, filed or f"{year + 1}-02-15")


def _slim(tags):
    return {"entity": "TEST CO", "taxonomies": ["dei", "us-gaap"], "tags": tags}


def _get(df, concept):
    return df[df["concept"] == concept].set_index("fy_end")["val"]


def test_revenue_merged_across_tag_switch():
    slim = _slim({
        "Revenues": [fy(2016, 80), fy(2017, 90), fy(2018, 95)],
        "RevenueFromContractWithCustomerExcludingAssessedTax": [fy(2018, 100), fy(2019, 110)],
    })
    rev = _get(h.company_history(slim, TAGS), "revenue")
    assert list(rev.index.year) == [2016, 2017, 2018, 2019]
    assert list(rev) == [80, 90, 100, 110]           # newer tag wins the overlapping year


def test_restatement_keeps_latest_value_and_first_filed():
    slim = _slim({"NetIncomeLoss": [
        fy(2024, 50, filed="2025-02-15", accn="k24"),
        fy(2024, 45, filed="2026-02-15", accn="k25"),   # comparative restated in next 10-K
        fy(2025, 60, filed="2026-02-15", accn="k25"),
    ]})
    df = h.company_history(slim, TAGS)
    row = df[(df["concept"] == "net_income") & (df["fy_end"] == "2024-12-31")].iloc[0]
    assert row["val"] == 45
    assert row["filed"] == pd.Timestamp("2026-02-15")
    assert row["first_filed"] == pd.Timestamp("2025-02-15")


def test_quarters_are_not_fiscal_years():
    slim = _slim({"NetIncomeLoss": [r("2025-01-01", "2025-03-31", 5, form="10-Q"), fy(2024, 40)]})
    assert list(_get(h.company_history(slim, TAGS), "net_income")) == [40]


def test_balance_sheet_aligned_on_52_53_week_calendar():
    slim = _slim({
        "Revenues": [r("2023-01-01", "2023-12-30", 100, "2024-02-15"),
                     r("2023-12-31", "2024-12-28", 120, "2025-02-15")],
        "Assets": [r(None, "2023-12-30", 500, "2024-02-15"), r(None, "2024-12-28", 550, "2025-02-15"),
                   r(None, "2024-06-29", 999, "2024-08-01")],        # mid-year 10-Q: ignored
    })
    assets = _get(h.company_history(slim, TAGS), "assets")
    assert list(assets) == [500, 550]


def test_derived_liabilities_gross_profit_and_debt():
    slim = _slim({
        "Revenues": [fy(2025, 1000)],
        "CostOfRevenue": [fy(2025, 600)],
        "LiabilitiesAndStockholdersEquity": [bs(2025, 900)],
        "StockholdersEquity": [bs(2025, 300)],
        "LongTermDebtNoncurrent": [bs(2025, 200)],
        "LongTermDebtCurrent": [bs(2025, 20)],
        "ShortTermBorrowings": [bs(2025, 5)],
    })
    df = h.company_history(slim, TAGS).set_index("concept")
    assert df.loc["gross_profit", "val"] == 400 and df.loc["gross_profit", "src"] == "derived"
    assert df.loc["liabilities", "val"] == 600
    assert df.loc["total_debt", "val"] == 225
    assert not set(h.HELPERS) & set(df.index)        # helper inputs aren't written out


def test_reported_beats_derived():
    slim = _slim({"Revenues": [fy(2025, 1000)], "CostOfRevenue": [fy(2025, 600)],
                  "GrossProfit": [fy(2025, 390)], "Liabilities": [bs(2025, 610)],
                  "LiabilitiesAndStockholdersEquity": [bs(2025, 900)],
                  "StockholdersEquity": [bs(2025, 300)]})
    df = h.company_history(slim, TAGS).set_index("concept")
    assert df.loc["gross_profit", "val"] == 390 and df.loc["gross_profit", "src"] == "reported"
    assert df.loc["liabilities", "val"] == 610


def test_diluted_shares_are_period_averages_not_sums():
    slim = _slim({"Revenues": [fy(2025, 10)],
                  "WeightedAverageNumberOfDilutedSharesOutstanding": [
                      r("2025-01-01", "2025-03-31", 100, form="10-Q"), fy(2025, 104)]})
    assert list(_get(h.company_history(slim, TAGS), "diluted_shares")) == [104]


def test_max_years_and_empty_company():
    slim = _slim({"Revenues": [fy(y, y) for y in range(2005, 2026)]})
    assert len(_get(h.company_history(slim, TAGS, max_years=15), "revenue")) == 15
    assert h.company_history(_slim({}), TAGS).empty


def test_predecessor_history_merges_into_successor():
    old = _slim({"Revenues": [fy(2023, 300), fy(2024, 310)]})
    new = _slim({"Revenues": [fy(2025, 330), fy(2024, 312, filed="2026-02-15")]})
    rev = _get(h.company_history(f.merge_slims([old, new]), TAGS), "revenue")
    assert list(rev) == [300, 312, 330]


def test_to_frame_and_wide():
    a = h.company_history(_slim({"Revenues": [fy(2024, 1), fy(2025, 2)],
                                 "NetIncomeLoss": [fy(2025, 1)]}), TAGS)
    long = h.to_frame({7: a, 8: None, 9: h.company_history(_slim({}), TAGS)})
    assert set(long["cik"]) == {7} and list(long.columns) == h.COLUMNS
    w = h.wide(long)
    assert list(w["revenue"]) == [1, 2] and w["net_income"].isna().tolist() == [True, False]


def test_notes_payable_is_last_resort_debt():
    slim = _slim({"Revenues": [fy(2024, 1), fy(2025, 1)],
                  "NotesPayable": [bs(2024, 50), bs(2025, 60)],
                  "LongTermDebt": [bs(2025, 70)]})
    assert list(_get(h.company_history(slim, TAGS), "total_debt")) == [50, 70]

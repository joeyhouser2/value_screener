"""Phase 3 — TTM construction, tag selection, debt and shares. Synthetic facts, no network."""

import datetime as dt

import pandas as pd
import pytest

from value_screener import fundamentals as f
from value_screener.config import CONFIG_DIR

TAGS = f.load_tags(CONFIG_DIR)


def r(start, end, val, filed=None, form="10-Q", accn="a"):
    return [start, end, val, filed or end, form, accn]


# Calendar FY2025 reported as 10-Q quarters (Q1-Q3) + a 10-K; Q1-Q2 2026 as 10-Qs.
INCOME = [
    r("2025-01-01", "2025-03-31", 10), r("2025-04-01", "2025-06-30", 20),
    r("2025-07-01", "2025-09-30", 30), r("2025-01-01", "2025-12-31", 100, form="10-K"),
    r("2026-01-01", "2026-03-31", 40), r("2026-04-01", "2026-06-30", 50),
]
# Cash flow: only year-to-date figures (Q1, H1, 9M, FY).
CASHFLOW = [
    r("2025-01-01", "2025-03-31", 5), r("2025-01-01", "2025-06-30", 12),
    r("2025-01-01", "2025-09-30", 20), r("2025-01-01", "2025-12-31", 30, form="10-K"),
    r("2026-01-01", "2026-03-31", 8), r("2026-01-01", "2026-06-30", 18),
]


def test_q4_derived_from_fy_minus_three_quarters():
    q, annual = f.quarters_and_annuals(INCOME)
    assert q[pd.Timestamp("2025-12-31")] == 40           # 100 - (10+20+30)
    assert annual[pd.Timestamp("2025-12-31")] == 100


def test_ttm_uses_latest_four_quarters():
    v = f.flow_value(INCOME)
    assert v["src"] == "ttm" and v["end"] == pd.Timestamp("2026-06-30")
    assert v["val"] == 30 + 40 + 40 + 50                 # Q3'25 + Q4'25 + Q1'26 + Q2'26


def test_ytd_cash_flow_differenced_into_quarters():
    q, _ = f.quarters_and_annuals(CASHFLOW)
    assert list(q.values) == [5, 7, 8, 10, 8, 10]        # Q1..Q4 2025, Q1..Q2 2026
    assert f.flow_value(CASHFLOW)["val"] == 8 + 10 + 8 + 10


def test_annual_only_filer_falls_back_to_annual():
    rows = [r("2024-01-01", "2024-12-31", 90, form="10-K"),
            r("2025-01-01", "2025-12-31", 120, form="10-K")]
    v = f.flow_value(rows)
    assert (v["val"], v["src"], v["prev"]) == (120, "annual", 90)


def test_restatement_keeps_latest_filing():
    rows = [r("2025-01-01", "2025-12-31", 100, filed="2026-02-01", form="10-K"),
            r("2025-01-01", "2025-12-31", 95, filed="2027-02-01", form="10-K")]
    assert f.flow_value(rows)["val"] == 95


def _slim(tags):
    return {"entity": "TEST CO", "taxonomies": ["dei", "us-gaap"], "tags": tags}


def test_newest_tag_wins_over_priority():
    # Old filings used Revenues; newer ones switched to the ASC 606 tag (listed first
    # anyway) — here the *lower*-priority tag is the more recent one and must win.
    slim = _slim({
        "RevenueFromContractWithCustomerExcludingAssessedTax":
            [r("2018-01-01", "2018-12-31", 1, form="10-K")],
        "Revenues": [r("2025-01-01", "2025-12-31", 500, form="10-K")],
    })
    row = f.extract(slim, TAGS, today=dt.date(2026, 3, 1))
    assert row["revenue"] == 500 and row["revenue_src"] == "annual"


def test_debt_split_and_short_term_borrowings():
    slim = _slim({
        "LongTermDebt": [r(None, "2020-12-31", 999)],                      # stale total tag
        "LongTermDebtNoncurrent": [r(None, "2025-12-31", 80)],
        "LongTermDebtCurrent": [r(None, "2025-12-31", 20)],
        "ShortTermBorrowings": [r(None, "2025-12-31", 5)],
    })
    row = f.extract(slim, TAGS, today=dt.date(2026, 3, 1))
    assert row["total_debt"] == 105


def test_shares_summed_across_classes_in_latest_filing():
    slim = _slim({"dei:EntityCommonStockSharesOutstanding": [
        r(None, "2025-01-20", 999, accn="old"),
        r(None, "2026-01-20", 300, accn="new"), r(None, "2026-01-20", 50, accn="new"),
    ]})
    assert f.extract(slim, TAGS)["shares_outstanding"] == 350


def test_stale_flag():
    slim = _slim({"NetIncomeLoss": [r("2023-01-01", "2023-12-31", 10, form="10-K")]})
    assert f.extract(slim, TAGS, today=dt.date(2026, 3, 1))["stale"]
    assert not f.extract(slim, TAGS, today=dt.date(2024, 3, 1))["stale"]


def test_no_data_is_stale_not_a_crash():
    row = f.extract(_slim({}), TAGS)
    assert row["revenue"] is None and row["stale"]


def test_slim_facts_keeps_only_configured_tags():
    payload = {"entityName": "X", "facts": {
        "us-gaap": {"NetIncomeLoss": {"units": {"USD": [
            {"start": "2025-01-01", "end": "2025-12-31", "val": 7, "filed": "2026-02-01",
             "form": "10-K", "accn": "1", "fy": 2025}]}},
                    "SomethingElse": {"units": {"USD": [{"end": "2025-12-31", "val": 1}]}}},
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"end": "2026-01-20", "val": 100, "filed": "2026-02-01", "form": "10-K", "accn": "1"}]}}},
    }}
    slim = f.slim_facts(payload, TAGS)
    assert set(slim["tags"]) == {"NetIncomeLoss", "dei:EntityCommonStockSharesOutstanding"}
    assert slim["tags"]["NetIncomeLoss"][0] == ["2025-01-01", "2025-12-31", 7, "2026-02-01", "10-K", "1"]


def test_prior_year_found_on_52_53_week_calendar():
    # Quarter ends drift a day or two each year (Apple-style fiscal calendar).
    ends = ["2025-03-29", "2025-06-28", "2025-09-27", "2025-12-27",
            "2026-03-28", "2026-06-27"]
    starts = ["2024-12-29", "2025-03-30", "2025-06-29", "2025-09-28",
              "2025-12-28", "2026-03-29"]
    rows = [r(s, e, 10 * (i + 1)) for i, (s, e) in enumerate(zip(starts, ends))]
    rows += [r("2024-03-31", "2024-06-29", 1), r("2024-06-30", "2024-09-28", 1),
             r("2024-09-29", "2024-12-28", 1)]
    v = f.flow_value(rows)
    assert v["val"] == 30 + 40 + 50 + 60
    assert v["prev"] == 1 + 1 + 10 + 20                # 4 quarters ending 2025-06-28


# --- reorganizations: successor CIK with a predecessor ------------------------ #

# Predecessor: full quarterly history through Q1 2026.
OLD = _slim({"NetIncomeLoss": [
    r("2025-04-01", "2025-06-30", 10, accn="o1"), r("2025-07-01", "2025-09-30", 20, accn="o2"),
    r("2025-01-01", "2025-12-31", 100, form="10-K", accn="o3"),
    r("2025-01-01", "2025-03-31", 30, accn="o0"), r("2026-01-01", "2026-03-31", 40, accn="o4"),
]})
# Successor: one 10-Q (Q2 2026 plus comparatives), restating Q2 2025 as 11.
NEW = _slim({"NetIncomeLoss": [
    r("2026-04-01", "2026-06-30", 50, filed="2026-08-05", accn="n1"),
    r("2026-01-01", "2026-06-30", 90, filed="2026-08-05", accn="n1"),
    r("2025-04-01", "2025-06-30", 11, filed="2026-08-05", accn="n1"),
]})


def test_successor_alone_has_short_history():
    row = f.extract(NEW, TAGS, today=dt.date(2026, 10, 31))
    assert row["net_income"] is None and row["short_history"] and row["stale"]


def test_merged_predecessor_gives_ttm_and_successor_wins_overlaps():
    merged = f.merge_slims([OLD, NEW])
    row = f.extract(merged, TAGS, today=dt.date(2026, 10, 31))
    # Q3'25 20 + Q4'25 (100-30-11-20 = 39, using the restated Q2 of 11) + Q1'26 40 + Q2'26 50
    assert row["net_income"] == 20 + 39 + 40 + 50
    assert row["net_income_end"] == pd.Timestamp("2026-06-30") and not row["short_history"]


def test_load_predecessors_has_xom():
    assert f.load_predecessors(CONFIG_DIR)[2115436] == [34088]


def test_tags_fingerprint_changes_with_tags():
    fp = f.tags_fingerprint(TAGS)
    assert fp == f.tags_fingerprint(f.load_tags(CONFIG_DIR))
    more = {**TAGS, "extra": {"kind": "flow", "unit": "USD", "tags": ["Extra"]}}
    assert f.tags_fingerprint(more) != fp


def test_average_concepts_ignored_by_ttm_extract():
    slim = _slim({"WeightedAverageNumberOfDilutedSharesOutstanding": [
        r("2025-01-01", "2025-03-31", 100), r("2025-04-01", "2025-06-30", 100)]})
    assert "diluted_shares" not in f.extract(slim, TAGS)


def test_notes_payable_only_when_no_other_debt_tag():
    only_notes = _slim({"NotesPayable": [r(None, "2025-09-30", 60)]})
    assert f.extract(only_notes, TAGS)["total_debt"] == 60
    both = _slim({"NotesPayable": [r(None, "2025-09-30", 60)], "LongTermDebt": [r(None, "2025-06-30", 90)]})
    assert f.extract(both, TAGS)["total_debt"] == 90


class _FakeClient:
    """company_facts from a dict of slims (None = 404); records the cache variant."""

    def __init__(self, slims):
        self.slims, self.variants = slims, set()

    def company_facts(self, cik, transform=None, variant=None):
        self.variants.add(variant)
        return self.slims.get(int(cik))


def test_refresh_parses_in_processes_and_logs_bad_tickers(tmp_path):
    from value_screener import history
    from value_screener.config import load_settings

    s = load_settings(data_dir=tmp_path)
    good = _slim({"Revenues": [r("2024-01-01", "2024-12-31", 90, form="10-K"),
                               r("2025-01-01", "2025-12-31", 120, form="10-K")],
                  "NetIncomeLoss": [r("2025-01-01", "2025-12-31", 12, form="10-K")]})
    uni = pd.DataFrame({"ticker": ["GOOD", "GONE", "NOCIK"], "cik": [1, 2, None]})
    client = _FakeClient({1: good, 2: None})
    df = f.refresh(s, client, uni, verbose=False).set_index("ticker")
    assert df.loc["GOOD", "revenue"] == 120 and pd.isna(df.loc["GONE", "revenue"])
    assert client.variants == {f.tags_fingerprint(TAGS)}
    h = history.load(s)
    assert set(h["cik"]) == {1} and len(h[h["concept"] == "revenue"]) == 2
    assert "GONE" in (tmp_path / "errors.csv").read_text(encoding="utf-8")


def test_proxy_statement_values_do_not_replace_the_10k():
    rows = [r("2025-06-01", "2026-05-31", 4433000000, filed="2026-07-20", form="10-K"),
            r("2025-06-01", "2026-05-31", 4433, filed="2026-08-17", form="DEF 14A"),
            r("2024-06-01", "2025-05-31", 4000, filed="2026-08-17", form="DEF 14A")]
    v = f.flow_value(rows)
    assert v["val"] == 4433000000 and v["prev"] is None


def test_amended_filings_still_count():
    rows = [r("2025-01-01", "2025-12-31", 100, filed="2026-02-01", form="10-K"),
            r("2025-01-01", "2025-12-31", 90, filed="2026-03-01", form="10-K/A")]
    assert f.flow_value(rows)["val"] == 90


def test_registration_statements_still_count():
    rows = [r("2025-01-01", "2025-12-31", 70, filed="2026-03-01", form="S-1")]
    assert f.flow_value(rows)["val"] == 70

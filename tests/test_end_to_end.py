"""Phase 9g — the whole chain, on a synthetic boom-bust industry and on real SEC filings.

``fixtures/facts`` holds real slimmed companyfacts (XOM, LEN, MU), the XBRL as filed, frozen
so a later filing can't change these tests. Reported figures are hard-coded; the arithmetic
on them is done here independently of the code under test.
"""

import gzip
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import requests

from value_screener import errors, industry, survival, valuation
from value_screener import fundamentals as f
from value_screener import history as h
from value_screener import normalized as n
from value_screener.config import CONFIG_DIR, Settings, load_settings
from value_screener.sec import SecClient

TAGS = f.load_tags(CONFIG_DIR)
FACTS = Path(__file__).parent / "fixtures" / "facts"
BASE = [0.10, 0.20, 0.30, 0.20, 0.10, 0.20, 0.30, 0.20, 0.10]    # nine years, peak 30%, then:
TROUGH_MARGIN, PEAK_MARGIN = 0.04, 0.30
M = 1e6


def _company_years(cik, margins):
    """Ten fiscal years: revenue $1,000M, tax 25%, flat balance sheet and share count."""
    rows = []
    for year, margin in zip(range(2016, 2026), margins):
        values = dict(revenue=1000 * M, operating_income=1000 * M * margin, net_income=750 * M * margin,
                      diluted_shares=100 * M, assets=2000 * M, operating_cash_flow=400 * M, total_debt=500 * M,
                      assets_current=800 * M, liabilities_current=400 * M, gross_profit=400 * M)
        rows += [{"cik": cik, "fy_end": pd.Timestamp(f"{year}-12-31"), "concept": k, "val": v}
                 for k, v in values.items()]
    return rows


def _industry(history_rows=()):
    """TROUGH and PEAK are cheap on through-the-cycle earnings; eight dear peers set the scale."""
    margins = {"TROUGH": [*BASE, TROUGH_MARGIN], "PEAK": [*BASE, PEAK_MARGIN]}
    caps = {"TROUGH": 522 * M, "PEAK": 600 * M}
    peers = [f"PEER{i}" for i in range(8)]
    tickers = [*margins, *peers]
    margins.update({t: [*BASE, 0.20] for t in peers})
    caps.update({t: 8000 * M for t in peers})
    uni = pd.DataFrame({"ticker": tickers, "name": tickers, "exchange": "NYSE",
                        "cik": [float(i + 1) for i in range(10)]})
    ind = pd.DataFrame({"ticker": tickers, "industry_group": "Homebuilders", "sector": "Consumer Discretionary",
                        "special": None, "sic": 1531})
    fund = pd.DataFrame({"ticker": tickers, "revenue": 1000 * M, "stale": False,
                         "operating_income": [1000 * M * margins[t][-1] for t in tickers],
                         "net_income": [750 * M * margins[t][-1] for t in tickers]})
    px = pd.DataFrame({"ticker": tickers, "price": [caps[t] / (100 * M) for t in tickers],
                       "price_date": pd.Timestamp("2026-10-30"), "market_cap": [caps[t] for t in tickers]})
    rows = [r for i, t in enumerate(tickers) for r in _company_years(float(i + 1), margins[t])]
    return uni, ind, fund, px, pd.DataFrame([*rows, *history_rows])


def _screen(history_rows=(), settings=None):
    uni, ind, fund, px, hist = _industry(history_rows)
    return valuation.build_screen(uni, ind, fund, px, settings or Settings(), hist).set_index("ticker")


def test_boom_bust_trough_flagged_and_peak_not():
    out = _screen()
    assert out.loc["TROUGH", "cyclical_trough"] and not out.loc["PEAK", "cyclical_trough"]
    assert "P/E10" in out.loc["TROUGH", "cyclical_trough_reasons"]
    assert out.loc["PEAK", "cyclical_trough_reasons"] == ""
    assert not out.loc["PEER0", "cyclical_trough"]


def test_boom_bust_figures_by_hand():
    out = _screen()
    trough, peak = out.loc["TROUGH"], out.loc["PEAK"]
    # Margins sum to 1.74 over 10 years: mean EBIT $174M, mean earnings $130.5M, cap $522M.
    assert trough["norm_earnings"] == pytest.approx(130.5 * M) and trough["pe10"] == pytest.approx(4.0)
    # Sum 2.00: mean earnings $150M, cap $600M. Same P/E10, so only the margin separates them.
    assert peak["norm_earnings"] == pytest.approx(150 * M) and peak["pe10"] == pytest.approx(4.0)
    # 4% is the lowest of ten, tied only with itself: (0 + 0.5) / 10.
    assert trough["margin_position"] == pytest.approx(0.05)
    # 30% beats seven years and ties three, itself included: (7 + 1.5) / 10.
    assert peak["margin_position"] == pytest.approx(0.85)
    assert trough["median_margin"] == pytest.approx(0.20) and trough["midcycle_ebit"] == pytest.approx(200 * M)
    assert trough["years_history"] == 10 and trough["loss_years"] == 0 and trough["share_cagr"] == pytest.approx(0)


def test_boom_bust_f_score_by_hand():
    trough = _screen().loc["TROUGH"]
    # Pass: ROA > 0, CFO > 0, CFO > NI, leverage flat, no issuance. Fail, as nothing else
    # improved: ROA up, liquidity, gross margin, turnover. 5 of 9 clears the trough bar of 4.
    assert trough["f_score"] == 5 and trough["f_score_n"] == 9


def test_financials_get_na_not_zero_through_the_whole_chain():
    uni, ind, fund, px, hist = _industry()
    ind["special"], ind["industry_group"] = "bank", "Banks"
    bank = valuation.build_screen(uni, ind, fund, px, Settings(), hist).set_index("ticker").loc["TROUGH"]
    assert pd.isna(bank["altman_z"]) and pd.isna(bank["interest_coverage"]) and pd.isna(bank["p_ncav"])
    assert bank["f_score_n"] == 7      # no current ratio or gross margin, rather than two fails


# --- real filings ------------------------------------------------------------- #

def _slim(name):
    with gzip.open(FACTS / f"{name}.json.gz", "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _years(*names):
    slim = f.merge_slims([_slim(x) for x in names]) if len(names) > 1 else _slim(names[0])
    return h.wide(h.company_history(slim, TAGS, max_years=15).assign(cik=1))


def _on(years, fy):
    return years[years["fy_end"].dt.year == fy].iloc[0]


def test_xom_needs_its_predecessor():
    alone, merged = _years("xom_new"), _years("xom_old", "xom_new")
    assert len(alone) <= 1 and len(merged) >= 10


def test_xom_energy_cycle_matches_its_10k():
    y = _years("xom_old", "xom_new")
    assert _on(y, 2020)["net_income"] == -22.44e9         # the 2020 loss
    assert _on(y, 2022)["net_income"] == 55.74e9          # the peak
    assert _on(y, 2023)["net_income"] == 36.01e9
    m = n.company_metrics(y, window=10)
    assert m["loss_years"] == 1 and m["years_history"] == 10


def test_homebuilder_len_matches_its_10k_and_survives_its_dropped_tag():
    y = _years("len")
    fy21, fy25 = _on(y, 2021), _on(y, 2025)
    assert fy21["revenue"] == pytest.approx(27.13e9, rel=1e-3)
    assert fy21["net_income"] == pytest.approx(4.43e9, rel=1e-3)
    # LEN stopped tagging operating income in 2021: margin comes from pretax income instead.
    assert pd.isna(fy25.get("operating_income"))
    assert n.yearly_figures(y, window=10)["margin"].iloc[-1] == pytest.approx(0.0823, abs=0.002)
    # By hand: each year's earnings on the FY2025 share count, no split-like jumps in these years.
    ni = [911.844, 810.48, 1695.831, 1849.052, 2465.036, 4430.111, 4614.125, 3938.511, 3932.533, 2078.179]
    shares = [223.079, 237.155, 307.968, 318.419, 309.406, 306.612, 289.824, 283.319, 272.019, 257.746]
    expected = np.mean([a * 257.746 / b for a, b in zip(ni, shares)]) * 1e6
    assert n.company_metrics(y, window=10)["norm_earnings"] == pytest.approx(expected, rel=1e-5)


def test_semi_mu_trough_and_peak_margins():
    y = _years("mu")
    fy22, fy23 = _on(y, 2022), _on(y, 2023)
    assert fy22["revenue"] == 30.758e9 and fy22["net_income"] == 8.687e9
    assert fy23["revenue"] == 15.54e9 and fy23["net_income"] == -5.833e9
    assert fy23["operating_income"] == -5.745e9
    trough = fy23["operating_income"] / fy23["revenue"]
    assert n.company_metrics(y, current_margin=trough, window=10)["margin_position"] == pytest.approx(0.05)
    last = y.sort_values("fy_end").tail(10)
    best = (last["operating_income"] / last["revenue"]).max()
    assert n.company_metrics(y, current_margin=best, window=10)["margin_position"] == pytest.approx(0.95)
    assert n.company_metrics(y, window=10)["margin_vol"] >= load_settings().cyclical_margin_vol


# --- the run as a whole --------------------------------------------------------- #

class _Sec:
    """Serves companyfacts and submissions payloads in the SEC's format."""

    headers = {}

    def get(self, url, timeout):
        rows = [{"start": f"{y}-01-01", "end": f"{y}-12-31", "val": 100.0 + y, "filed": f"{y + 1}-02-01",
                 "form": "10-K", "accn": f"a{y}"} for y in range(2020, 2026)]
        payload = ({"sic": "1531", "sicDescription": "Operative Builders", "filings": {"recent": {"form": ["10-K"]}}}
                   if "submissions" in url else
                   {"facts": {"us-gaap": {"Revenues": {"units": {"USD": rows}},
                                          "NetIncomeLoss": {"units": {"USD": rows}}}}})
        resp = requests.Response()
        resp.status_code = 200
        resp._content = json.dumps(payload).encode()
        return resp


class _NoNetwork:
    headers = {}

    def get(self, url, timeout):
        raise AssertionError(f"warm cache hit the network: {url}")


def test_warm_cache_rerun_makes_no_sec_calls(tmp_path):
    s = load_settings(data_dir=tmp_path, yf_industry_fallback=False)
    uni = pd.DataFrame({"ticker": ["AAA", "BBB"], "cik": [1, 2]})
    ua = "Test User test@example.com"
    cold = SecClient(ua, tmp_path / "sec", rps=1000, base_delay=0, session=_Sec())
    industry.refresh(s, cold, uni, verbose=False)
    first = f.refresh(s, cold, uni, verbose=False)
    assert cold.calls == 4                                  # submissions and facts for each

    warm = SecClient(ua, tmp_path / "sec", rps=1000, base_delay=0, session=_NoNetwork())
    industry.refresh(s, warm, uni, verbose=False)
    again = f.refresh(s, warm, uni, verbose=False)
    assert warm.calls == 0
    assert again["revenue"].tolist() == first["revenue"].tolist()


BAD_ROWS = {
    "nan": {"cik": 1.0, "fy_end": pd.Timestamp("2015-12-31"), "concept": "revenue", "val": np.nan},
    "inf": {"cik": 1.0, "fy_end": pd.Timestamp("2015-12-31"), "concept": "net_income", "val": np.inf},
    "no date": {"cik": 1.0, "fy_end": pd.NaT, "concept": "revenue", "val": 5.0},
    "negative shares": {"cik": 1.0, "fy_end": pd.Timestamp("2015-12-31"), "concept": "diluted_shares", "val": -9.0},
    "zero revenue": {"cik": 1.0, "fy_end": pd.Timestamp("2015-12-31"), "concept": "revenue", "val": 0.0},
    "unknown company": {"cik": 999.0, "fy_end": pd.Timestamp("2015-12-31"), "concept": "revenue", "val": 5.0},
    "unknown concept": {"cik": 1.0, "fy_end": pd.Timestamp("2015-12-31"), "concept": "nonsense", "val": 5.0},
}


@pytest.mark.parametrize("name", BAD_ROWS)
def test_one_bad_history_row_does_not_fail_the_run(name):
    out = _screen([BAD_ROWS[name]])
    assert len(out) == 10
    assert out.loc["PEER0", "pe10"] == _screen().loc["PEER0", "pe10"]


@pytest.mark.parametrize("module, func", [(n, "company_metrics"), (survival, "f_score")])
def test_a_company_that_cannot_be_computed_is_logged_and_the_run_continues(module, func, tmp_path, monkeypatch):
    real = getattr(module, func)

    def flaky(g, **kw):
        if g["cik"].iloc[0] == 1:
            raise ValueError("bad company")
        return real(g, **kw)

    monkeypatch.setattr(module, func, flaky)
    out = _screen(settings=Settings(data_dir=tmp_path))
    assert len(out) == 10 and pd.notna(out.loc["PEAK", "f_score"]) and pd.notna(out.loc["PEAK", "pe10"])
    assert "bad company" in errors.errors_path(tmp_path).read_text(encoding="utf-8")

"""Phase 6 — screen filters and export, on a tiny screen. No network."""

import pandas as pd

from value_screener import cli, valuation
from value_screener.config import Settings


def _screen():
    return pd.DataFrame({
        "ticker": ["A", "B", "C", "D"], "name": list("ABCD"),
        "industry_group": ["Software & IT Services", "Banks", "Software & IT Services", "Banks"],
        "sector": ["Technology", "Financials", "Technology", "Financials"],
        "label": ["Undervalued", "Undervalued", "Overvalued", "Insufficient data"],
        "market_cap": [5e9, 5e8, 2e10, None], "quality_pass": [True, True, False, False],
        "composite_pct": [0.1, 0.05, 0.95, None],
    })


def test_filters():
    df = _screen()
    assert cli.filter_screen(df, label="undervalued")["ticker"].tolist() == ["A", "B"]
    assert cli.filter_screen(df, industry_q="software")["ticker"].tolist() == ["A", "C"]
    assert cli.filter_screen(df, label="undervalued", min_mcap=1e9)["ticker"].tolist() == ["A"]
    assert cli.filter_screen(df, quality_only=True)["ticker"].tolist() == ["A", "B"]
    assert cli.filter_screen(df, sector_q="finan", label="insufficient")["ticker"].tolist() == ["D"]


def test_export_one_sheet_per_label(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(data_dir=tmp_path))
    monkeypatch.setattr(valuation, "load", lambda s: _screen())
    out = tmp_path / "s.xlsx"
    cli.main(["export", "--xlsx", str(out)])
    sheets = pd.read_excel(out, sheet_name=None)
    assert set(sheets) == set(valuation.LABELS)
    assert sheets["Undervalued"]["ticker"].tolist() == ["A", "B"]


def test_coverage_report():
    df = pd.DataFrame({"industry_group": ["X", "X", "Y"], "label": ["Fair", "Insufficient data", "Fair"],
                       "market_cap": [1.0, None, 2.0], "pe": [10.0, None, None], "ps": None,
                       "pb": None, "ev_ebitda": None, "fcf_yield": None, "p_ffo": None})
    rep = cli.coverage_report(df)
    assert rep.loc["ALL", "stocks"] == 3
    assert rep.loc["X", "pe"] == 0.5 and rep.loc["X", "labelled"] == 0.5
    assert rep.loc["Y", "market_cap"] == 1.0

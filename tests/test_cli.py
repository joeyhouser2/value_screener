"""Phase 6 — screen filters and export, on a tiny screen. No network."""

import subprocess
import sys

import pandas as pd
import pytest

from value_screener import cli, screens, valuation
from value_screener.config import Settings


def _screen():
    return pd.DataFrame({
        "ticker": ["A", "B", "C", "D"], "name": list("ABCD"),
        "industry_group": ["Software & IT Services", "Banks", "Software & IT Services", "Banks"],
        "sector": ["Technology", "Financials", "Technology", "Financials"],
        "label": ["Undervalued", "Undervalued", "Overvalued", "Insufficient data"],
        "market_cap": [5e9, 5e8, 2e10, None], "quality_pass": [True, True, False, False],
        "composite_pct": [0.1, 0.05, 0.95, None],
        "deep_value": [True, False, False, False], "cyclical_trough": [False, True, False, False],
        "deep_value_reasons": ["P/TBV", "", "", ""], "cyclical_trough_reasons": ["", "P/E10", "", ""],
        "turning": pd.array([True, pd.NA, False, pd.NA], dtype="boolean"),
    })


def test_filters():
    df = _screen()
    assert cli.filter_screen(df, label="undervalued")["ticker"].tolist() == ["A", "B"]
    assert cli.filter_screen(df, industry_q="software")["ticker"].tolist() == ["A", "C"]
    assert cli.filter_screen(df, label="undervalued", min_mcap=1e9)["ticker"].tolist() == ["A"]
    assert cli.filter_screen(df, quality_only=True)["ticker"].tolist() == ["A", "B"]
    assert cli.filter_screen(df, sector_q="finan", label="insufficient")["ticker"].tolist() == ["D"]


def test_screen_filters():
    df = _screen()
    assert cli.filter_screen(df, label="deep-value")["ticker"].tolist() == ["A"]
    assert cli.filter_screen(df, label="cyclical-trough")["ticker"].tolist() == ["B"]
    assert cli.filter_screen(df, turning_only=True)["ticker"].tolist() == ["A"]    # blank isn't turning


def test_export_one_sheet_per_label_and_screen(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(data_dir=tmp_path))
    monkeypatch.setattr(valuation, "load", lambda s: _screen())
    out = tmp_path / "s.xlsx"
    cli.main(["export", "--xlsx", str(out)])
    sheets = pd.read_excel(out, sheet_name=None)
    assert set(sheets) == {*valuation.LABELS, *screens.SCREENS}
    assert sheets["Undervalued"]["ticker"].tolist() == ["A", "B"]
    assert sheets["Deep value"]["ticker"].tolist() == ["A"]
    assert sheets["Cyclical trough"]["ticker"].tolist() == ["B"]


def test_screen_command_shows_why(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: Settings())
    monkeypatch.setattr(valuation, "load", lambda s: _screen())
    cli.main(["screen", "--label", "deep-value"])
    out = capsys.readouterr().out
    assert "P/TBV" in out and "1 matching stocks" in out


def test_coverage_report():
    df = pd.DataFrame({"industry_group": ["X", "X", "Y"], "label": ["Fair", "Insufficient data", "Fair"],
                       "market_cap": [1.0, None, 2.0], "pe": [10.0, None, None], "ps": None,
                       "pb": None, "ev_ebitda": None, "fcf_yield": None, "p_ffo": None})
    rep = cli.coverage_report(df)
    assert rep.loc["ALL", "stocks"] == 3
    assert rep.loc["X", "pe"] == 0.5 and rep.loc["X", "labelled"] == 0.5
    assert rep.loc["Y", "market_cap"] == 1.0


@pytest.mark.parametrize("module", ["cli", "history", "normalized", "peers", "screens", "survival", "trend", "valuation"])
def test_each_module_imports_on_its_own(module):
    # In one process an earlier import hides a circular one, so each gets a fresh interpreter.
    subprocess.run([sys.executable, "-c", f"import value_screener.{module}"], check=True)

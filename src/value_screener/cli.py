"""Phase 6 — command line: ``value-screener refresh | screen | export``."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import pandas as pd

from . import errors, fundamentals, history, industry, prices, screens, sec, track, trend, universe, valuation
from .backtest import panel, returns, survivorship
from .config import REPO_ROOT, load_settings

LABEL_ALIASES = {"undervalued": "Undervalued", "fair": "Fair", "overvalued": "Overvalued",
                 "insufficient": "Insufficient data"}
SCREEN_ALIASES = {c.replace("_", "-"): c for c in screens.SCREENS.values()}
SHOW = ["ticker", "name", "industry_group", "label", "composite_pct", "market_cap", "pe", "ps",
        "pb", "ev_ebitda", "fcf_yield", "p_ffo", "dividend_yield", "roe", "quality_issues"]
SCREEN_SHOW = ["ticker", "name", "industry_group", "label", "market_cap", "pe10", "p_tbv", "p_ncav",
               "margin_position", "f_score", "f_score_n", "turning", "off_52w_high"]


def cmd_refresh(args) -> int:
    s = load_settings()
    client = sec.client_from_settings(s)
    errors.reset(s.data_dir)
    t0 = time.time()

    uni = universe.refresh(s, client)
    if args.tickers:
        wanted = {t.upper().replace(".", "-") for t in args.tickers}
        uni = uni[uni["ticker"].isin(wanted)].reset_index(drop=True)
        print(f"Subset run: {len(uni)} tickers (peer percentiles need full runs to be meaningful)")
    ind = industry.refresh(s, client, uni)
    fund = fundamentals.refresh(s, client, uni)
    px = prices.refresh(s, uni, fund)
    tr = trend.refresh(s, uni)
    screen = valuation.refresh(s, uni, ind, fund, px, history.load(s), tr)

    n_err = max(0, sum(1 for _ in errors.errors_path(s.data_dir).open(encoding="utf-8")) - 1)
    print(f"Done in {time.time() - t0:.0f}s: {len(screen)} stocks, {client.calls} SEC requests, "
          f"{n_err} logged errors -> {errors.errors_path(s.data_dir)}")
    return 0


def filter_screen(df: pd.DataFrame, *, label=None, industry_q=None, sector_q=None,
                  min_mcap=None, quality_only=False, turning_only=False) -> pd.DataFrame:
    if label in SCREEN_ALIASES:
        df = df[df[SCREEN_ALIASES[label]]]
    elif label:
        df = df[df["label"] == LABEL_ALIASES.get(label.lower(), label)]
    if industry_q:
        df = df[df["industry_group"].fillna("").str.contains(industry_q, case=False, regex=False)]
    if sector_q:
        df = df[df["sector"].fillna("").str.contains(sector_q, case=False, regex=False)]
    if min_mcap:
        df = df[df["market_cap"] >= min_mcap]
    if quality_only:
        df = df[df["quality_pass"]]
    if turning_only:
        df = df[df["turning"].fillna(False).astype(bool)]
    return df


def _fmt(df: pd.DataFrame, show: list[str] = SHOW) -> str:
    out = df.reindex(columns=show).copy()
    out["name"] = out["name"].fillna("").str.slice(0, 28)
    out["market_cap"] = (out["market_cap"] / 1e9).map(lambda v: f"{v:,.1f}B" if pd.notna(v) else "")
    fmt = lambda columns, spec: [out.__setitem__(c, out[c].map(lambda v: format(v, spec) if pd.notna(v) else ""))
                                 for c in columns if c in out]
    fmt(("composite_pct", "fcf_yield", "dividend_yield", "roe", "margin_position", "off_52w_high"), ".0%")
    fmt(("pe", "ps", "pb", "ev_ebitda", "p_ffo", "pe10", "p_tbv", "p_ncav"), ".1f")
    fmt(("f_score", "f_score_n"), ".0f")
    if "turning" in out:
        out["turning"] = out["turning"].map(lambda v: "" if pd.isna(v) else "yes" if v else "no")
    return out.to_string(index=False)


def cmd_screen(args) -> int:
    s = load_settings()
    df = filter_screen(valuation.load(s), label=args.label, industry_q=args.industry,
                       sector_q=args.sector, min_mcap=args.min_mcap, quality_only=args.quality_only,
                       turning_only=args.turning)
    df = df.sort_values("composite_pct", ascending=args.label != "overvalued")
    col = SCREEN_ALIASES.get(args.label)
    print(_fmt(df.head(args.top), [*SCREEN_SHOW, f"{col}_reasons"] if col else SHOW))
    print(f"\n{len(df)} matching stocks")
    return 0


def write_sheets(df: pd.DataFrame, target) -> None:
    """One sheet per label and per screen; a stock on a screen is on its label's sheet too."""
    with pd.ExcelWriter(target) as xl:
        for label in valuation.LABELS:
            df[df["label"] == label].to_excel(xl, sheet_name=label[:31], index=False)
        for name, col in screens.SCREENS.items():
            df[df[col]].to_excel(xl, sheet_name=name, index=False)


def cmd_export(args) -> int:
    s = load_settings()
    df = valuation.load(s)
    path = Path(args.xlsx or REPO_ROOT / "output" / f"screen_{dt.date.today().isoformat()}.xlsx")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_sheets(df.drop(columns=["price_date"], errors="ignore"), path)
    print(f"Wrote {path}")
    if args.csv:
        df.to_csv(args.csv, index=False)
        print(f"Wrote {args.csv}")
    return 0


def cmd_track(args) -> int:
    s = load_settings()
    out = track.refresh(s)
    if out.empty:
        print("Nothing to track yet: no snapshot has the screen columns. Run `value-screener refresh`.")
        return 0
    show = out.copy()
    for c in ("ret", "ret_if_gone_lost", *track.BENCHMARKS, "excess_vs_spy"):
        show[c] = show[c].map(lambda v: f"{v:+.1%}" if pd.notna(v) else "")
    show["days"] = show["days"].map(lambda v: f"{v:.0f}" if pd.notna(v) else "not bought yet")
    print(show.to_string(index=False))
    print("\nEqual weight and before costs, each cohort held since its first close after the snapshot.")
    if not (out["days"] >= 365).any():
        print("Under a year of results: this is a record being kept, not evidence of anything.")
    return 0


def cmd_panel(args) -> int:
    s = load_settings()
    dates = [dt.date.fromisoformat(d) for d in args.dates] if args.dates \
        else panel.quarter_ends(s.backtest_start, dt.date.today())
    panel.build(s, dates)
    return 0


def _print_gate(name: str, r: dict) -> None:
    print(f"\n{name}: {r['common']} tickers in both, {len(r['live_not_replayed'])} live only")
    print(f"  Market caps: {r['caps_close']}/{r['caps_compared']} within {panel.CAP_CLOSE:.0%}, "
          f"{len(r['caps_wrong'])} off by over {panel.CAP_WRONG:.0%}, "
          f"{r['caps_lost']} lost, {r['caps_gained']} gained")
    print(f"  Labels match: {r['label_match']:.1%}")
    print("  Fields match: " + ", ".join(f"{k} {v:.1%}" for k, v in r["fields"].items()))
    for c in screens.SCREENS.values():
        o = r[c]
        print(f"  {c}: live {o['live']}, replay {o['replay']}, both {o['both']}; "
              f"live only {o['live_only'][:15]}; replay only {o['replay_only'][:15]}")
    print(f"  Most wrong caps: {r['caps_wrong'].head(15).round(1).to_dict()}")


def cmd_panel_gate(args) -> int:
    same, full = panel.gate(load_settings(), dt.date.fromisoformat(args.date) if args.date else None)
    _print_gate("Replay with live prices and caps", same)
    _print_gate("Replay with past-date prices and caps", full)
    return 0


def _average_drag(s, client) -> dict[str, float]:
    """Each screen's average yearly failure drag (10c) over the years it's judged on."""
    missing, summ = survivorship.run(s, client, verbose=False)
    drag = survivorship.failure_drag(s, missing, survivorship.public_floats(client), summ)
    out = {}
    for c, start in s.backtest_starts.items():
        rows = drag[drag["year"] > dt.date.fromisoformat(start).year]
        out[c] = float(rows[f"{c}_drag"].mean()) if len(rows) else float("nan")
    return out


def cmd_backtest(args) -> int:
    s = load_settings()
    drag = _average_drag(s, sec.client_from_settings(s))
    results = returns.run(s, drag)
    returns.save(s, results)
    pct = lambda v: f"{v:+.1%}" if pd.notna(v) else ""
    for label, r in results.items():
        print(f"\n=== {label} ({r.periods['screen']['start'].iloc[0].date()} to {r.periods['screen']['stop'].iloc[-1].date()}) ===")
        show = r.summary.copy()
        for c in show:
            if c.startswith(("cagr", "max_", "worst", "excess", "cost")):
                show[c] = show[c].map(pct)
            elif c.startswith("t_") or c in ("avg_stocks", "turnover_per_year"):
                show[c] = show[c].round(2)
        print(show.to_string(index=False))
        print("\nBy year:")
        print(r.yearly.map(pct).to_string())
        if len(r.sensitivity):
            sens = r.sensitivity[["variant", "cagr", "avg_stocks", "excess_SPY", f"excess_{returns.UNIVERSE}",
                                  f"t_{returns.UNIVERSE}", f"excess_{returns.BASELINE}"]].copy()
            for c in sens.columns[1:]:
                sens[c] = sens[c].round(2) if c.startswith(("t_", "avg")) else sens[c].map(pct)
            print("\nSensitivity (excess over the same-universe EW uses each variant's own universe):")
            print(sens.to_string(index=False))
        print(f"\nVerdict: {r.verdicts}")
    return 0


def cmd_survivorship(args) -> int:
    s = load_settings()
    missing, summ = survivorship.run(s, sec.client_from_settings(s))
    print(f"{len(missing)} delisted 10-K filers missing from today's universe: "
          f"{int(missing['acquired'].sum())} after a merger filing, {int(missing['reorganized'].sum())} reorganized, "
          f"{int(missing['spac'].sum())} SPACs, {int(missing['failed'].sum())} failed or other, "
          f"{int(missing['still_filing'].sum())} still filing")
    show = summ.copy()
    for c in ("hole_share",):
        show[c] = show[c].map(lambda v: f"{v:.1%}")
    for c in [c for c in show if c.endswith("_missing") or c.endswith("_failed")]:
        show[c] = show[c].round(1)
    print(show.to_string(index=False))
    drag = survivorship.failure_drag(s, missing, survivorship.public_floats(sec.client_from_settings(s)), summ)
    print("\nYearly drag if every failure the screens would have held went to zero:")
    print(drag.round(3).to_string(index=False))
    return 0


COVERAGE_METRICS = ["market_cap", "pe", "ps", "pb", "ev_ebitda", "fcf_yield", "p_ffo"]


def coverage_report(df: pd.DataFrame) -> pd.DataFrame:
    """Share of stocks with each metric, by industry group (plus an overall row)."""
    have = df[COVERAGE_METRICS].notna()
    have["labelled"] = df["label"] != "Insufficient data"
    by = have.groupby(df["industry_group"].fillna("Unclassified")).mean()
    by.insert(0, "stocks", df.groupby(df["industry_group"].fillna("Unclassified")).size())
    total = have.mean().to_frame("ALL").T
    total.insert(0, "stocks", len(df))
    return pd.concat([total, by.sort_values("stocks", ascending=False)])


def cmd_coverage(args) -> int:
    rep = coverage_report(valuation.load(load_settings()))
    pct = rep.drop(columns="stocks").map(lambda v: f"{v:.0%}")
    pct.insert(0, "stocks", rep["stocks"])
    print(pct.to_string())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="value-screener",
                                description="Industry-relative cheap / fair / expensive stock labels.")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("refresh", help="Run the full pipeline (universe -> labels).")
    r.add_argument("--tickers", nargs="+", help="Quick subset run for these tickers only.")
    r.set_defaults(func=cmd_refresh)

    sc = sub.add_parser("screen", help="Filter and print the latest screen.")
    sc.add_argument("--label", choices=sorted([*LABEL_ALIASES, *SCREEN_ALIASES]),
                    help="Only this label or screen.")
    sc.add_argument("--turning", action="store_true", help="Only names with revenue up and price above 200-day.")
    sc.add_argument("--industry", help="Industry group contains this text (e.g. Software).")
    sc.add_argument("--sector", help="Sector contains this text (e.g. Energy).")
    sc.add_argument("--min-mcap", type=float, help="Minimum market cap in USD (e.g. 1e9).")
    sc.add_argument("--quality-only", action="store_true", help="Only names passing quality.")
    sc.add_argument("--top", type=int, default=50, help="Rows to print (default 50).")
    sc.set_defaults(func=cmd_screen)

    ex = sub.add_parser("export", help="Write the screen to Excel (one sheet per label and screen).")
    ex.add_argument("--xlsx", help="Output path (default output/screen_YYYY-MM-DD.xlsx).")
    ex.add_argument("--csv", help="Also write the full screen as CSV here.")
    ex.set_defaults(func=cmd_export)

    tr = sub.add_parser("track", help="Record the screens' cohorts and their returns since.")
    tr.set_defaults(func=cmd_track)

    pa = sub.add_parser("panel", help="Build point-in-time screens, one file per date; rerun to resume.")
    pa.add_argument("--dates", nargs="+", help="YYYY-MM-DD dates (default: every quarter end).")
    pa.set_defaults(func=cmd_panel)

    pg = sub.add_parser("panel-gate", help="Check that a replay reproduces the live screen.")
    pg.add_argument("--date", help="YYYY-MM-DD (default: the live screen's price date).")
    pg.set_defaults(func=cmd_panel_gate)

    bt = sub.add_parser("backtest", help="Phase 10d: the screens' returns on the panel, after costs.")
    bt.set_defaults(func=cmd_backtest)

    sv = sub.add_parser("survivorship", help="Count companies listed at each panel date but missing from it.")
    sv.set_defaults(func=cmd_survivorship)

    cv = sub.add_parser("coverage",help="Percent of stocks with each metric, by industry.")
    cv.set_defaults(func=cmd_coverage)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

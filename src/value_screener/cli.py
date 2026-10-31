"""Phase 6 — command line: ``value-screener refresh | screen | export``."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import pandas as pd

from . import errors, fundamentals, industry, prices, sec, universe, valuation
from .config import REPO_ROOT, load_settings

LABEL_ALIASES = {"undervalued": "Undervalued", "fair": "Fair", "overvalued": "Overvalued",
                 "insufficient": "Insufficient data"}
SHOW = ["ticker", "name", "industry_group", "label", "composite_pct", "market_cap", "pe", "ps",
        "pb", "ev_ebitda", "fcf_yield", "p_ffo", "dividend_yield", "roe", "quality_issues"]


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
    screen = valuation.refresh(s, uni, ind, fund, px)

    n_err = max(0, sum(1 for _ in errors.errors_path(s.data_dir).open(encoding="utf-8")) - 1)
    print(f"Done in {time.time() - t0:.0f}s: {len(screen)} stocks, {client.calls} SEC requests, "
          f"{n_err} logged errors -> {errors.errors_path(s.data_dir)}")
    return 0


def filter_screen(df: pd.DataFrame, *, label=None, industry_q=None, sector_q=None,
                  min_mcap=None, quality_only=False) -> pd.DataFrame:
    if label:
        df = df[df["label"] == LABEL_ALIASES.get(label.lower(), label)]
    if industry_q:
        df = df[df["industry_group"].fillna("").str.contains(industry_q, case=False, regex=False)]
    if sector_q:
        df = df[df["sector"].fillna("").str.contains(sector_q, case=False, regex=False)]
    if min_mcap:
        df = df[df["market_cap"] >= min_mcap]
    if quality_only:
        df = df[df["quality_pass"]]
    return df


def _fmt(df: pd.DataFrame) -> str:
    out = df.reindex(columns=SHOW).copy()
    out["name"] = out["name"].fillna("").str.slice(0, 28)
    out["market_cap"] = (out["market_cap"] / 1e9).map(lambda v: f"{v:,.1f}B" if pd.notna(v) else "")
    for c in ("composite_pct", "fcf_yield", "dividend_yield", "roe"):
        out[c] = out[c].map(lambda v: f"{v:.0%}" if pd.notna(v) else "")
    for c in ("pe", "ps", "pb", "ev_ebitda", "p_ffo"):
        out[c] = out[c].map(lambda v: f"{v:.1f}" if pd.notna(v) else "")
    return out.to_string(index=False)


def cmd_screen(args) -> int:
    s = load_settings()
    df = filter_screen(valuation.load(s), label=args.label, industry_q=args.industry,
                       sector_q=args.sector, min_mcap=args.min_mcap, quality_only=args.quality_only)
    df = df.sort_values("composite_pct", ascending=args.label != "overvalued")
    print(_fmt(df.head(args.top)))
    print(f"\n{len(df)} matching stocks")
    return 0


def cmd_export(args) -> int:
    s = load_settings()
    df = valuation.load(s)
    path = Path(args.xlsx or REPO_ROOT / "output" / f"screen_{dt.date.today().isoformat()}.xlsx")
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path) as xl:
        for label in valuation.LABELS:
            part = df[df["label"] == label].drop(columns=["price_date"], errors="ignore")
            part.to_excel(xl, sheet_name=label[:31], index=False)
    print(f"Wrote {path}")
    if args.csv:
        df.to_csv(args.csv, index=False)
        print(f"Wrote {args.csv}")
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
    sc.add_argument("--label", choices=sorted(LABEL_ALIASES), help="Only this label.")
    sc.add_argument("--industry", help="Industry group contains this text (e.g. Software).")
    sc.add_argument("--sector", help="Sector contains this text (e.g. Energy).")
    sc.add_argument("--min-mcap", type=float, help="Minimum market cap in USD (e.g. 1e9).")
    sc.add_argument("--quality-only", action="store_true", help="Only names passing quality.")
    sc.add_argument("--top", type=int, default=50, help="Rows to print (default 50).")
    sc.set_defaults(func=cmd_screen)

    ex = sub.add_parser("export", help="Write the screen to Excel (one sheet per label).")
    ex.add_argument("--xlsx", help="Output path (default output/screen_YYYY-MM-DD.xlsx).")
    ex.add_argument("--csv", help="Also write the full screen as CSV here.")
    ex.set_defaults(func=cmd_export)

    cv = sub.add_parser("coverage", help="Percent of stocks with each metric, by industry.")
    cv.set_defaults(func=cmd_coverage)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

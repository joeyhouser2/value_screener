"""Phase 10b — the screen as it would have looked on a past date. One Parquet per date, resumable.

A replay of the live pipeline, not a copy: each company's filings are cut to what was filed by
the date (restatements undone), then go through :func:`fundamentals.parse_company` and
:func:`valuation.build_screen`. A filing made on the date counts, since positions are bought at
the next close.

- Who: today's universe with a filing by the date and a close within ``trend.MAX_AGE_DAYS``.
  So a reused ticker's older prices never meet the new company's figures. Companies that have
  since delisted are missing altogether, which is Phase 10c's subject.
- Market cap: shares known then x that day's printed close, with any split since the share
  count's date applied to the shares. Today's Nasdaq and Yahoo cap fallbacks have no history,
  so what they would have corrected goes wrong or missing here: :func:`gate` counts it.
  :func:`past_shares` stands in for them where it can, from what was filed by the date:

  - Net income / diluted EPS fills a share count that is missing or stale, as filings leave
    out per-class counts (META, UPS). It is too rough to overrule a fresh count: trailing EPS
    mixes periods around a merger or a split, so it is unused for 15 months after a split,
    and rounding swamps it when EPS is small.
  - A count about 1,000 or 1,000,000 times that figure is a scaling slip on the cover page
    (Fortinet 2014, Alcoa 2019), so it is scaled back.
  - A fresh count under half that figure may be one class of several (HEI), which would make
    the stock look cheap, so that cap is left blank.
  - A cap over ``past_cap_max_multiple`` x the larger of revenue and equity is left blank:
    Yahoo misses some reverse splits of very old penny stocks (Atlas Lithium 2014).
  - 20-F and 40-F filers get no cap: their shares are ordinaries, their price an ADR's, and
    the ratio isn't in the filings. So the panel's screens cover US-form filers only. Some
    10-K filers trade as ADSs too (ONC, ZLAB); nothing in the filings shows it, so those are wrong.

- SIC codes and ``ticker_overrides`` are today's.
"""

from __future__ import annotations

import datetime as dt
import functools
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from .. import cache, errors, fundamentals, history, industry, prices, trend, universe, valuation
from ..config import Settings
from . import price_history

CAP_CLOSE = 0.05            # a replayed market cap this near the live one counts as matching
CAP_WRONG = 0.50            # this far away means the share count, not the day's price, is off
GATE_FIELDS = ("pe", "p_tbv", "ev_sales", "pe10", "margin_position", "f_score", "off_52w_high")
GATE_FIELD_TOL = 0.02
ANNUAL_FORMS = {"10-K", "10-KT", "20-F", "40-F"}
FOREIGN_FORMS = {"20-F", "40-F"}
SCALE_SLIP_TOL = 1.5        # how near a power of 1,000 a count's excess must be to count as a slip
MAX_SHARE_SHORTFALL = 2.0   # a fresh count this far under net income / EPS may be one class of several


def as_of(slim: dict, date: dt.date) -> dict:
    """``slim`` with only the rows filed by ``date``. ISO strings sort as dates."""
    cut = date.isoformat()
    tags = {tag: kept for tag, rows in slim["tags"].items()
            if (kept := [r for r in rows if r[3] and r[3] <= cut])}
    return {**slim, "tags": tags}


def is_foreign_filer(slim: dict) -> bool:
    """Whether the latest annual report in ``slim`` is a 20-F or 40-F. Companies do switch."""
    annual = [(r[3], r[4].split("/")[0]) for rows in slim["tags"].values() for r in rows
              if r[3] and r[4] and r[4].split("/")[0] in ANNUAL_FORMS]
    return bool(annual) and max(annual)[1] in FOREIGN_FORMS


def _replay_company(paths: list[Path], tags_cfg: dict, date: dt.date, stale_months: int, history_years: int):
    """Process-pool worker: (TTM row, annual history) as of ``date``, or None if nothing was filed."""
    slims = [s for s in (cache.read_json(p, float("inf")) for p in paths) if s and s != cache.MISSING]
    if not slims:
        return None
    seen = as_of(fundamentals.merge_slims(slims), date)
    if not seen["tags"]:
        return None
    row, hist, _ = fundamentals.parse_company([seen], tags_cfg, stale_months, history_years, today=date)
    row["foreign_filer"] = is_foreign_filer(seen)
    return row, hist


def replay_fundamentals(settings: Settings, uni: pd.DataFrame, date: dt.date) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(fundamentals per ticker, long history) from the cached filings; no network."""
    tags_cfg = fundamentals.load_tags(settings.config_dir)
    predecessors = fundamentals.load_predecessors(settings.config_dir)
    facts = settings.cache_dir / "sec" / "facts" / fundamentals.tags_fingerprint(tags_cfg)
    path = lambda c: facts / f"CIK{int(c):010d}.json.gz"
    ciks = [int(c) for c in uni["cik"].dropna().unique()]
    work = functools.partial(_replay_company, tags_cfg=tags_cfg, date=date,
                             stale_months=settings.stale_months, history_years=settings.history_years)
    rows, hist = {}, {}
    with ProcessPoolExecutor(max(1, (os.cpu_count() or 2) - 1)) as pool:
        futures = {c: pool.submit(work, [*(path(p) for p in predecessors.get(c, [])), path(c)]) for c in ciks}
        for c, fut in futures.items():
            try:
                got = fut.result()
            except Exception as exc:  # noqa: BLE001 - logged, run continues
                errors.log(settings.data_dir, "panel", str(c), f"{date}: {exc}")
                continue
            if got:
                rows[c] = got[0]
                if got[1] is not None:
                    hist[c] = got[1]
    out = [{"ticker": t, **rows[int(c)]} for t, c in zip(uni["ticker"], uni["cik"])
           if pd.notna(c) and int(c) in rows]
    return pd.DataFrame(out), history.to_frame(hist)


def past_closes(frames: dict[str, pd.DataFrame], date: dt.date) -> pd.DataFrame:
    """ticker, price (as printed then), price_date, for tickers with a recent enough close."""
    day = pd.Timestamp(date)
    rows = []
    for t, df in frames.items():
        i = df.index.searchsorted(day, side="right") - 1
        if i >= 0 and (day - df.index[i]).days <= trend.MAX_AGE_DAYS and df["close"].iloc[i] > 0:
            rows.append({"ticker": t, "price": price_history.unadjusted(df, df.index[i]),
                         "price_date": df.index[i].date()})
    return pd.DataFrame(rows, columns=["ticker", "price", "price_date"])


def past_shares(fund: pd.DataFrame, frames: dict[str, pd.DataFrame], settings: Settings, date: dt.date) -> pd.DataFrame:
    """``fund`` with its share counts restated for splits, corrected, or blanked (see the module docstring)."""
    fund = fund.copy()
    column = lambda c: pd.to_numeric(fund[c], errors="coerce") if c in fund else pd.Series(np.nan, index=fund.index)
    ends = pd.to_datetime(fund["shares_outstanding_end"], errors="coerce")
    factor = [price_history.split_factor(frames[t]["split"], e, date, shares_only=True)
              if t in frames and pd.notna(e) else 1.0 for t, e in zip(fund["ticker"], ends)]
    window = pd.Timestamp(date) - pd.DateOffset(months=settings.stale_months)
    recent_split = pd.Series([t in frames and price_history.split_factor(frames[t]["split"], window, date, shares_only=True) != 1.0
                              for t in fund["ticker"]], index=fund.index)

    income, eps = column("net_income"), column("eps_diluted")
    same_period = "net_income_end" in fund and "eps_diluted_end" in fund \
        and fund["net_income_end"].eq(fund["eps_diluted_end"])
    rough = (income / eps).where((income / eps > 0) & same_period)
    # Scaling slips first: on the count as filed, and on any EPS, since they are so large.
    filed = column("shares_outstanding")
    power = np.round(np.log(filed / rough) / np.log(1000)).clip(lower=0).fillna(0)
    slip = (power >= 1) & ((filed / rough / 1000.0 ** power).between(1 / SCALE_SLIP_TOL, SCALE_SLIP_TOL))
    shares = filed.where(~slip, filed / 1000.0 ** power) * factor

    implied = rough.where((eps.abs() >= settings.past_cap_min_eps) & ~recent_split)
    stale = ends < window
    fill = implied.notna() & (shares.isna() | stale)
    too_low = implied.notna() & ~fill & (shares < implied / MAX_SHARE_SHORTFALL)
    shares = shares.where(~fill, implied).where(~too_low)
    fund.loc[fill, "shares_outstanding_end"] = pd.Timestamp(date)

    if "foreign_filer" in fund:
        shares = shares.where(~fund["foreign_filer"].fillna(False).astype(bool))
    fund["shares_outstanding"] = shares
    return fund


def past_market_caps(closes: pd.DataFrame, fund: pd.DataFrame, frames: dict[str, pd.DataFrame],
                     settings: Settings, date: dt.date) -> pd.DataFrame:
    """The live cap checks on the shares from :func:`past_shares`."""
    df = prices.with_market_cap(closes, past_shares(fund, frames, settings, date), None, today=date,
                                stale_months=settings.stale_months)
    f = fund.set_index("ticker")
    size = lambda c: pd.to_numeric(df["ticker"].map(f[c]), errors="coerce").abs() if c in f else pd.Series(np.nan, index=df.index)
    absurd = df["market_cap"] > settings.past_cap_max_multiple * pd.concat([size("revenue"), size("equity")], axis=1).max(axis=1)
    df.loc[df["needs_check"] | absurd, "market_cap"] = np.nan
    return df


def past_trend(frames: dict[str, pd.DataFrame], date: dt.date) -> pd.DataFrame:
    """The live trend figures on the closes up to ``date``."""
    day = pd.Timestamp(date)
    start = day - pd.Timedelta(days=trend.LOOKBACK_DAYS)
    rows = []
    for t, df in frames.items():
        w = df[(df.index >= start) & (df.index <= day)]
        if len(w):
            data = pd.concat({"Close": w[["close"]].set_axis([t], axis=1),
                              "Adj Close": w[["adj_close"]].set_axis([t], axis=1),
                              "Stock Splits": w[["split"]].set_axis([t], axis=1)}, axis=1)
            rows.append(trend.from_download(data, [t], date))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=trend.COLUMNS)


def _screen_from(settings: Settings, date: dt.date, frames: dict[str, pd.DataFrame], uni: pd.DataFrame,
                 ind: pd.DataFrame, fund: pd.DataFrame, hist: pd.DataFrame) -> pd.DataFrame:
    closes = past_closes({t: frames[t] for t in fund["ticker"] if t in frames}, date)
    fund = fund[fund["ticker"].isin(closes["ticker"])]
    caps = past_market_caps(closes, fund, frames, settings, date)
    return valuation.build_screen(uni[uni["ticker"].isin(fund["ticker"])], ind, fund, caps, settings, hist,
                                  past_trend({t: frames[t] for t in fund["ticker"]}, date))


def build_date(settings: Settings, date: dt.date, frames: dict[str, pd.DataFrame],
               uni: pd.DataFrame, ind: pd.DataFrame) -> pd.DataFrame:
    fund, hist = replay_fundamentals(settings, uni, date)
    if fund.empty:
        return pd.DataFrame(columns=valuation.SCREEN_COLUMNS)
    return _screen_from(settings, date, frames, uni, ind, fund, hist)


def panel_path(settings: Settings, date: dt.date) -> Path:
    return settings.data_dir / "backtest" / "panel" / f"screen_{date.isoformat()}.parquet"


def quarter_ends(start: str, end: dt.date) -> list[dt.date]:
    return [d.date() for d in pd.date_range(start, end, freq="QE")]


def price_start(settings: Settings) -> dt.date:
    """Early enough for the first date's trend figures."""
    return dt.date.fromisoformat(settings.backtest_start) - dt.timedelta(days=trend.LOOKBACK_DAYS + 7)


def build(settings: Settings, dates: list[dt.date], *, source=None, verbose: bool = True) -> None:
    """Build each date not yet on disk; rerun to resume after an interruption."""
    uni, ind = universe.load(settings), industry.load(settings)
    tickers = uni["ticker"].tolist()
    got = price_history.fetch(settings, tickers, price_start(settings), source)
    frames = price_history.load(settings, tickers)
    if verbose:
        print(f"Price history: {len(frames)}/{len(tickers)} tickers ({got} fetched now)", flush=True)
    for date in dates:
        path = panel_path(settings, date)
        if path.exists():
            continue
        df = build_date(settings, date, frames, uni, ind)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        df.to_parquet(tmp, index=False)
        tmp.replace(path)
        if verbose:
            print(f"{date}: {len(df)} stocks, {int(df['deep_value'].fillna(False).astype(bool).sum())} deep value, "
                  f"{int(df['cyclical_trough'].fillna(False).astype(bool).sum())} cyclical trough", flush=True)


def _overlap(replay: pd.Index, live: pd.Index) -> dict:
    a, b = set(replay), set(live)
    return {"live": len(b), "replay": len(a), "both": len(a & b),
            "live_only": sorted(b - a), "replay_only": sorted(a - b)}


def compare(replay: pd.DataFrame, live: pd.DataFrame) -> dict:
    """How closely a replay reproduces a live screen, on the tickers in both."""
    r, l = replay.set_index("ticker"), live.set_index("ticker")
    common = r.index.intersection(l.index)
    r, l = r.loc[common], l.loc[common]
    both = r["market_cap"].notna() & l["market_cap"].notna()
    gap = (r["market_cap"] / l["market_cap"] - 1).abs()[both]
    fields = {}
    for c in GATE_FIELDS:
        a, b = pd.to_numeric(r[c], errors="coerce"), pd.to_numeric(l[c], errors="coerce")
        same = (a.isna() & b.isna()) | ((a - b).abs() <= GATE_FIELD_TOL * b.abs())
        fields[c] = float(same.mean())
    members = lambda df, c: df.index[df[c].fillna(False).astype(bool)]
    return {"common": len(common), "live_not_replayed": sorted(set(live["ticker"]) - set(replay["ticker"])),
            "caps_compared": int(both.sum()), "caps_close": int((gap <= CAP_CLOSE).sum()),
            "caps_wrong": gap[gap > CAP_WRONG].sort_values(ascending=False),
            "caps_lost": int((l["market_cap"].notna() & r["market_cap"].isna()).sum()),
            "caps_gained": int((l["market_cap"].isna() & r["market_cap"].notna()).sum()),
            "label_match": float((r["label"] == l["label"]).mean()), "fields": fields,
            "deep_value": _overlap(members(r, "deep_value"), members(l, "deep_value")),
            "cyclical_trough": _overlap(members(r, "cyclical_trough"), members(l, "cyclical_trough"))}


def gate(settings: Settings, date: dt.date | None = None, *, source=None) -> tuple[dict, dict]:
    """Replay the live screen's date twice: with today's prices and caps, which tests the
    replay alone, then with the past-date ones, which also tests the market cap method."""
    live = valuation.load(settings)
    date = date or pd.to_datetime(live["price_date"]).max().date()
    uni, ind = universe.load(settings), industry.load(settings)
    tickers = uni["ticker"].tolist()
    price_history.fetch(settings, tickers, price_start(settings), source)
    frames = price_history.load(settings, tickers)
    fund, hist = replay_fundamentals(settings, uni, date)
    same_prices = valuation.build_screen(uni[uni["ticker"].isin(fund["ticker"])], ind, fund, prices.load(settings),
                                         settings, hist, trend.load(settings))
    return compare(same_prices, live), compare(_screen_from(settings, date, frames, uni, ind, fund, hist), live)

"""Phase 10d — the screens' returns on the point-in-time panel, after costs, against benchmarks.

Rules (``backtest_*`` in settings.yaml) were fixed before any result was seen:

- Rebalanced quarterly: members are read from the panel at each quarter end and bought at the
  next close. Equal weight; a holding of H quarters averages the last H cohorts.
- Costs one way by market cap (``backtest_cost_caps`` / ``backtest_cost_bps``), charged on the
  weight traded: names bought, sold, and the trim or top-up back to equal weight. Stocks with
  a median daily dollar volume under ``backtest_min_dollar_volume`` can't be bought.
- Returns from Yahoo's dividend-adjusted close. A stock that stops trading holds its last price
  until the next rebalance, as if sold there. The panel only has today's universe, so failures
  are missing: Phase 10c sizes that hole.
- Benchmarks: SPY, RSP and IWD over the same days, no costs; every eligible stock at equal
  weight (shares the panel's survivorship bias, so the fairest test); and the cheapest 10% by
  earnings yield (the dumb alternative). Both of those pay the same costs as a screen.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .. import screens
from ..config import Settings
from . import panel, price_history

ETFS = ("SPY", "RSP", "IWD")
UNIVERSE = "Same-universe EW"
BASELINE = "Cheapest 10% E/Y"
SCREEN_COLS = ("deep_value", "cyclical_trough")
FINANCIALS = "Financials"
BOOT_SAMPLES = 5000

# One step each way per threshold (see PLAN.md, 10d): fixed with the rules, before any result.
STEPS = {
    "deep_value": {"deep_value_pct": (0.15, 0.25), "deep_value_min_f": (5, 7), "deep_value_max_p_tbv": (2.0, 4.0),
                   "max_share_cagr": (0.03, 0.08)},
    "cyclical_trough": {"trough_pct": (0.20, 0.40), "trough_max_margin_position": (0.25, 0.40),
                        "trough_min_f": (3, 5), "max_share_cagr": (0.03, 0.08)},
}


@dataclass(frozen=True)
class Variant:
    """Which stocks a strategy holds: ``pick`` on each panel date, then the shared filters."""
    name: str
    screen: str | None = None            # a SCREEN_COLS name; None for the universe or baseline
    baseline: bool = False
    overrides: tuple = ()                # settings changed for the screen, as (name, value)
    turning: bool = False
    hold: int = 1                        # quarters each cohort is held
    min_cap: float = 0.0
    drop_sectors: tuple = ()

    def universe_twin(self) -> "Variant":
        """The same-universe equal weight under this variant's own filters."""
        return Variant(UNIVERSE, hold=self.hold, min_cap=self.min_cap, drop_sectors=self.drop_sectors)


def cost_bps(caps: pd.Series, settings: Settings) -> pd.Series:
    """One-way cost per stock in basis points; an unknown cap pays the highest."""
    idx = np.searchsorted(np.asarray(settings.backtest_cost_caps), caps.fillna(0).to_numpy(), side="right")
    return pd.Series(np.asarray(settings.backtest_cost_bps, dtype=float)[idx], index=caps.index)


def load_panels(settings: Settings, dates: list[dt.date]) -> dict[dt.date, pd.DataFrame]:
    out = {}
    for d in dates:
        path = panel.panel_path(settings, d)
        if path.exists():
            out[d] = pd.read_parquet(path)
    return out


def load_prices(settings: Settings, tickers: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(dividend-adjusted closes, rolling median dollar volume), one column per ticker."""
    frames = price_history.load(settings, sorted(set(tickers)))
    # Carried forward, so a stock that stops trading holds its last price.
    adj = pd.DataFrame({t: f["adj_close"] for t, f in frames.items()}).sort_index().ffill()
    dollars = pd.DataFrame({t: f["close"] * f["volume"] for t, f in frames.items()}).sort_index()
    n = settings.backtest_volume_days
    return adj, dollars.rolling(n, min_periods=n // 2).median()


def market_days(adj: pd.DataFrame) -> pd.DatetimeIndex:
    """SPY's trading days, without the last bar: the cache's last bar may be intraday."""
    return adj["SPY"].dropna().index[:-1]


def entry_day(days: pd.DatetimeIndex, date) -> pd.Timestamp | None:
    later = days[days > pd.Timestamp(date)]
    return later[0] if len(later) else None


def picks(p: pd.DataFrame, v: Variant, settings: Settings, volume: pd.Series) -> pd.Index:
    """Tickers ``v`` buys from one panel date, given each ticker's dollar volume then."""
    cap = pd.to_numeric(p["market_cap"], errors="coerce")
    ok = (cap >= max(settings.screen_min_market_cap, v.min_cap)) & ~p["stale"].fillna(True).astype(bool)
    ok &= p["ticker"].map(volume).fillna(0).to_numpy() >= settings.backtest_min_dollar_volume
    if v.drop_sectors:
        ok &= ~p["sector"].isin(v.drop_sectors)
    if v.screen:
        if v.overrides:
            redone = screens.add_screens(p, dataclasses.replace(settings, **dict(v.overrides)))
            # The panel has no equity_end, so the spin-off check is carried over from the build.
            spun = p[f"{v.screen}_reasons"].fillna("").str.contains("spin-off")
            ok &= redone[v.screen].fillna(False).astype(bool) & ~spun
        else:
            ok &= p[v.screen].fillna(False).astype(bool)
        if v.turning:
            ok &= p["turning"].fillna(False).astype(bool)
    elif v.baseline:
        pe = pd.to_numeric(p["pe"], errors="coerce")
        cheap = p[ok & (pe > 0)]
        n = round(int(ok.sum()) * settings.track_baseline_pct)
        return pd.Index(cheap.loc[pd.to_numeric(cheap["pe"]).nsmallest(n).index, "ticker"])
    return pd.Index(p.loc[ok, "ticker"])


def simulate(v: Variant, settings: Settings, panels: dict, adj: pd.DataFrame, volume: pd.DataFrame,
             start: dt.date) -> tuple[pd.DataFrame, pd.Series]:
    """(one row per quarter: dates, stocks, gross, cost, net, turnover; daily value after costs)."""
    days = market_days(adj)
    dates = [d for d in sorted(panels) if d >= start and entry_day(days, d) is not None]
    cohorts: list[pd.Index] = []
    caps: dict[str, float] = {}
    held = pd.Series(dtype=float)       # weights at the end of the last quarter, after drift
    rows, value, nav = [], 1.0, []
    for k, d in enumerate(dates):
        p = panels[d]
        vol_then = volume.loc[:pd.Timestamp(d)]
        vol_now = vol_then.iloc[-1] if len(vol_then) else pd.Series(dtype=float)
        cohorts = (cohorts + [picks(p, v, settings, vol_now)])[-v.hold:]
        caps.update(pd.to_numeric(p.set_index("ticker")["market_cap"], errors="coerce").dropna().to_dict())
        go, stop = entry_day(days, d), (entry_day(days, dates[k + 1]) if k + 1 < len(dates) else days[-1])
        target = pd.Series(dtype=float)
        live = [c for c in cohorts if len(c)]
        at_go = adj.loc[go]
        for c in live:
            priced = c[at_go.reindex(c).notna().to_numpy()]
            if len(priced):
                target = target.add(pd.Series(1 / (len(priced) * len(live)), index=priced), fill_value=0)
        if target.sum():
            target /= target.sum()
        trade = target.sub(held, fill_value=0).abs()
        cost = float((trade * cost_bps(pd.Series(caps).reindex(trade.index), settings) / 1e4).sum())
        if target.empty:
            gross, path = 0.0, pd.Series(1.0, index=days[(days >= go) & (days <= stop)])
            held = target
        else:
            px = adj.loc[go:stop, target.index]
            growth = px / px.iloc[0]
            path = growth @ target
            gross = float(path.iloc[-1] - 1)
            held = growth.iloc[-1] * target / path.iloc[-1]
        nav.append(value * (1 - cost) * path)
        value *= (1 - cost) * (1 + gross)
        rows.append({"date": d, "start": go, "stop": stop, "stocks": len(target), "gross": gross, "cost": cost,
                     "net": (1 - cost) * (1 + gross) - 1, "turnover": float(trade.sum() / 2)})
    nav = pd.concat(nav) if nav else pd.Series(dtype=float)
    return pd.DataFrame(rows), nav[~nav.index.duplicated(keep="last")]


def etf(ticker: str, adj: pd.DataFrame, periods: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """An ETF over the same quarters, no costs."""
    s = adj[ticker]
    out = periods[["date", "start", "stop"]].copy()
    out["net"] = [float(s[b] / s[a] - 1) for a, b in zip(out["start"], out["stop"])]
    out["gross"], out["cost"], out["stocks"], out["turnover"] = out["net"], 0.0, 1, 0.0
    return out, s.loc[out["start"].iloc[0]:out["stop"].iloc[-1]] / s[out["start"].iloc[0]]


def year_of(periods: pd.DataFrame) -> pd.Series:
    """The calendar year a quarter's returns belong to: the one after its rebalance date."""
    return (pd.to_datetime(periods["date"]) + pd.Timedelta(days=1)).dt.year


def annualized(periods: pd.DataFrame) -> float:
    if periods.empty:
        return np.nan
    years = (periods["stop"].iloc[-1] - periods["start"].iloc[0]).days / 365.25
    return float((1 + periods["net"]).prod() ** (1 / years) - 1)


def yearly(periods: pd.DataFrame) -> pd.Series:
    return (1 + periods["net"]).groupby(year_of(periods)).prod() - 1


def max_drawdown(nav: pd.Series) -> float:
    return float((nav / nav.cummax() - 1).min()) if len(nav) else np.nan


def block_t(diff: np.ndarray, block: int, samples: int = BOOT_SAMPLES, seed: int = 0) -> float:
    """Mean over its circular-block-bootstrap standard error: quarters next to each other, and
    overlapping holds, aren't independent."""
    diff = np.asarray(diff, dtype=float)
    n = len(diff)
    if n < 2 or not np.isfinite(diff).all():
        return np.nan
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n, size=(samples, -(-n // block)))
    idx = (starts[:, :, None] + np.arange(block)).reshape(samples, -1)[:, :n] % n
    se = diff[idx].mean(axis=1).std(ddof=1)
    return float(diff.mean() / se) if se > 0 else np.nan


def stats(name: str, periods: pd.DataFrame, nav: pd.Series, against: dict[str, pd.DataFrame], block: int) -> dict:
    """Headline figures for one strategy, with its excess and t over each comparison."""
    row = {"strategy": name, "quarters": len(periods), "cagr": annualized(periods),
           "max_drawdown": max_drawdown(nav), "worst_year": float(yearly(periods).min()) if len(periods) else np.nan,
           "avg_stocks": float(periods["stocks"].mean()), "min_stocks": int(periods["stocks"].min()),
           "turnover_per_year": float(periods["turnover"].sum() / max(len(periods) / 4, 1e-9)),
           "cost_per_year": float(periods["cost"].sum() / max(len(periods) / 4, 1e-9))}
    for b, bp in against.items():
        row[f"excess_{b}"] = row["cagr"] - annualized(bp)
        row[f"t_{b}"] = block_t(periods["net"].to_numpy() - bp["net"].to_numpy(), block)
    return row


def subperiod_excess(periods: pd.DataFrame, bench: pd.DataFrame, spans) -> list[float]:
    years = year_of(periods)
    out = []
    for a, b in spans:
        m = ((years >= a) & (years <= b)).to_numpy()
        out.append(annualized(periods[m]) - annualized(bench[m]) if m.any() else np.nan)
    return out


def verdict(main: dict, large: dict, subs: list[float], drag: float, settings: Settings) -> tuple[str, dict]:
    """``pass`` / ``inconclusive`` / ``fail`` by the rules frozen in PLAN.md (10d), and each check."""
    ex_u = main[f"excess_{UNIVERSE}"]
    checks = {
        "beats SPY": main["excess_SPY"] > 0,
        "beats same-universe EW": ex_u > 0,
        "beats cheapest 10%": main[f"excess_{BASELINE}"] > 0,
        "most subperiods": sum(x > 0 for x in subs if pd.notna(x)) >= 2,
        "large caps beat SPY and their EW": large["excess_SPY"] > 0 and large[f"excess_{UNIVERSE}"] > 0,
        "outlasts survivorship drag": pd.notna(drag) and ex_u - drag > 0,
    }
    if not all(checks.values()):
        return "fail", checks
    return ("pass" if ex_u > settings.backtest_hurdle else "inconclusive"), checks


def variants(screen: str, settings: Settings, sectors) -> list[Variant]:
    """The base strategy, then every sensitivity variant fixed with the rules."""
    out = [Variant("base", screen)]
    for k, (lo, hi) in STEPS[screen].items():
        out += [Variant(f"{k}={lo}", screen, overrides=((k, lo),)), Variant(f"{k}={hi}", screen, overrides=((k, hi),))]
    out += [Variant("turning required", screen, turning=True),
            Variant("12-month hold", screen, hold=4), Variant("36-month hold", screen, hold=12),
            Variant("large caps only", screen, min_cap=settings.backtest_large_cap),
            Variant("without Financials", screen, drop_sectors=(FINANCIALS,))]
    out += [Variant(f"without {s}", screen, drop_sectors=(s,)) for s in sorted(sectors) if s != FINANCIALS]
    return out


@dataclass
class Result:
    summary: pd.DataFrame                # one row per strategy on the judged window
    yearly: pd.DataFrame                 # return per year, strategies as columns
    sensitivity: pd.DataFrame            # every variant tried, with its own universe twin
    verdicts: dict = field(default_factory=dict)
    periods: dict = field(default_factory=dict)


def run(settings: Settings, drag: dict[str, float] | None = None, *, verbose: bool = True) -> dict[str, Result]:
    """Every strategy and variant; ``drag`` is each screen's average yearly failure drag (10c)."""
    drag = drag or {}
    dates = panel.quarter_ends(settings.backtest_start, dt.date.today())
    panels = load_panels(settings, dates)
    tickers = sorted({t for p in panels.values() for t in p["ticker"]})
    price_history.fetch(settings, list(ETFS), panel.price_start(settings))
    adj, volume = load_prices(settings, [*tickers, *ETFS])
    sectors = {s for p in panels.values() for s in p["sector"].dropna().unique()}
    block = settings.backtest_block_quarters
    cache: dict = {}

    def sim(v: Variant, start: dt.date):
        key = (v, start)
        if key not in cache:
            cache[key] = simulate(v, settings, panels, adj, volume, start)
        return cache[key]

    results = {}
    windows = [(s, dt.date.fromisoformat(settings.backtest_starts[s])) for s in SCREEN_COLS]
    windows.append(("deep_value_long", dt.date.fromisoformat(settings.backtest_long_start)))
    for label, start in windows:
        screen = "deep_value" if label == "deep_value_long" else label
        base = Variant("base", screen)
        per, nav = sim(base, start)
        if per.empty:
            continue
        comps = {b: etf(b, adj, per) for b in ETFS}
        comps[UNIVERSE] = sim(base.universe_twin(), start)
        comps[BASELINE] = sim(Variant(BASELINE, baseline=True), start)
        against = {b: c[0] for b, c in comps.items()}
        rows = [stats(screen, per, nav, against, block)] + [stats(b, *c, against, block) for b, c in comps.items()]
        year_table = pd.DataFrame({screen: yearly(per), **{b: yearly(c[0]) for b, c in comps.items()}})

        sens = []
        if label != "deep_value_long":
            for v in variants(screen, settings, sectors):
                vp, vn = sim(v, start)
                if vp.empty:
                    continue
                twin = sim(v.universe_twin(), start)[0]
                vs = {"SPY": against["SPY"], UNIVERSE: twin, BASELINE: against[BASELINE]}
                sens.append({"variant": v.name, **stats(screen, vp, vn, vs, max(block, v.hold))})
                if verbose:
                    r = sens[-1]
                    print(f"  {screen} {v.name}: {r['cagr']:.1%}, vs EW {r[f'excess_{UNIVERSE}']:+.1%}", flush=True)
        sens = pd.DataFrame(sens)

        main = rows[0]
        if label == "deep_value_long":
            outcome, checks = "not judged", {}
        else:
            large = sens.set_index("variant").loc["large caps only"].to_dict()
            subs = subperiod_excess(per, against[UNIVERSE], settings.backtest_subperiods[screen])
            outcome, checks = verdict(main, large, subs, drag.get(screen, np.nan), settings)
            checks = {**checks, "subperiod excess": subs, "drag": drag.get(screen, np.nan)}
        results[label] = Result(pd.DataFrame(rows), year_table, sens, {"outcome": outcome, **checks},
                                {"screen": per, **{b: c[0] for b, c in comps.items()}})
    return results


def save(settings: Settings, results: dict[str, Result]) -> None:
    out = settings.data_dir / "backtest" / "results"
    out.mkdir(parents=True, exist_ok=True)
    for label, r in results.items():
        r.summary.to_csv(out / f"{label}_summary.csv", index=False)
        r.yearly.to_csv(out / f"{label}_yearly.csv")
        if len(r.sensitivity):
            r.sensitivity.to_csv(out / f"{label}_sensitivity.csv", index=False)
        for name, per in r.periods.items():
            per.to_csv(out / f"{label}_periods_{name.replace(' ', '_').replace('%', 'pct').replace('/', '')}.csv", index=False)

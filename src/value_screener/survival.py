"""Phase 9c — can the company survive until the cycle turns?

- ``f_score``: Piotroski's 9 binary tests, latest fiscal year vs the one before. A test
  that can't be computed is missing, not failed: ``f_score_n`` says how many ran.
- ``altman_z``: Z for manufacturers (SIC 2000-3999), Z'' for everyone else, with
  ``altman_model`` and ``altman_zone``. The two scales differ, so compare zones rather
  than raw scores. Missing for banks, insurers, REITs, brokers and exchanges, and for
  unclassified balance sheets (homebuilders), which have no working capital. Z'' ignores
  market value and punishes buybacks and deferred revenue, so its "distress" zone
  includes healthy companies (LOW, HCA, NOW): read it with ``coverage_ok``.
- ``interest_coverage``: EBIT / interest expense. ``coverage_ok`` also passes net-cash
  companies and is NA when neither can be judged.
- ``tangible_book`` / ``p_tbv``: equity less goodwill and intangibles.
- ``ncav`` / ``p_ncav``: current assets less total liabilities (same exclusions as Altman).

Balance-sheet figures come from the latest filing, so a 10-Q counts and they can sit
beside today's market cap. A tag is only used when it matches that filing's date: a
company that stops tagging current assets shouldn't pair old ones with new liabilities.
Goodwill is the exception, as some filers report it only in the 10-K; after
``MAX_ANNUAL_LAG_DAYS`` it is taken as written off.

Deviations from Piotroski: debt is total debt, as long-term debt alone isn't in the
history; "no increase" passes the leverage test, so debt-free companies aren't failed;
and shares are weighted diluted, with split-like jumps removed by
:func:`normalized.share_index` (a company returning to profit can fail on antidilution).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import history, normalized, valuation
from .config import Settings

NO_ALTMAN = (*normalized.FINANCIALS, "reit")
# Brokers, exchanges and insurance brokers: much of the balance sheet is client money.
CLIENT_FUND_GROUPS = ("Capital Markets", "Insurance Brokers")
MANUFACTURING_SIC = (2000, 3999)
FY_GAP_DAYS = (335, 395)        # consecutive fiscal years, 52/53-week calendars included
MAX_FY_LAG_DAYS = 400           # latest FY older than this before the latest period -> no score
MAX_BALANCE_LAG_DAYS = 120      # about one missed quarterly filing
SAME_FILING_DAYS = 15
MAX_ANNUAL_LAG_DAYS = 400
SHARE_TOL = 0.005               # rounding in reported share counts
F_TESTS = ("roa", "cfo", "roa_up", "accruals", "leverage", "liquidity", "no_issuance",
           "margin_up", "turnover_up")
COMPANY_COLUMNS = ["f_score", "f_score_n", "f_score_fy"]


def _test(passed, *inputs) -> float:
    """1.0 / 0.0, or NaN when any input is missing."""
    return np.nan if any(pd.isna(x) for x in inputs) else float(bool(passed))


def _consecutive(later, earlier) -> bool:
    return FY_GAP_DAYS[0] <= (later["fy_end"] - earlier["fy_end"]).days <= FY_GAP_DAYS[1]


def f_tests(g: pd.DataFrame, *, special=None) -> dict[str, float]:
    """The nine tests on the last fiscal years of ``g`` (as from ``history.wide``)."""
    rows = [r for _, r in g.sort_values("fy_end").tail(3).iterrows()]
    cur = rows[-1]
    prev = rows[-2] if len(rows) >= 2 and _consecutive(rows[-1], rows[-2]) else None
    before = rows[-3] if prev is not None and len(rows) == 3 and _consecutive(rows[-2], rows[-3]) else None
    val = lambda r, c: np.nan if r is None else float(r.get(c, np.nan))
    pos = lambda x: x if x > 0 else np.nan
    ratio = lambda a, b: a / pos(b)

    ni, cfo = val(cur, "net_income"), val(cur, "operating_cash_flow")
    roa = ratio(ni, val(prev, "assets"))
    roa_prev = ratio(val(prev, "net_income"), val(before, "assets"))
    lev = ratio(val(cur, "total_debt"), (val(cur, "assets") + val(prev, "assets")) / 2)
    lev_prev = ratio(val(prev, "total_debt"), (val(prev, "assets") + val(before, "assets")) / 2)
    liq = ratio(val(cur, "assets_current"), val(cur, "liabilities_current"))
    liq_prev = ratio(val(prev, "assets_current"), val(prev, "liabilities_current"))
    gm = ratio(val(cur, "gross_profit"), val(cur, "revenue"))
    gm_prev = ratio(val(prev, "gross_profit"), val(prev, "revenue"))
    turn = ratio(val(cur, "revenue"), val(prev, "assets"))
    turn_prev = ratio(val(prev, "revenue"), val(before, "assets"))

    shares = np.array([val(prev, "diluted_shares"), val(cur, "diluted_shares")])
    issued = np.nan
    if (shares > 0).all():
        issued = 1 / normalized.share_index(shares)[0] - 1

    tests = {
        "roa": _test(roa > 0, roa),
        "cfo": _test(cfo > 0, cfo),
        "roa_up": _test(roa > roa_prev, roa, roa_prev),
        "accruals": _test(cfo > ni, cfo, ni),
        "leverage": _test(lev <= lev_prev, lev, lev_prev),
        "liquidity": _test(liq > liq_prev, liq, liq_prev),
        "no_issuance": _test(issued <= SHARE_TOL, issued),
        "margin_up": _test(gm > gm_prev, gm, gm_prev),
        "turnover_up": _test(turn > turn_prev, turn, turn_prev),
    }
    if special in normalized.FINANCIALS:     # no current ratio or gross margin in a bank
        tests["liquidity"] = tests["margin_up"] = np.nan
    return tests


def f_score(g: pd.DataFrame, *, special=None) -> dict:
    tests = pd.Series(f_tests(g, special=special))
    n = int(tests.notna().sum())
    return {"f_score": float(tests.sum()) if n else np.nan, "f_score_n": n,
            "f_score_fy": g["fy_end"].max()}


def altman(x1, x2, x3, mcap_to_liab, book_to_liab, x5, manufacturer):
    """(Z, Z''), each computed for every row; the caller picks per ``manufacturer``."""
    z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * mcap_to_liab + 1.0 * x5
    z2 = 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * book_to_liab
    return z.where(manufacturer), z2.where(~manufacturer)


def _company_table(history_df: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    ctx = df.dropna(subset=["cik"]).drop_duplicates("cik")
    special = dict(zip(ctx["cik"].astype("int64"), ctx["special"]))
    rows = [{"cik": cik, **f_score(g, special=special.get(cik))}
            for cik, g in history.wide(history_df).groupby("cik")]
    return pd.DataFrame(rows, columns=["cik", *COMPANY_COLUMNS])


def add_survival(df: pd.DataFrame, history_df: pd.DataFrame | None, settings: Settings) -> pd.DataFrame:
    """Add the survival columns to the screen (missing when the inputs are)."""
    df = df.copy()
    num = lambda c: pd.to_numeric(df[c], errors="coerce") if c in df else pd.Series(np.nan, index=df.index)
    date = lambda c: pd.to_datetime(df[c]) if c in df else pd.Series(pd.NaT, index=df.index)
    as_of = date("as_of")

    def near(c, anchor, days):
        """``c``, unless its period end is more than ``days`` from ``anchor``."""
        return num(c).where((date(f"{c}_end") - anchor).abs() <= pd.Timedelta(days=days))

    pos = lambda s: s.where(s > 0)
    # Some 10-Qs tag only the balance sheet's other total (LEN), which equals assets.
    assets_end, total_end = date("assets_end"), date("liabilities_and_equity_end")
    use_total = (total_end > assets_end) | (assets_end.isna() & total_end.notna())
    assets_end = assets_end.mask(use_total, total_end)
    assets = pos(num("assets").mask(use_total, num("liabilities_and_equity"))
                 .where((assets_end - as_of).abs() <= pd.Timedelta(days=MAX_BALANCE_LAG_DAYS)))
    same = lambda c: near(c, assets_end, SAME_FILING_DAYS)
    equity = same("equity")
    liab = same("liabilities").fillna(
        same("liabilities_and_equity") - same("equity_incl_nci").fillna(equity))
    liab = liab.where(assets.notna())
    equity = equity.where(assets.notna())
    annual = lambda c: near(c, assets_end, MAX_ANNUAL_LAG_DAYS).fillna(0.0)

    special = df["special"] if "special" in df else pd.Series(None, index=df.index, dtype=object)
    group = df["industry_group"] if "industry_group" in df else pd.Series(None, index=df.index, dtype=object)
    no_altman = special.isin(NO_ALTMAN) | group.isin(CLIENT_FUND_GROUPS)
    mcap = num("market_cap")

    df["tangible_book"] = equity - annual("goodwill") - annual("intangibles")
    df["p_tbv"] = mcap / pos(df["tangible_book"])
    df["p_tbv_pct"], _ = valuation._peer_pct(df, df["p_tbv"], True, settings.min_peers)
    df["ncav"] = (same("assets_current") - liab).where(~no_altman)
    df["p_ncav"] = mcap / pos(df["ncav"])

    x1 = (same("assets_current") - same("liabilities_current")) / assets
    x2 = same("retained_earnings") / assets
    x3 = num("ebit") / assets
    x5 = num("revenue") / assets
    sic = num("sic")
    manufacturer = sic.between(*MANUFACTURING_SIC)
    z, z2 = altman(x1, x2, x3, mcap / pos(liab), equity / pos(liab), x5, manufacturer)
    df["altman_z"] = z.fillna(z2).where(~no_altman)
    df["altman_model"] = pd.Series(np.where(manufacturer, "Z", "Z''"), index=df.index, dtype=object) \
        .where(df["altman_z"].notna(), None)
    distress = np.where(manufacturer, settings.altman_z_distress, settings.altman_z2_distress)
    safe = np.where(manufacturer, settings.altman_z_safe, settings.altman_z2_safe)
    df["altman_zone"] = pd.Series(
        np.select([df["altman_z"] < distress, df["altman_z"] > safe], ["distress", "safe"], "grey"),
        index=df.index, dtype=object).where(df["altman_z"].notna(), None)

    financial = special.isin(normalized.FINANCIALS)
    interest = near("interest_expense", as_of, valuation.MAX_FLOW_LAG_DAYS)
    df["interest_coverage"] = (num("ebit") / pos(interest)).where(~financial)
    cash_known = (as_of - date("cash_end")) <= pd.Timedelta(days=MAX_BALANCE_LAG_DAYS)
    debt_known = ~df["debt_unknown"].astype(bool) if "debt_unknown" in df else True
    df["net_cash"] = (num("cash") > num("total_debt").fillna(0.0)) & debt_known & cash_known & ~financial
    ok = pd.Series(pd.NA, index=df.index, dtype="boolean")
    judged = df["interest_coverage"].notna()
    ok[judged] = df.loc[judged, "interest_coverage"] >= settings.min_interest_coverage
    ok[df["net_cash"]] = True
    df["coverage_ok"] = ok

    table = pd.DataFrame(columns=["cik", *COMPANY_COLUMNS])
    if history_df is not None and len(history_df) and "cik" in df:
        table = _company_table(history_df, df)
    if "cik" in df:
        table = table.astype({"cik": "float64", "f_score": "float64", "f_score_n": "float64",
                              "f_score_fy": "datetime64[ns]"})
        df = df.merge(table, on="cik", how="left")
    else:
        df = df.assign(**dict.fromkeys(COMPANY_COLUMNS, np.nan))
    # A company that stopped filing annuals has no current score, whatever its old years said.
    old = (pd.to_datetime(df["as_of"]) if "as_of" in df else pd.Series(pd.NaT, index=df.index)) \
        - pd.to_datetime(df["f_score_fy"]) > pd.Timedelta(days=MAX_FY_LAG_DAYS)
    df.loc[old, ["f_score", "f_score_n"]] = np.nan
    return df

"""Phase 7 — Streamlit dashboard over data/screen.parquet.

Run:  streamlit run app.py      (refresh the data first with `value-screener refresh`)
"""

from __future__ import annotations

import io

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from value_screener import history, normalized, screens, valuation
from value_screener.cli import write_sheets
from value_screener.config import load_settings

st.set_page_config(page_title="Value Screener", layout="wide")
SETTINGS = load_settings()

METRIC_LABELS = {"pe": "P/E", "ps": "P/S", "pb": "P/B", "ev_ebitda": "EV/EBITDA",
                 "fcf_yield": "FCF yield", "p_ffo": "P/FFO", "dividend_yield": "Dividend yield",
                 "price_to_graham": "Price / Graham number"}
TABLE_COLS = ["ticker", "name", "industry_group", "exchange", "label", "insufficient_reason", "composite_pct",
              "market_cap_b", "pe", "ps", "pb", "ev_ebitda", "fcf_yield", "p_ffo",
              "dividend_yield", "roe", "debt_to_equity", "pe_pct", "ps_pct", "pb_pct",
              "ev_ebitda_pct", "fcf_yield_pct", "p_ffo_pct", "quality_pass", "quality_issues",
              "peer_level", "as_of"]
# Shown after market cap while a screen is selected.
SCREEN_COLS = ["deep_value_reasons", "cyclical_trough_reasons", "turning", "pe10", "p_tbv", "p_ncav", "ev_sales",
               "margin_position", "industry_cycle_position", "years_history", "f_score", "f_score_n",
               "altman_zone", "interest_coverage", "share_cagr", "off_52w_high", "momentum_12_1", "price_vs_200d"]

# Why a cell is often blank: mostly the figure doesn't apply, which isn't missing data.
BLANK_HELP = {
    "pe": "Price / earnings. Blank when earnings are zero or negative.",
    "ev_ebitda": "Blank when EBITDA is negative or can't be computed from the filings.",
    "p_ffo": "Price / funds from operations (earnings + depreciation). Ranked among REITs only. Blank when negative.",
    "pe10": "Price / average earnings over up to 10 years. Blank with under 3 years of filings or average losses.",
    "p_ncav": "Price / (current assets - all liabilities). Blank when that's negative, as it is for most companies.",
    "ev_sales": "Blank for banks, insurers and REITs, whose revenue isn't comparable, or when EV or revenue "
                "is negative or missing.",
    "interest_coverage": "EBIT / interest. Blank for banks and insurers, and when there's no interest expense.",
    "altman_zone": "Bankruptcy risk score. Blank for banks, insurers and REITs, where it doesn't apply, or when "
                   "a figure it needs is missing.",
    "margin_position": "Margin against its own range, 0 = low. Blank without enough years of history.",
    "deep_value_reasons": "Gates passed and checks failed. Blank: not cheap enough on P/TBV, P/NCAV or EV/sales.",
    "cyclical_trough_reasons": "Blank: not a cyclical at a margin low, or not cheap on through-the-cycle earnings.",
    "quality_issues": "Blank means no quality issue was found.",
}


# What each "Insufficient data" reason (valuation.REASONS) means, and whether it can change.
REASON_HELP = {
    "No SEC financial data": "No XBRL financials at SEC at all: mostly ADRs and companies without a CIK.",
    "Foreign filer (20-F / 40-F)": "Files annual reports as a foreign company, usually under IFRS or in "
                                   "another currency, which this screen doesn't read.",
    "SPAC (blank-check company)": "A shell holding cash in trust until it buys a business: there's nothing "
                                  "to value yet.",
    "New registrant: under a year of filings": "Recent IPO or spin-off, or a new holding company. It fills "
                                               "in once a full year is filed.",
    "No 12-month income figures in SEC data": "Has filings, but no full-year revenue or earnings in US "
                                              "dollars: reports in another currency (Enbridge, Canadian "
                                              "Pacific), or annual figures missing from SEC's data (Tanger).",
    "Financials over": "The latest figures are too old to compare, usually a company that stopped filing.",
    "No trustworthy market cap": "The SEC share count looks wrong (multi-class or stale), and neither Nasdaq "
                                 "nor a recent snapshot gives a cap.",
    "Loss-making: earnings ratios don't apply": "P/E, EV/EBITDA and FCF yield are meaningless on losses, so "
                                                "too few ratios are left (biotech, crypto miners).",
    "Too few comparable metrics": "Profitable, but most ratios can't be computed: negative book value, or "
                                  "mortgage REITs, which have no depreciation for P/FFO.",
}


def reason_help(reason: str) -> str:
    return next((h for k, h in REASON_HELP.items() if reason.startswith(k)), "")


def reason_table(insufficient: pd.DataFrame) -> pd.DataFrame:
    """Reasons with their count, meaning and largest examples."""
    rows = []
    for reason, g in insufficient.groupby("insufficient_reason"):
        top = g.sort_values("market_cap", ascending=False)["ticker"].head(4)
        rows.append({"reason": reason, "stocks": len(g), "means": reason_help(reason),
                     "largest": ", ".join(top)})
    return pd.DataFrame(rows).sort_values("stocks", ascending=False)


@st.cache_data(show_spinner=False)
def load_screen(mtime: float) -> pd.DataFrame:
    df = valuation.load(SETTINGS)
    df["market_cap_b"] = df["market_cap"] / 1e9
    return df


@st.cache_data(show_spinner=False)
def load_years(cik: int, special, mtime: float) -> pd.DataFrame:
    rows = history.load(SETTINGS, cik)
    if rows.empty:
        return pd.DataFrame()
    return normalized.yearly_figures(history.wide(rows), special=special, window=SETTINGS.normalized_window)


def to_excel(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    write_sheets(df, buf)
    return buf.getvalue()


def year_chart(years: pd.DataFrame, field: str, title: str, fmt: str, mark: str) -> alt.Chart:
    base = alt.Chart(years.dropna(subset=[field]), title=title)
    return getattr(base, mark)().encode(
        x=alt.X("year:O", title=None),
        y=alt.Y(f"{field}:Q", title=None, axis=alt.Axis(format=fmt)),
        tooltip=["year:O", alt.Tooltip(f"{field}:Q", format=fmt)])


path = SETTINGS.data_dir / "screen.parquet"
st.title("Value Screener")
if not path.exists():
    st.warning("No screen yet. Run `value-screener refresh` first.")
    st.stop()
df = load_screen(path.stat().st_mtime)
st.caption(f"{len(df):,} NYSE + NASDAQ stocks, scored against industry peers "
           f"(0% = cheapest in its industry). Prices as of {df['price_date'].max()}.")

# --- filters ---------------------------------------------------------------- #
with st.sidebar:
    st.header("Filters")
    screen = st.selectbox("Screen", ["None", *screens.SCREENS])
    screen_col = screens.SCREENS.get(screen)
    # A screen cuts across labels, so choosing one starts from all of them.
    labels = st.multiselect("Label", valuation.LABELS, default=[] if screen_col else ["Undervalued"],
                            key=f"labels_{screen}")
    turning = st.checkbox("Turning only", help="Revenue above a year ago and price above its 200-day average.")
    groups = st.multiselect("Industry", sorted(df["industry_group"].dropna().unique()))
    exchanges = st.multiselect("Exchange", sorted(df["exchange"].dropna().unique()))
    caps = df["market_cap_b"].dropna()
    top = float(np.ceil(caps.max())) if len(caps) else 1.0
    # A screen has its own size floor (screen_min_market_cap), so it starts from everything it passed.
    lo, hi = st.slider("Market cap ($B)", 0.0, top, (0.0 if screen_col else 0.3, top), step=0.05,
                       key=f"cap_{screen}")
    quality = st.radio("Quality filter", ["Any", "Pass", "Fail"], horizontal=True)
    search = st.text_input("Ticker / name contains")

view = df
if screen_col:
    view = view[view[screen_col]]
if turning:
    view = view[view["turning"].fillna(False).astype(bool)]
if labels:
    view = view[view["label"].isin(labels)]
if groups:
    view = view[view["industry_group"].isin(groups)]
if exchanges:
    view = view[view["exchange"].isin(exchanges)]
view = view[view["market_cap_b"].between(lo, hi) | (view["market_cap_b"].isna() & (lo == 0))]
if quality != "Any":
    view = view[view["quality_pass"] == (quality == "Pass")]
if search:
    q = search.lower()
    view = view[view["ticker"].str.lower().str.contains(q) | view["name"].fillna("").str.lower().str.contains(q)]
view = view.sort_values("composite_pct")

tiles = [(lab, (df["label"] == lab).sum()) for lab in valuation.LABELS]
tiles += [(name, df[col].sum()) for name, col in screens.SCREENS.items()]
for col, (name, count) in zip(st.columns(len(tiles)), tiles):
    col.metric(name, f"{count:,}")

st.subheader(f"{len(view):,} stocks")
if screen_col:
    st.caption(f"{int(df[screen_col].sum())} stocks pass {screen}; the filters show {len(view)}. A starting point "
               "for reading filings, not a buy list: in the 2017-2026 backtest (docs/validation.md) neither screen "
               "beat the S&P 500 after costs, and nothing here tells a cyclical low from a permanent decline. "
               "Blank cells mean a figure doesn't apply, not that it's missing: hover a column name to see why.")

insufficient = df[df["label"] == "Insufficient data"]
if len(insufficient) and (not labels or "Insufficient data" in labels):
    with st.expander(f"Why {len(insufficient):,} stocks are 'Insufficient data'"):
        st.caption("A stock needs a market cap, financials from the last "
                   f"{SETTINGS.stale_months} months, and at least {SETTINGS.min_metrics} valuation ratios to "
                   "compare with its industry. Most of these aren't errors: the figures don't exist in SEC's "
                   "US GAAP data, or a ratio doesn't apply. Each stock's reason is in the table's "
                   "'Why insufficient' column.")
        st.dataframe(reason_table(insufficient), width="stretch", hide_index=True,
                     column_config={"stocks": st.column_config.NumberColumn(format="%d"),
                                    "means": st.column_config.TextColumn("What it means", width="large")})
table_cols = TABLE_COLS
if screen_col:
    at = TABLE_COLS.index("market_cap_b") + 1
    table_cols = [*TABLE_COLS[:at], *SCREEN_COLS, *TABLE_COLS[at:]]
st.dataframe(
    view.reindex(columns=table_cols), width="stretch", height=480, hide_index=True,
    column_config={
        **{c: st.column_config.NumberColumn(format="%.1f", help=BLANK_HELP.get(c))
           for c in ("pe10", "p_tbv", "p_ncav", "ev_sales", "interest_coverage")},
        **{c: st.column_config.NumberColumn(format="%.2f", help=BLANK_HELP.get(c)) for c in
           ("margin_position", "industry_cycle_position", "share_cagr", "off_52w_high", "momentum_12_1", "price_vs_200d")},
        **{c: st.column_config.TextColumn(help=h) for c, h in BLANK_HELP.items()
           if c in ("deep_value_reasons", "cyclical_trough_reasons", "altman_zone", "quality_issues")},
        "p_ffo_pct": st.column_config.NumberColumn(format="%.2f", help=BLANK_HELP["p_ffo"]),
        "pe_pct": st.column_config.NumberColumn(format="%.2f", help=BLANK_HELP["pe"]),
        "composite_pct": st.column_config.ProgressColumn("Industry pct", format="%.2f",
                                                         min_value=0, max_value=1),
        "market_cap_b": st.column_config.NumberColumn("Mkt cap $B", format="%.1f"),
        "insufficient_reason": st.column_config.TextColumn(
            "Why insufficient", help="Why the stock can't be compared with its industry. Blank when it can."),
        **{c: st.column_config.NumberColumn(format="%.1f", help=BLANK_HELP.get(c))
           for c in ("pe", "ps", "pb", "ev_ebitda", "p_ffo")},
        **{c: st.column_config.NumberColumn(format="%.3f") for c in
           ("fcf_yield", "dividend_yield", "roe", "debt_to_equity")},
    })
d1, d2 = st.columns(2)
d1.download_button("Download CSV", view.to_csv(index=False).encode(), "screen.csv", "text/csv")
d2.download_button("Download Excel (one sheet per label and screen)", to_excel(view), "screen.xlsx")

# --- per-industry view -------------------------------------------------------- #
st.subheader("Compare with industry peers")
tickers = view["ticker"].tolist() or df["ticker"].tolist()
p1, p2 = st.columns(2)
pick = p1.selectbox("Stock", tickers)
metric = p2.selectbox("Metric", list(METRIC_LABELS), format_func=METRIC_LABELS.get)
row = df[df["ticker"] == pick].iloc[0]
peers = df[(df["industry_group"] == row["industry_group"]) & df[metric].notna()].copy()
if peers.empty:
    st.info(f"No {METRIC_LABELS[metric]} values in {row['industry_group']}.")
else:
    hi_clip = peers[metric].quantile(0.95)
    peers["value"] = peers[metric].clip(upper=hi_clip)
    peers["selected"] = np.where(peers["ticker"] == pick, pick, "peers")
    chart = alt.Chart(peers).mark_bar().encode(
        x=alt.X("value:Q", bin=alt.Bin(maxbins=30), title=f"{METRIC_LABELS[metric]} (capped at 95th pct)"),
        y=alt.Y("count()", title="stocks"),
        color=alt.Color("selected:N", scale=alt.Scale(domain=["peers", pick],
                                                       range=["#9aa5b1", "#d62728"]), legend=None),
    )
    st.altair_chart(chart, width="stretch")
    val = row[metric]
    st.caption(f"{pick} in {row['industry_group']} ({len(peers)} peers with this metric): "
               f"{METRIC_LABELS[metric]} = {val:.3g}" if pd.notna(val) else
               f"{pick} has no {METRIC_LABELS[metric]}.")

# --- the selected stock's history ---------------------------------------------- #
st.subheader(f"{pick}: last {SETTINGS.normalized_window} fiscal years")
if row["label"] == "Insufficient data" and row.get("insufficient_reason"):
    st.info(f"{pick} is 'Insufficient data': {row['insufficient_reason']}. {reason_help(row['insufficient_reason'])}")
for name, col in screens.SCREENS.items():
    reasons = row[f"{col}_reasons"]
    st.caption(f"{name}: " + (f"passes ({reasons})" if row[col] else f"near miss ({reasons})" if reasons else "didn't pass any entry test"))
hist_path = history.path(SETTINGS)
years = pd.DataFrame()
if pd.notna(row["cik"]) and hist_path.exists():
    years = load_years(int(row["cik"]), row["special"], hist_path.stat().st_mtime)
if years.empty:
    st.info(f"No annual history for {pick}: foreign filers often have none in SEC data.")
else:
    # 52/53-week years can end on Jan 3 or Dec 28: the plain calendar year would repeat or skip one.
    years["year"] = (pd.to_datetime(years["fy_end"]) - history.TOL).dt.year
    years["revenue_b"] = years["revenue"] / 1e9
    y1, y2, y3 = st.columns(3)
    y1.altair_chart(year_chart(years, "revenue_b", "Revenue ($B)", ",.1f", "mark_bar"), width="stretch")
    y2.altair_chart(year_chart(years, "margin", "EBIT margin", ".0%", "mark_line"), width="stretch")
    y3.altair_chart(year_chart(years, "eps", "EPS, split-adjusted ($)", ",.2f", "mark_bar"), width="stretch")
    st.caption("EPS is approximate: net income, before preferred dividends, on today's share scale, not the "
               "reported figure. For banks and insurers, margin uses pretax income.")

# --- industry cycle position --------------------------------------------------- #
st.subheader("Industry cycle position")
st.caption("Median of each company's current margin against its own history: 0 = at the lows, 1 = at the highs. "
           "Margins only: a low reading can also mean an industry in lasting decline.")
st.dataframe(
    screens.cycle_table(df), width="stretch", hide_index=True,
    column_config={
        "industry_group": "Industry",
        "cycle_position": st.column_config.ProgressColumn("Cycle position", format="%.2f", min_value=0, max_value=1),
        "with_history": "With history",
        "cyclical": "Cyclical",
        "deep_value": "Deep value",
        "cyclical_trough": "Cyclical trough",
        "turning": "Turning",
    })

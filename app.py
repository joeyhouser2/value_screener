"""Phase 7 — Streamlit dashboard over data/screen.parquet.

Run:  streamlit run app.py      (refresh the data first with `value-screener refresh`)
"""

from __future__ import annotations

import io

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from value_screener import valuation
from value_screener.config import load_settings

st.set_page_config(page_title="Value Screener", layout="wide")
SETTINGS = load_settings()

METRIC_LABELS = {"pe": "P/E", "ps": "P/S", "pb": "P/B", "ev_ebitda": "EV/EBITDA",
                 "fcf_yield": "FCF yield", "p_ffo": "P/FFO", "dividend_yield": "Dividend yield",
                 "price_to_graham": "Price / Graham number"}
TABLE_COLS = ["ticker", "name", "industry_group", "exchange", "label", "composite_pct",
              "market_cap_b", "pe", "ps", "pb", "ev_ebitda", "fcf_yield", "p_ffo",
              "dividend_yield", "roe", "debt_to_equity", "pe_pct", "ps_pct", "pb_pct",
              "ev_ebitda_pct", "fcf_yield_pct", "p_ffo_pct", "quality_pass", "quality_issues",
              "peer_level", "as_of"]


@st.cache_data(show_spinner=False)
def load_screen(mtime: float) -> pd.DataFrame:
    df = valuation.load(SETTINGS)
    df["market_cap_b"] = df["market_cap"] / 1e9
    return df


def to_excel(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as xl:
        for label in valuation.LABELS:
            df[df["label"] == label].to_excel(xl, sheet_name=label[:31], index=False)
    return buf.getvalue()


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
    labels = st.multiselect("Label", valuation.LABELS, default=["Undervalued"])
    groups = st.multiselect("Industry", sorted(df["industry_group"].dropna().unique()))
    exchanges = st.multiselect("Exchange", sorted(df["exchange"].dropna().unique()))
    caps = df["market_cap_b"].dropna()
    lo, hi = st.slider("Market cap ($B)", 0.0, float(np.ceil(caps.max())) if len(caps) else 1.0,
                       (0.3, float(np.ceil(caps.max())) if len(caps) else 1.0), step=0.1)
    quality = st.radio("Quality filter", ["Any", "Pass", "Fail"], horizontal=True)
    search = st.text_input("Ticker / name contains")

view = df
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

c1, c2, c3, c4 = st.columns(4)
for col, lab in zip((c1, c2, c3, c4), valuation.LABELS):
    col.metric(lab, f"{(df['label'] == lab).sum():,}")

st.subheader(f"{len(view):,} stocks")
st.dataframe(
    view.reindex(columns=TABLE_COLS), width="stretch", height=480, hide_index=True,
    column_config={
        "composite_pct": st.column_config.ProgressColumn("Industry pct", format="%.2f",
                                                         min_value=0, max_value=1),
        "market_cap_b": st.column_config.NumberColumn("Mkt cap $B", format="%.1f"),
        **{c: st.column_config.NumberColumn(format="%.1f") for c in ("pe", "ps", "pb", "ev_ebitda", "p_ffo")},
        **{c: st.column_config.NumberColumn(format="%.3f") for c in
           ("fcf_yield", "dividend_yield", "roe", "debt_to_equity")},
    })
d1, d2 = st.columns(2)
d1.download_button("Download CSV", view.to_csv(index=False).encode(), "screen.csv", "text/csv")
d2.download_button("Download Excel (one sheet per label)", to_excel(view), "screen.xlsx")

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

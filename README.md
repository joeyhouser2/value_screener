# value_screener

A free, self-hosted screen of every NYSE + NASDAQ common stock, labeled
**Undervalued / Fair / Overvalued** relative to its *industry peers*, from SEC
filings and daily closes. See [PLAN.md](PLAN.md) for the design and checklist.

## Setup (Windows)

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[ui,dev]"
```

Set your SEC contact in `config/settings.local.yaml` (git-ignored) as `sec_user_agent: "Name email@domain"`, or the `SEC_USER_AGENT` env var;
SEC rejects requests without one.

## Use

```powershell
value-screener refresh                       # full pipeline (first run ~30-40 min, cached after)
value-screener refresh --tickers AAPL MSFT   # quick subset (percentiles need a full run)
value-screener screen --label undervalued --min-mcap 1e9 --top 50
value-screener screen --label undervalued --industry Banks --quality-only
value-screener export                        # output/screen_YYYY-MM-DD.xlsx, one sheet per label
value-screener coverage                      # % of stocks with each metric, by industry
streamlit run app.py                         # dashboard
```

`scripts/weekly_refresh.ps1` runs refresh + export + coverage for Task Scheduler
(registration snippet in the script header).

Desktop shortcut: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/make_shortcut.ps1`
puts a "Value Screener" icon on the Desktop. It runs `scripts/launch_dashboard.bat`, which
starts the dashboard and opens it in the browser, or just opens it if it's already running.
`scripts/make_icon.py` redraws `assets/icon.ico`.

## Pipeline

| Phase | Module | Output |
|---|---|---|
| Universe | `universe.py` | `data/universe.parquet` — NASDAQ Trader symbol files, common stock only, SEC CIK |
| Industry | `industry.py` | `data/industry.parquet` — SIC → group/sector (`config/sic_groups.yaml`), Yahoo fallback |
| Fundamentals | `fundamentals.py` | `data/fundamentals.parquet` — TTM from XBRL companyfacts (`config/xbrl_tags.yaml`) |
| Prices | `prices.py` | `data/prices.parquet` — last close (Yahoo, swappable), market cap |
| Valuation | `valuation.py` | `data/screen.parquet` + `data/history/` snapshot |

**Scoring.** Each metric becomes a percentile within the industry group (0 = cheapest
peer); groups with fewer than `min_peers` stocks fall back to the sector. Metric sets:
banks / specialty finance / insurers use P/B and P/E; REITs P/FFO (net income + D&A)
and P/B; everyone else P/E, EV/EBITDA, P/S and FCF yield. The composite (mean
percentile) is ranked again within the industry. **Undervalued** = cheapest 20% *and*
passes quality (profitable, positive FCF, debt/equity under the industry 80th
percentile, ROE > 0). **Overvalued** = dearest 20%. Cheap names that fail quality stay
**Fair** with `cheap_fails_quality` set. **Insufficient data** = fewer than 2 metrics,
no market cap, or fundamentals older than 15 months. All thresholds are in
`config/settings.yaml`.

**Data notes.**
- Market cap = price × SEC shares outstanding. When that's missing or implausible
  (multi-class shares like BRK, stale share counts), Yahoo's market cap is used
  instead (`market_cap_src`).
- Foreign filers on 20-F/40-F mostly report IFRS, not US GAAP, so they usually land
  in Insufficient data.
- Every network call is cached (`data/cache/`, 7 days), so a warm re-run makes ~0 SEC
  requests. SEC is limited to 8 requests/second.
- One bad ticker never stops a run: it is logged to `data/errors.csv`.

This is a screening tool, not investment advice.

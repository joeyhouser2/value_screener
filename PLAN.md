# value_screener — build plan

## Phase 1 — Universe  (`universe.py`)
- [x] Download NASDAQ Trader symbol files: `nasdaqlisted.txt` and `otherlisted.txt`
      (otherlisted covers NYSE / NYSE American / Arca)
- [x] Keep NYSE + NASDAQ only; drop ETFs, test issues, warrants, units, rights,
      preferreds (filter on ETF flag + name/suffix patterns)
- [x] Map ticker -> CIK via SEC `company_tickers_exchange.json`
- [x] Output `data/universe.parquet`: ticker, name, exchange, cik
- [x] Report count of tickers with no CIK (foreign/ADR filers) — expected, keep them flagged
      (first run: 5,349 stocks, 11 without a CIK)

## Phase 2 — Industry labels  (`industry.py`)
- [x] Pull SIC code + description from SEC submissions API (`data.sec.gov/submissions/CIK##########.json`)
- [x] Roll SIC up to ~25-40 readable industry groups via a mapping table in `config/sic_groups.yaml`
      (e.g. Banks, Insurance, REITs, Software, Semiconductors, Biotech, Oil & Gas, Utilities, Retail ...)
      (48 groups across 11 sectors; first run: 0 unclassified)
- [x] Fallback: yfinance `sector` / `industry` when SIC is missing (also SIC 0000 / 8880 ADR placeholder)
- [x] Flag special industries that need different metrics: banks, insurers, REITs, utilities

## Phase 3 — Fundamentals  (`fundamentals.py`)
- [x] Fetch `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` per company (cached)
- [x] Extract with fallback tag lists in `config/xbrl_tags.yaml`:
      revenue, net income, operating income, D&A (-> EBITDA), EPS diluted, shares outstanding,
      total equity, total debt, cash, operating cash flow, capex, dividends
- [x] Build TTM values by summing the last 4 quarters (10-Q + derive Q4 from 10-K) ; fall back to latest annual
- [x] Record `as_of` date and data source per field; flag stale data (> 15 months old)
      (first run: 4,113/5,349 with revenue; most gaps are IFRS 20-F/40-F filers and SPACs)

## Phase 4 — Prices  (`prices.py`)
- [x] Batch-download last close via `yfinance.download` (chunks of ~200, throttled)
- [x] Market cap = price x shares outstanding (EDGAR), cross-checked against Nasdaq's screener API
      (one request for all listings); disagreements use Nasdaq, then Yahoo per ticker
- [x] Keep the price layer swappable (interface) so a paid API (FMP/Polygon) can drop in later

## Phase 5 — Valuation & labels  (`valuation.py`)
Metrics per stock:
- [x] P/E, P/S, P/B, EV/EBITDA, FCF yield, PEG (if growth available), dividend yield
- [x] Graham Number and price/Graham ratio
Peer-relative scoring:
- [x] For each metric, percentile rank within industry group (min 8 peers; else use sector)
- [x] Industry-specific metric sets: banks/insurers -> P/B, P/E ; REITs -> P/FFO-proxy, P/B ;
      everyone else -> P/E, EV/EBITDA, P/S, FCF yield
- [x] Composite value score = mean of available percentiles (lower = cheaper)
Quality filter (to avoid value traps):
- [x] Positive TTM earnings, positive FCF, debt/equity under industry 80th pct, ROE > 0
Labels:
- [x] `Undervalued` = composite in cheapest 20% of industry AND passes quality
- [x] `Overvalued`  = composite in most expensive 20% of industry
- [x] `Fair` otherwise; `Insufficient data` when < 2 metrics available
- [x] All thresholds live in `config/settings.yaml`

## Phase 6 — CLI  (`cli.py`)
- [x] `value-screener refresh` (full pipeline; `--tickers AAPL MSFT` for a quick subset)
- [x] `value-screener screen --label undervalued --industry Software --min-mcap 1e9 --top 50`
- [x] `value-screener export --xlsx output/screen_YYYY-MM-DD.xlsx` (one sheet per label)

## Phase 7 — Dashboard  (`app.py`, Streamlit)
- [x] Table: ticker, name, industry, market cap, key multiples, industry percentiles, score, label
- [x] Filters: label, industry, exchange, market-cap range, quality pass/fail
- [x] Per-industry view: distribution of P/E etc. with the selected stock highlighted
- [x] Download buttons (CSV / Excel)  (smoke-tested headless with streamlit AppTest)

## Phase 8 — Automation & checks
- [x] Windows Task Scheduler script `scripts/weekly_refresh.ps1`
- [ ] Sanity tests: spot-check 10 well-known tickers' P/E against Yahoo (within ~10%)
- [x] Coverage report: % of universe with each metric, by industry
- [x] Snapshot each run to `data/history/` so labels can be tracked over time

First full run (2026-10-31): 382 Undervalued, 2,804 Fair, 753 Overvalued, 1,410 Insufficient data.
Open issues: XOM shows no net income (check NetIncomeLoss tag selection); a few SIC groups
look odd (SSRM in Holding & Investment Cos).

## Known pitfalls
- Inconsistent XBRL tags (Revenues vs RevenueFromContractWithCustomer...) -> fallback lists
- Foreign filers (20-F/40-F), SPACs, ADRs often lack usable XBRL -> flag, don't fail
- Negative earnings make P/E meaningless -> treat as missing, not as "cheap"
- Share-count changes (splits/buybacks) -> use latest `dei:EntityCommonStockSharesOutstanding`
- yfinance is unofficial and may break; that's why prices are behind an interface

## Not in scope (yet)
DCF models, analyst estimates, intraday data, options.

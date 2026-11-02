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
Open issues: a few SIC groups look odd (SSRM in Holding & Investment Cos). XOM's missing net
income is fixed (predecessor CIK merge).

## Phase 9 — Deep value & cyclical value  (`history.py`, `normalized.py`)
Why: TTM multiples + the positive-earnings quality filter reject cyclicals at the trough
(when they're best bought) and favour them at peak earnings. Industry-relative ranks
also miss whole industries that are cheap together. This phase adds a through-the-cycle
lens and two new screens; the existing `label` column is unchanged.

9a — Annual history (EDGAR, no new endpoints)
- [x] New tags in `config/xbrl_tags.yaml`: Assets, AssetsCurrent, Liabilities,
      LiabilitiesCurrent, RetainedEarningsAccumulatedDeficit, InterestExpense (+ fallbacks),
      GrossProfit (+ CostOfRevenue fallback), Goodwill, IntangibleAssetsNetExcludingGoodwill
      (+ pretax income for an EBIT fallback, weighted diluted shares, NotesPayable as
      last-resort debt — the TTM screen now uses it too when no other debt tag exists)
- [x] The companyfacts cache stores *slimmed* payloads, so key the cache on a hash of the
      tag list: changing tags triggers one cold refetch (~30 min), then warm again
      (`data/cache/sec/facts/t<hash>/`; the old unkeyed `facts/CIK*.json.gz` can be deleted)
- [x] `history.py`: per company, fiscal-year values for every flow and FY-end instants ->
      long-format `data/fundamentals_history.parquet` (cik, fy_end, concept, val, src, tag,
      filed, first_filed); tags merged across switches, derived gross profit /
      liabilities / total debt when not reported; `history_years` setting (15)
- [x] Keep `filed` / `first_filed` so Phase 10 can rebuild point-in-time views; predecessor
      CIKs merged
      Known gaps: many filers (XOM, NUE, DHI, banks) don't tag OperatingIncomeLoss -> 9b uses
      pretax income + interest; banks/homebuilders have unclassified balance sheets (no
      current assets/liabilities) -> NCAV and current-ratio tests N/A there

9b — Normalized ("through-the-cycle") valuation
- [x] `years_history` (need >= 5 FY for normalized metrics, >= 7 to call a cycle)
- [x] P/E10: market cap / mean net income over up to 10 FYs, each year restated to today's
      share count. Reported EPS can't be averaged (old years aren't split-adjusted), so split
      jumps are removed from the share count by ratio. Flatters diluters -> `share_cagr`
- [x] Mid-cycle EBIT = median EBIT margin over the window x TTM revenue;
      EV / mid-cycle EBIT (handles growth better than plain averaging)
- [x] Normalized FCF yield = mean FCF over the window / market cap
- [x] `margin_position`: percentile of the TTM margin among its own FY margins
      (0 = below every year, 1 = above every year); `margin_vol` and `loss_years`
- [x] `cyclical` flag: industry listed in `sic_groups.yaml` (`cyclical: true` on Energy,
      Metals & Mining, Chemicals, Semis, Autos, Homebuilders, Transportation, Machinery,
      ...) OR `margin_vol` >= `cyclical_margin_vol` (not financials / REITs)
- [x] Industry cycle position = median `margin_position` of its members (flags whole
      industries at a trough, which peer-relative ranks can't see)
- [x] Peer percentiles for the normalized metrics via the existing `_peer_pct`
- [x] Data fixes found on the way: proxy-statement XBRL no longer replaces 10-K values (FDX);
      EBIT falls back to pretax + interest, and a tag abandoned > 120 days before the latest
      period no longer pairs with current revenue (LEN's operating income ended in 2021)
      Known gaps: stale capex (665 companies), dividends (537) and D&A (298) are still used
      as-is. A real offering that exactly doubles the share count passes as a split.

9c — Survival / quality (replaces "profitable now" for these screens)
- [x] Piotroski F-score (9 binary tests from this FY vs last FY); `f_score_n` = tests computable
- [x] Altman Z (Z for manufacturers, Z'' for non-manufacturers; N/A for banks/insurers/REITs,
      brokers/exchanges); `altman_zone` because the two scales have different cutoffs
- [x] Interest coverage = EBIT / interest expense; `coverage_ok` passes net-cash companies
- [x] Tangible book, P/TBV, NCAV = current assets - total liabilities, P/NCAV
      Balance-sheet figures use the latest filing (10-Q included), only when dates match.
      Coverage of 5,349: F-score 4,429, Altman 3,198, coverage_ok 3,266, P/TBV 3,214, P/NCAV 1,503.
      Known gaps: homebuilders mostly get no Altman (unclassified balance sheets); captive
      finance arms (F) distort Altman and coverage; many microcap ADRs show P/NCAV near 0
      (UPC 0.0002), which looks like bad data; debt-free filers often lose the leverage test
      (no tag), so raw F-scores read low.
- [x] Stale / missing debt: 839 live companies had a debt tag > 400 days old feeding EV and
      debt/equity (ORCL $0 from 2022). Added a combined-debt tag and summed debt pieces (394
      companies), rejected when they exceed liabilities or imply > 25% interest. Still
      abandoned: repaid unless the interest bill implies debt >= 5% of market cap, then
      `debt_unknown` (about 174, incl. DE, F, KKR): no EV, fails quality unless financial.
      Known gaps: the history doesn't use debt pieces, so the F-score leverage test can be
      N/A; pieces can overstate (MTH $2.4B against $2.5B liabilities, not yet checked).
- [x] First-of-day runs crashed since 9b: fresh closes stayed object dtype until a parquet
      round trip. Fixed in `with_market_cap`, with a regression test and a real replay.
- [x] P/E10 from 3+ years for young companies (`pe10_min_years`), not peer-ranked: 2,270 have it.
      Short-history names aren't all young: BLK and FERG may need predecessor CIKs (unconfirmed).

9d — Price trend (for the "is it turning?" check; yfinance, cached)
- [x] 400 days of daily history per ticker in its own batched download (`trend.py`), so a
      failure can't touch market caps or labels: % off 52w high, 12-1 month momentum (total
      return), price vs 200-day average; cached per day in `data/cache/prices/`
- [x] Shown as context only, never required to pass a screen
- [x] Failed tickers retried, each batch limited to 2 minutes, and tickers still without
      trend logged to `errors.csv`: Yahoo stalled for about two hours and dropped 241 tickers
      as "possibly delisted" (CVX, CSX), with nothing logged
      Known gaps: the closes download in `prices.py` has no such limit; a run during market hours takes the live price as today's close (also true
      of the screen's price); momentum on serial reverse-splitters depends on Yahoo's adjustment

9e — New screens (columns `deep_value`, `cyclical_trough`, plus `*_reasons`)
- [x] Deep value: (P/TBV in industry's cheapest 20% OR P/NCAV 0.1-1 OR EV/sales cheapest 20%)
      AND P/TBV <= 3 AND F-score >= 6 AND survival checks. The P/TBV ceiling is new: EV/sales
      alone passed thin-margin names (MCK, CAH), and it isn't deep value to be the cheapest
      of an expensive industry
- [x] Cyclical trough: cyclical AND years_history >= 7 AND margin_position <= 0.33
      AND P/E10 or EV/mid-cycle EBIT in industry's cheapest 30% AND F-score >= 4 (lower, as
      the F-score rewards the rising margins a trough lacks) AND survival checks
- [x] Survival checks: `coverage_ok` not False AND not (Altman distress without coverage_ok).
      Not a plain Altman gate: Z'' puts 203 large profitable companies in distress (LOW, HCA,
      NOW), since it ignores market value and punishes buybacks and deferred revenue.
      F-score scaled to `f_score_n` (at least 6 tests), market cap >= $50M, debt known
- [x] Dilution guard: serial diluters fail both screens on `share_cagr` > 5% a year
- [x] `turning` hint: revenue up YoY AND price above 200-day (informational). Both, as either
      alone is true of 67% of the market. Blank when missing data could still decide it
- [x] All thresholds in `config/settings.yaml`; financials excluded from Altman / NCAV
      Known gaps: nothing separates a cyclical low from permanent decline (BIIB passes), and
      a one-off charge can make a company look cyclical. Thresholds are judgment calls until
      Phase 10 tests them

9f — CLI, export, dashboard
- [x] `screen --label deep-value | cyclical-trough`, plus `--turning`; export adds one sheet
      per new screen
- [x] Dashboard: new screen filters, an industry cycle-position table, and a 10-yr
      revenue / margin / EPS chart for the selected stock, with why it passed or nearly did
- [x] New columns included in `data/history/` snapshots
      Known gaps: charted EPS is net income before preferred dividends on today's share scale,
      so it runs above reported EPS (JPM 2016: 6.70 vs 6.19), and a real share change in a split
      year is lost (AAPL 2016: 2.18 vs 2.08). The dashboard's $300M default hides small passers

9g — Tests
- [x] Synthetic boom-bust company: trough flagged, peak not; P/E10 and margin_position by hand
- [x] F-score and Altman Z against hand-computed fixtures; financials get N/A, not 0
- [x] Real fixtures: XOM (predecessor merge + energy cycle), a homebuilder (LEN), a semi (MU)
- [x] Warm-cache rerun makes 0 SEC calls; one bad history row doesn't fail the run. A company
      whose P/E10 or F-score raised used to crash the run: now logged to `errors.csv`
- [x] `ticker_overrides` in `sic_groups.yaml`: ACT and EQH file under SIC 6411 (brokers) but
      underwrite, so they rank as insurers. Both leave the trough screen (81 -> 79)
      Known gaps: only those two were checked; other SIC 6411 names may be insurers too (ARX?).
      These tests show the figures are computed as intended, not that the screens beat anything

## Phase 10 — Validation (outline; plan in detail after Phase 9)
- [ ] Point-in-time panel from `filed` dates (no look-ahead), quarterly rebalance dates
- [ ] Forward returns vs SPY, RSP (equal weight) and IWD (Russell 1000 Value), after costs
- [ ] Measure each screen against a plain "cheapest earnings yield" baseline
- [ ] Survivorship caveat: today's listings lack delisted names; flag results as optimistic
      until a delisted-ticker source is added
- [ ] Forward-track the weekly `data/history/` snapshots as a no-look-ahead test

## Known pitfalls
- Inconsistent XBRL tags (Revenues vs RevenueFromContractWithCustomer...) -> fallback lists
- Foreign filers (20-F/40-F), SPACs, ADRs often lack usable XBRL -> flag, don't fail
- Negative earnings make P/E meaningless -> treat as missing, not as "cheap"
- Share-count changes (splits/buybacks) -> use latest `dei:EntityCommonStockSharesOutstanding`
- yfinance is unofficial and may break; that's why prices are behind an interface

## Not in scope (yet)
DCF models, analyst estimates, intraday data, options.

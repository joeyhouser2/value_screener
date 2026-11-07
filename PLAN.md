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

## Phase 10 — Validation
Question: did `deep_value` and `cyclical_trough` beat the nominal S&P, and a cheap, dumb
alternative, using only what was knowable at the time? A "no" is a valid result.

What a feasibility check found (Nov 2026, on the real cache):
- Replay works: the slim cache keeps every filed value with its `filed` date, so filtering
  rows by date and rerunning `extract` and `company_history` rebuilds what was known, with
  restatements undone (GE has 777 periods with more than one filed value)
- Cost: 640 ms per company per date, or 31 core-hours for 4,400 companies x 40 dates
- History limits the test window (companies with enough fiscal years filed by then):

      end of     >=5 yrs   >=7 yrs   >=10 yrs
      2016         1,983         9          0
      2018         2,324     1,992          2
      2020         2,537     2,318        148
      2022         2,793     2,532      2,174

  So `cyclical_trough` (needs 7 years) can only start around the end of 2018, and P/E10 is
  a true 10-year figure only from 2022. That's about eight years, with one or two cycles
- Survivorship: Yahoo has no prices for SIVB, FRC, WLL or SHLDQ, and tickers get reused (BBBY
  starts in July 2026). Failures are missing, and distressed cyclicals are exactly what
  `cyclical_trough` buys, so its results will be flattered the most
- Replay bug: when a tag changes, `extract` can pair new earnings with old revenue (MU, June
  2019: $30.4B from FY2018, when trailing revenue was about $27B). Revenue lags net income by
  over 120 days for 4-8% of companies at the dates sampled, and is missing for 3-17%

10a — Forward tracking (first: it costs little, and every week not recorded is lost)
- [x] `value-screener track`: a cohort every 91 days, returns since, vs SPY, RSP, IWD. Members
      are frozen on first sight; stocks Yahoo stops pricing keep their last return, flagged
      `gone`, with a second figure counting them as total losses
- [x] Cheapest 10% by earnings yield tracked too, as its losers can't be recovered later
- [x] Snapshots from before Phase 9 (no screen columns) excluded; weekly script runs it
      First cohort: 2026-11-02 (108 deep value, 79 trough, 231 baseline), bought at the next close
      The only test free of look-ahead and survivorship, but it needs years to mean anything
      Known gaps: returns are before costs. Record lives only in `data/`, which isn't in git.
      The baseline is deliberately naive, so it includes P/Es under 1 that are one-offs or bad data

10b — Point-in-time panel (`backtest/`, resumable, one cached Parquet per date)
- [x] `as_of(slim, date)` and a replay of the existing pipeline code, not a second copy of it
- [x] Fix the MU bug first (stale flows treated as missing), which also protects live runs
      Tags are stitched first, but about 207 live companies were estimated to lose revenue
- [x] Price history: daily Close, Adj Close, Volume and split events per ticker, cached; the
      batch time limit and retries from `trend.py`
- [x] Past market cap = shares known then x that day's unadjusted close. Yahoo's Close is
      split-adjusted, so undo later splits. Today's Nasdaq and Yahoo cap fallbacks have no
      history: compare against live caps and report how many companies this gets wrong
      Cover-page scale slips (x1000) are scaled back; net income / EPS fills only a missing or
      stale count, never one changed by a split; a count under half of it is blank (one class
      only); a cap over 1000x revenue and equity is blank. Yahoo's spin-offs (CTVA 6.665) are
      not splits for the share count. Apple June 2014 $560B, Microsoft $344B; BKNG, APH, TPL,
      TSCO match live
      US-form filers: 3,426 of 3,675 caps within 5%, 79 off by over 50%, 355 lost. Of $1B+ live
      caps, 1,847 of 2,059 are within 5%, 128 lost (mostly Nasdaq-sourced: multi-class, V, BRK,
      CMCSA, HEI, ABNB), 23 wrong (Up-C class A only, recent IPOs, ADS filers ONC, ZLAB)
- [x] Key on CIK; drop price history from before a reused ticker's first filing
- [x] Gate: replay at today's date must closely reproduce today's `screen.parquet`. Report
      the match rate, and don't go on until differences are understood
      Replay with live prices and caps: 100.0% of labels, fields and screen members, on 5,148
      tickers. The 201 not replayed have no fundamentals, all "Insufficient data"
      With past caps, US-form filers (Nov 3 2026): labels 90.1%. Of 414 label differences, 266
      are a cap lost or gained, 22 a cap off by over 5%, 126 names near a cutoff whose peers lost
      caps. Deep value keeps 83 of 90 live names (CTVA by the spin-off check, 6 lost caps) and
      adds 15, all peer shifts (P/TBV percentile 0.2-0.36 live, under 0.2 replayed); trough
      keeps 69 of 73 (3 lost caps, AOS a peer shift) and adds 6, all peer shifts
      Effect on a backtest: names without a past cap are left out, and the cheapest-fifth
      cutoffs run slightly looser than live
- [x] Full panel built: 59 quarter ends, 2012-03-31 to 2026-09-30, 1,847 to 5,130 stocks,
      63-137 deep value names a quarter. The first build had GOOG at $25T (a count filed after
      the 2022 split, multiplied by it again) and BYD at $623B (a x1000 slip missed on a $0.08
      EPS): splits now apply after the filing date, and the slip check allows 3x. Largest caps
      are plausible every quarter; caps over 200x revenue and equity are SPAC-like shells
      Cyclical trough finds 0 names before 2014 and 9-16 in 2014-15: it needs 7 fiscal years,
      and XBRL mostly starts in 2009. Alphabet is missing before 2016: Google Inc's CIK isn't
      in `cik_predecessors.yaml`, and other reorganizations may be missing the same way
- [x] Test on hand-built slims that a figure filed one day after the date is not visible
      Known gaps: SIC codes and `ticker_overrides` are today's, not those of the past. 20-F and
      40-F filers get no past cap, nor do companies whose filings miss a class or an ADS ratio.
      A spin-off since the last balance sheet now fails both screens, live and replayed. The
      price cache's Nov 3 2026 bar is intraday, and the cache is never refetched

10c — Survivorship: measure the hole before trusting anything inside it
- [x] From EDGAR's free quarterly form indexes, count 10-K filers per year missing from
      today's universe, by size where filings allow. This is the size of the bias
      `value-screener survivorship` (`backtest/survivorship.py`): delisting notices (25-NSE, 25)
      for 10-K filers not in today's universe, sized by `dei:EntityPublicFloat` from SEC's
      frames API; 64 index downloads, cached. 5,100 delisted since 2011: 3,547 after a merger
      filing (DEFM14A, tender offer, 13E-3 or 425 within 3 years), 89 reorganized into a CIK in
      today's universe, 205 SPACs, 1,259 failed or other (bankruptcies, listing-rule failures,
      going dark, reorganizations the name test misses: Apache, Baker Hughes)
      Hole, with float of $50M+: 51-53% of what was listed in 2012, 34-46% in 2016-2019,
      27-34% in 2020-2022, under 20% from late 2023 (later dates have had less time to lose
      names). At the panel's hit rates by size, deep value would have held 57-97 more names a
      quarter in 2016-2019 next to about 95 held, so the panel tests little more than half of
      the screen; 11-18 of those were later failures. Cyclical trough: 19-32 more next to 36-57
- [x] Stress test: how many missing names, at what loss, would erase each screen's excess?
      Yearly drag if every failure the screens would have held went to zero that year:
      deep value 0.2-1.1% a year, 1.4% in 2022 and 1.9% in 2023; cyclical trough 0.1-0.7%,
      1.6% in 2023. Rough: it assumes failures were in the screens at the panel's hit rate
      (cheap and distressed names may be likelier, the F-score and coverage checks less), and
      a failure's loss lands in its last year. `erase_loss` gives the exact threshold once 10d
      has returns. The missing takeovers cut the other way: value names are bid for at a premium
      Known gaps: floats with scale slips (Party City $1T, Tofutti $11B) land in the wrong size
      bucket; the 2013-2015 rows wait on the Alphabet rebuild of those panel dates
- [ ] Decide on paid delisted prices only if results survive this far (see open decisions)
      Proposal: a deep value excess over about 3% a year (three times the worst-case drag
      outside 2022-23) survives; between 0 and 3% is inconclusive without delisted prices

10d — Backtest
- [ ] Freeze rules first: commit screen settings and these criteria before any result is seen.
      Every variant tried gets reported, not only the best
- [ ] Equal weight, rebalanced quarterly at the next close after the date; `deep_value` from
      2017 (2012 as a longer check on its own), `cyclical_trough` from the end of 2018
- [ ] Costs by size (one way: 10 bps over $10B, 30 bps $2-10B, 75 bps $300M-2B, 150 bps below),
      names under a minimum dollar volume excluded, as thin stocks can't be traded at their close
- [ ] Compare with, on the same dates and total-return basis:
      SPY (the goal), RSP and IWD; every stock in the same universe, equal weight, which
      shares its survivorship bias and so is the fairest test; and the cheapest 10% by earnings
      yield, the dumb alternative. If that matches a screen, the screen adds nothing
- [ ] Report: return and excess per year, max drawdown, worst year, stocks held per quarter,
      turnover; t-statistic with a block bootstrap, as overlapping returns aren't independent
- [ ] Sensitivity: each threshold moved up and down, `turning` on and off, 3 / 12 / 36 month
      holds, large caps only, without financials, each sector removed in turn, and by
      subperiod. An edge living in one sector or one year, or at one setting, isn't an edge

10e — Verdict
- [ ] Written result in `docs/`, including failures and what the test can't show
- [ ] Passes only if, after costs, a screen beats SPY, the same-universe equal weight and the
      cheapest-10% baseline; holds up in most subperiods and with large caps only; and its
      excess outlasts the survivorship stress test
- [ ] If it fails: say so. Don't tune until it passes, as that fits the screens to one history.
      Any change gets judged by forward tracking, not by rerunning this window

Open decisions
- Paid delisted prices (Sharadar, Norgate or similar): without them, 10d can reject a screen
  more convincingly than it can confirm one. Decide after 10c
- Costs and pass criteria above are proposals: agree them before 10d, as they can't fairly
  change after

## Known pitfalls
- Inconsistent XBRL tags (Revenues vs RevenueFromContractWithCustomer...) -> fallback lists
- Foreign filers (20-F/40-F), SPACs, ADRs often lack usable XBRL -> flag, don't fail
- Negative earnings make P/E meaningless -> treat as missing, not as "cheap"
- Share-count changes (splits/buybacks) -> use latest `dei:EntityCommonStockSharesOutstanding`
- yfinance is unofficial and may break; that's why prices are behind an interface

## Not in scope (yet)
DCF models, analyst estimates, intraday data, options.

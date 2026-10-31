# value_screener — instructions for Claude Code

## Goal
A free, self-hosted replacement for MarketInOut's undervalued/overvalued screens:
every NYSE + NASDAQ common stock, labeled by industry, scored cheap / fair / expensive
**relative to its industry peers**, refreshed weekly, viewable in a Streamlit dashboard
and exportable to CSV/Excel.

## Environment
- Windows, Python 3.12+, venv in `.venv`, install with `pip install -e .`
- Package lives in `src/value_screener/`; CLI entry point `value-screener`
- SEC requires a User-Agent: read it from `config/settings.yaml` (`sec_user_agent: "Joey Houser <email>"`)
- Respect SEC rate limit: <= 8 requests/second, with retries + backoff

## Reuse before writing
The sibling repo `../stock_research` already has working code. Read these first and
port/adapt rather than rewriting from scratch:
- `src/stock_research/edgar.py`, `edgar_xbrl.py` — EDGAR + XBRL companyfacts access
- `src/stock_research/cache.py` — on-disk caching
- `src/stock_research/fundamentals.py` — yfinance fundamentals fields
- `src/stock_research/valuescan.py` — `apply_value_filters`, `composite_value_scores`
- `app.py` — Streamlit pattern (run-button + session_state)
Copy code into this repo; do not import across repos.

## Conventions
- pandas for logic, Parquet files in `data/` for storage (DuckDB optional later)
- Every network call is cached; a re-run with a warm cache must make ~0 SEC calls
- Tests in `tests/` with pytest; mock network calls, keep a few real-ticker fixtures
- Never crash the whole run on one bad ticker: log it to `data/errors.csv` and continue

## Workflow
Build in the phase order in PLAN.md. Finish and test each phase before the next,
and update the checklist in PLAN.md as phases complete.

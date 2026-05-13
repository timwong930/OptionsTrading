# Options Trading Screener MCP

A production-oriented Python/FastMCP service and CLI for screening bullish long-call option candidates. It combines a stock technical screen, option-chain enrichment, IV-rank tracking, catalyst checks, weighted contract scoring, paper-trade templates, and a lightweight proxy backtest.

> **Important:** This project is a research and paper-trading tool only. It is **not** financial advice, an instruction to trade, or a substitute for your own due diligence, broker risk controls, or professional advice.

## What is included

| Area | Files | What it does |
| --- | --- | --- |
| MCP server and CLI | `screener_mcp.py` | Exposes tools to Claude Code/FastMCP and provides a standalone command-line entry point. |
| Market data/cache layer | `core/data.py` | Uses `yfinance` for history, sectors, expiries, and option chains; includes disk-backed TTL caching, retry logic, and optional Polygon request helper/rate limiter. |
| Technical stock screen | `core/screener.py` | Adds SMA50, SMA200, RSI14, volume breakout, and 63-trading-day relative strength versus SPY. |
| Option-chain enrichment | `core/chains.py` | Pulls call chains in a DTE window and calculates IV/Greeks/spread data locally with `py_vollib`. |
| IV rank tracking | `core/ivrank.py` | Logs daily ATM IV observations into SQLite and computes IV Rank/Percentile once enough local history exists; uses a VIX proxy fallback while history is sparse. |
| Catalyst scoring | `core/catalysts.py`, `config/sector_tailwinds.json` | Checks upcoming earnings and manually editable sector-tailwind flags. |
| Contract scoring | `core/scoring.py` | Scores contracts with weighted IVR, liquidity, spread, delta, DTE, technical, and catalyst subscores. |
| Paper-trade templates | `core/paper.py` | Converts top scored contracts into mechanical paper-trade idea records. |
| Proxy backtest | `core/backtest.py` | Backtests the underlying-stock setup as a first-pass proxy because free data sources generally do not provide historical option chains/Greeks. |
| Editable universe | `config/universe.json` | Contains the default `starter_100` watchlist and a `personal` watchlist you can customize. |

## Requirements

- Python `>=3.11,<3.13`
- `uv` recommended, or `pip` as a fallback
- Internet access for `yfinance` market data
- Optional: `POLYGON_API_KEY` if you want to use the Polygon helper in `core/data.py`

## Quick start

### 1. Clone and enter the repo

```bash
git clone <your-repo-url>
cd OptionsTrading
```

### 2. Create an environment and install dependencies

Recommended with `uv`:

```bash
uv sync
```

Pip fallback:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 3. Optional environment file

Create `.env` if you want persistent environment configuration:

```bash
cat > .env <<'EOF_ENV'
# Optional. The current pipeline primarily uses yfinance, but polygon_get()
# is available for future Polygon-backed endpoints.
POLYGON_API_KEY=

# Optional cache location. Defaults to .cache.
CACHE_DIR=.cache

# Optional config overrides. Defaults are config/universe.json and
# config/sector_tailwinds.json.
# SCREENER_CONFIG_DIR=config
# SCREENER_UNIVERSE_FILE=config/universe.json
# SCREENER_SECTOR_TAILWINDS_FILE=config/sector_tailwinds.json
EOF_ENV
```

Do **not** commit `.env` if it contains secrets.

## How to run it

### CLI: score option candidates

Use explicit tickers for a fast smoke test:

```bash
uv run python screener_mcp.py --tickers AAPL,MSFT,NVDA --top 5 --min-dte 60 --max-dte 120
```

Or with an activated virtualenv/pip install:

```bash
python screener_mcp.py --tickers AAPL,MSFT,NVDA --top 5 --min-dte 60 --max-dte 120
```

After `uv sync`, you can also use the installed console script:

```bash
uv run options-screener --tickers AAPL,MSFT,NVDA --top 5 --min-dte 60 --max-dte 120
```

The output is JSON with:

- `top`: top scored option contracts
- `errors`: ticker-level fetch/enrichment errors, if any
- `total_scored`: number of contracts scored
- `as_of`: UTC timestamp

### CLI: generate paper-trade idea templates

```bash
uv run python screener_mcp.py --tickers AAPL,MSFT,NVDA --top 3 --paper --contracts 1 --min-dte 60 --max-dte 120
```

Paper ideas include the contract symbol, expiry, strike, midpoint entry estimate, estimated debit, example paper stop/target, and setup snapshot. These are templates for tracking only; review and edit before any real-world use.

### CLI: run the proxy backtest

```bash
uv run python screener_mcp.py --tickers AAPL,MSFT,NVDA --backtest
```

This evaluates the underlying stock setup over historical data. It is **not** an historical options P&L backtest.

### CLI: use configured watchlists

If `--tickers` is omitted, the app uses the configured default watchlist in `config/universe.json`.

```bash
uv run python screener_mcp.py --watchlist starter_100 --top 10 --min-dte 60 --max-dte 120
```

### CLI: filter by sectors

Sector names must match the sector strings returned by `yfinance`, such as `Technology`, `Communication Services`, or `Healthcare`.

```bash
uv run python screener_mcp.py --tickers AAPL,MSFT,NVDA --sectors Technology --top 5 --min-dte 60 --max-dte 120
```

## MCP usage with Claude Code

Run the MCP server directly:

```bash
uv run python screener_mcp.py
```

The same server can also be launched with the installed console script after `uv sync`:

```bash
uv run options-screener
```

For Claude Code project-scope registration, add a `.mcp.json` file like this at the repo root and adjust paths for your machine:

```json
{
  "mcpServers": {
    "options-screener": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/OptionsTrading", "run", "python", "screener_mcp.py"],
      "env": {
        "CACHE_DIR": "/absolute/path/to/OptionsTrading/.cache",
        "POLYGON_API_KEY": "${POLYGON_API_KEY}"
      }
    }
  }
}
```

Then verify in Claude Code:

```bash
claude mcp list
```

Available MCP tools:

- `screen_stocks` — run the bullish technical screen with optional sector filtering.
- `get_configured_watchlist` — inspect watchlists and selected tickers.
- `get_sector_tailwind_config` — inspect sector-tailwind settings.
- `filter_by_sector` — map/filter tickers by sector.
- `analyze_option_chain` — fetch and enrich a ticker's calls for a DTE window.
- `get_iv_rank` — log ATM IV and compute IV rank/percentile or VIX proxy fallback.
- `score_candidates` — run the complete screen → chain → catalyst → score pipeline.
- `make_paper_trade_candidates` — generate mechanical paper-trade templates from top candidates.
- `backtest_score` — run the underlying-stock proxy backtest.


## Hermes agent setup

If your Hermes agent can launch stdio MCP servers, use the same server command as Claude Code and point Hermes at this project directory. Prefer an absolute path and keep the cache directory persistent so IV history survives restarts.

Generic Hermes MCP server entry:

```json
{
  "name": "options-screener",
  "transport": "stdio",
  "command": "uv",
  "args": ["--directory", "/absolute/path/to/OptionsTrading", "run", "python", "screener_mcp.py"],
  "env": {
    "CACHE_DIR": "/absolute/path/to/OptionsTrading/.cache",
    "POLYGON_API_KEY": "${POLYGON_API_KEY}"
  }
}
```

Recommended Hermes pre-flight sequence:

1. Start with `get_configured_watchlist` to confirm Hermes sees the expected tickers.
2. Run `screen_stocks` on two or three tickers before using the full default watchlist.
3. Run `analyze_option_chain` for one liquid symbol, such as `AAPL`, to confirm option-chain access.
4. Run `score_candidates` with explicit small inputs first: `tickers=["AAPL", "MSFT", "NVDA"]`, `top_n=3`, `min_dte=60`, and `max_dte=120`.
5. Treat any non-empty `errors` array as an operational warning that needs review before acting on output.

For automated Hermes workflows, do not let the agent place trades directly from this output. Keep this service as a scanner/planner, then manually verify each candidate in your broker platform.

## What to edit

### Watchlists: `config/universe.json`

- Change `default` to the watchlist name you want used when no `--tickers` or `--watchlist` is supplied.
- Add symbols to `watchlists.personal` to merge them into non-personal watchlists automatically.
- Add new named watchlists under `watchlists`.

Example:

```json
{
  "default": "my_growth_list",
  "watchlists": {
    "personal": ["YOUR", "TICKERS"],
    "my_growth_list": ["AAPL", "MSFT", "NVDA"]
  }
}
```

### Sector tailwinds: `config/sector_tailwinds.json`

Use this file to reflect your current market view. For each sector:

- `active`: `true` adds a catalyst flag for that sector.
- `score_bonus`: used by the proxy backtest setup score when the sector tailwind is active.
- `note`: free-form explanation for your own review.

Keep sector keys aligned with `yfinance` sector names.

### Scoring model: `core/scoring.py`

Edit only if you want to change the ranking philosophy:

- `WEIGHTS` controls final score weighting.
- `_piecewise(...)` point lists inside `score_contract` control how IVR, spread, and DTE are rewarded/penalized.
- The current model favors lower IVR, higher open interest, tighter spreads, 0.50–0.85 delta, 60–120 DTE, bullish technicals, and catalysts.

### DTE and contract filters: `screener_mcp.py`

Defaults are currently:

- `min_dte=60`
- `max_dte=120`
- delta must be between `0.30` and `0.90` before scoring
- CLI options `--min-dte`, `--max-dte`, and `--contracts` are validated before the pipeline runs

You can override DTE from the CLI or MCP inputs. Change the hard-coded delta pre-filter in `score_candidates` if your strategy requires a different delta range.

### Cache and IV history

- Runtime cache defaults to `.cache`.
- IV observations are stored at `.cache/iv_history.sqlite` unless `CACHE_DIR` is changed.
- IV Rank/Percentile needs at least 30 locally logged ATM IV observations for a symbol; until then, the output includes a VIX proxy fallback.

## Production-readiness checklist

Before relying on the tool every day, complete this checklist:

1. **Install and import-check dependencies**
   ```bash
   uv sync
   uv run python -m compileall screener_mcp.py core
   uv run options-screener --help
   ```
2. **Run a small live-data smoke test**
   ```bash
   uv run python screener_mcp.py --tickers AAPL,MSFT,NVDA --top 3 --min-dte 60 --max-dte 120
   ```
3. **Run paper output check**
   ```bash
   uv run python screener_mcp.py --tickers AAPL,MSFT,NVDA --top 2 --paper --contracts 1 --min-dte 60 --max-dte 120
   ```
4. **Run proxy backtest check**
   ```bash
   uv run python screener_mcp.py --tickers AAPL,MSFT,NVDA --backtest
   ```
5. **Inspect errors in JSON output**
   - Some ticker-level errors can occur when `yfinance` has missing option chains, stale earnings data, empty sectors, or transient rate limits.
   - For production operation, monitor the `errors` array and avoid treating empty results as a successful trading signal.
6. **Confirm your config**
   - Review `config/universe.json` for the exact symbols you want screened.
   - Review `config/sector_tailwinds.json` so active tailwinds match your current thesis.
7. **Confirm MCP registration**
   ```bash
   claude mcp list
   ```
8. **Do a manual brokerage cross-check**
   - Compare top candidates against your broker's real-time option chain before placing any trade.
   - Verify bid/ask, liquidity, corporate actions, upcoming events, and trading permissions.

## Operational notes and limitations

- `yfinance` is convenient but unofficial and can occasionally return incomplete, delayed, or changed-format data.
- Free data sources generally do not provide reliable historical option chains, so the included backtest is an underlying-stock setup proxy.
- Greeks are calculated locally from available option-chain fields and may differ from broker/platform Greeks.
- Bid/ask midpoint estimates are not guaranteed fills.
- Earnings dates can be missing or stale. Always verify catalysts independently.
- This project currently screens long calls only.
- Keep secrets out of git. Use `.env`, shell environment variables, or your deployment platform's secret store.

## Troubleshooting

### `yfinance returned empty history`

Usually a transient data issue, invalid symbol, market holiday edge case, or network problem. Retry later and verify the ticker.

### No contracts returned

Common causes:

- No listed options for the symbol.
- No expirations in the selected DTE window.
- Empty/stale `yfinance` option-chain response.
- The technical screen filtered the ticker out before option-chain scoring.

Try widening DTE:

```bash
uv run python screener_mcp.py --tickers AAPL --top 5 --min-dte 30 --max-dte 180
```

Then inspect the chain directly through MCP tool `analyze_option_chain` or by calling the Python functions interactively.

### IV Rank shows `insufficient_history`

That is expected at first. Run the screener regularly so `.cache/iv_history.sqlite` accumulates ATM IV observations. The tool uses the VIX proxy fallback until enough local observations exist.

### Polygon key missing

The primary CLI path uses `yfinance`. You only need `POLYGON_API_KEY` for code paths that call `polygon_get()` or `get_polygon_chain()`.

## Suggested deployment approach

For a simple production-like personal workflow:

1. Run from a dedicated virtual environment created with `uv sync`.
2. Keep `.cache` on persistent storage so IV history accumulates.
3. Keep `.env` outside source control.
4. Start with a small watchlist and expand once results are stable.
5. Log CLI JSON outputs to dated files for audit/review.
6. Cross-check every candidate with your broker before acting.

Example daily run:

```bash
mkdir -p runs
uv run python screener_mcp.py --watchlist starter_100 --top 10 --min-dte 60 --max-dte 120 > "runs/$(date -u +%Y-%m-%d)-scores.json"
uv run python screener_mcp.py --watchlist starter_100 --top 5 --paper --contracts 1 --min-dte 60 --max-dte 120 > "runs/$(date -u +%Y-%m-%d)-paper.json"
```

## End-to-end trade decision engine

Use `--analyze` to run the full ticker workflow: technical context, catalyst snapshot, normalized option chain, IV analytics, budget-aware strategy selection, and a concrete trade-management plan.

```bash
uv run python screener_mcp.py --analyze --tickers SBUX --bias bullish --budget 500 --portfolio 5000 --horizon-days 45
```

The engine prefers defined-risk trades for small accounts. It will choose a debit spread when a naked option is too expensive, a credit spread when IV is elevated and the risk fits, a cash-secured put only when collateral fits the budget, or `no_trade` when liquidity/data quality/setup is not good enough.

Key output fields include:

- `tradable`, `recommended_strategy`, `recommended_expiry`, and `recommended_legs`
- `estimated_debit` or `estimated_credit`, `max_loss`, `max_profit`, and `breakeven`
- `budget_fit`, `suggested_contract_count`, `risk_budget`, and `risk_pct_of_portfolio`
- `entry_plan`, `exit_plan`, `invalidation`, and detailed `trade_plan`
- `catalyst_summary`, `next_earnings_date`, recent earnings, dividends, headlines, and analyst actions when public data is available
- `iv_current`, `iv_rank`, `iv_percentile`, `iv_method`, and explicit fallback/data-quality flags

New MCP tools:

- `analyze_ticker_trade` — returns the end-to-end structured trade plan.
- `create_paper_trade` — stores the original recommendation and thesis in the paper journal.
- `update_paper_trade` — updates status, prices, thesis, risk, quantity, or lessons.
- `close_paper_trade` — closes a journal entry and records lessons learned.
- `list_paper_trades` — lists active/closed/all paper trades.
- `paper_trade_stats` — summarizes paper-trading results.

## Data quality and normalization

Option rows now preserve raw source values in `raw` and add normalized fields for OCC contract parsing, strike sanity checks, bid/ask/mid, spread percentage, volume, open interest, stale quote detection, liquidity flags, and malformed-contract flags. Recommendations carry `data_quality_flags` so proxied, missing, stale, inferred, or illiquid inputs are visible instead of silently trusted.

## Paper trade journal lifecycle

The persistent paper journal is SQLite-backed and defaults to `.cache/paper_trades.sqlite` via `CACHE_DIR`. A typical MCP flow is:

1. Call `analyze_ticker_trade`.
2. If the result is tradable, pass that JSON into `create_paper_trade` with your thesis.
3. Use `update_paper_trade` for notes or interim price changes.
4. Use `close_paper_trade` with exit price and lessons learned.
5. Review `paper_trade_stats` over time.

## Tests

Run the full deterministic test suite with:

```bash
python -m unittest discover -s tests -v
```

Coverage includes option normalization, catalyst fallback/summarization, IV rank/proxy behavior, budget-aware strategy selection, liquidity rejection, horizon-aware expiry selection, and paper-trade create/update/close/stats.

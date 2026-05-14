"""
Options Call Screener — Claude Code MCP server + standalone CLI.

Run as MCP:    python screener_mcp.py
Run as CLI:    python screener_mcp.py --tickers AAPL,MSFT,NVDA --top 5
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys

from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from core.backtest import backtest_stock_setup
from core.catalysts import has_catalyst
from core.chains import get_call_chain
from core.config import list_watchlists, load_sector_tailwinds, resolve_tickers
from core.data import yf_history, yf_sector
from core.ivrank import iv_rank, log_atm_iv
from core.engine import analyze_trade, score_option_candidates
from core.journal import close_trade, create_trade, list_trades, trade_stats, update_trade
from core.paper import make_paper_trade_ideas
from core.screener import screen_universe

mcp = FastMCP(name="options-screener")


@mcp.tool
def screen_stocks(
    tickers: list[str] | None = None,
    watchlist: str | None = None,
    allowed_sectors: list[str] | None = None,
    excluded_sectors: list[str] | None = None,
) -> dict:
    """
    Run the bullish technical screen on tickers with optional sector filtering.

    The screen requires price > SMA50 > SMA200, RSI 40-70, and positive
    63-trading-day relative strength versus SPY.
    """
    universe = resolve_tickers(tickers, watchlist)
    passing = screen_universe(universe, allowed_sectors, excluded_sectors)
    return {
        "universe_count": len(universe),
        "passed": passing,
        "count": len(passing),
        "as_of": dt.datetime.now(dt.UTC).isoformat(),
    }


@mcp.tool
def get_configured_watchlist(name: str | None = None) -> dict:
    """Return configured watchlist metadata and the selected ticker list."""
    tickers = resolve_tickers(None, name)
    return {"selected": name, "tickers": tickers, "count": len(tickers), **list_watchlists()}


@mcp.tool
def get_sector_tailwind_config() -> dict:
    """Return editable sector-tailwind settings used by catalyst scoring."""
    return load_sector_tailwinds()


@mcp.tool
def filter_by_sector(tickers: list[str] | None = None, sectors: list[str] | None = None, watchlist: str | None = None) -> dict:
    """Filter explicit tickers or a configured watchlist by yfinance sector."""
    universe = resolve_tickers(tickers, watchlist)
    selected_sectors = sectors or []
    out = [{"symbol": s.strip().upper(), "sector": yf_sector(s.strip().upper())} for s in universe if s.strip()]
    return {"matched": [r for r in out if r["sector"] in selected_sectors], "all": out}


@mcp.tool
def analyze_option_chain(symbol: str, min_dte: int = 60, max_dte: int = 120) -> dict:
    """Fetch and enrich a ticker's call chain for the requested DTE window."""
    sym = symbol.strip().upper()
    chain = get_call_chain(sym, min_dte=min_dte, max_dte=max_dte)
    spot = float(yf_history(sym, period="5d")["Close"].iloc[-1])
    atm = min((c for c in chain if c.get("iv")), key=lambda c: abs(c["strike"] - spot), default=None)
    if atm and atm.get("iv"):
        log_atm_iv(sym, atm["iv"])
    return {"symbol": sym, "spot": spot, "n_contracts": len(chain), "contracts": chain}


@mcp.tool
def get_iv_rank(symbol: str) -> dict:
    """Compute IV Rank/Percentile from local ATM IV history with VIX fallback."""
    sym = symbol.strip().upper()
    spot = float(yf_history(sym, period="5d")["Close"].iloc[-1])
    chain = get_call_chain(sym, min_dte=20, max_dte=45)
    atm = min((c for c in chain if c.get("iv")), key=lambda c: abs(c["strike"] - spot), default=None)
    if not atm:
        return {"symbol": sym, "error": "no ATM contract found"}
    log_atm_iv(sym, atm["iv"])
    return {"symbol": sym, "current_iv": atm["iv"], **iv_rank(sym, atm["iv"])}


@mcp.tool
def score_candidates(
    tickers: list[str] | None = None,
    min_dte: int = 60,
    max_dte: int = 120,
    top_n: int = 10,
    allowed_sectors: list[str] | None = None,
    watchlist: str | None = None,
) -> dict:
    """Run the full screen → chain → catalyst → score pipeline and return top contracts."""
    candidates: list[dict] = []
    rejects: list[dict] = []
    universe = resolve_tickers(tickers, watchlist)
    passing = screen_universe(universe, allowed_sectors=allowed_sectors)
    passing_symbols = {row.get("symbol") for row in passing if "error" not in row}
    for sym in [s.strip().upper() for s in universe if s.strip()]:
        if sym not in passing_symbols and not any(row.get("symbol") == sym and "error" in row for row in passing):
            rejects.append({"symbol": sym, "category": "stock_setup", "reason": "did not pass bullish stock screen before option-chain scoring"})
    for tech in passing:
        if "error" in tech:
            rejects.append({"symbol": tech.get("symbol"), "category": "data", "reason": tech.get("error")})
            continue
        sym = tech["symbol"]
        try:
            chain = get_call_chain(sym, min_dte, max_dte)
            liquid_chain = [c for c in chain if c.get("liquid") and not c.get("stale") and c.get("mid")]
            cat = has_catalyst(sym, max_dte, tech.get("sector"))
            spot = tech["price"]
            atm = min((c for c in chain if c.get("iv")), key=lambda c: abs(c["strike"] - spot), default=None)
            ivr_info = iv_rank(sym, atm["iv"]) if atm and atm.get("iv") else {"ivr": None, "method": "missing", "data_quality_flags": ["missing_atm_iv"]}
            scored_for_symbol = score_option_candidates(liquid_chain, tech, cat, ivr_info, "bullish")
            if not scored_for_symbol:
                flags = sorted({flag for c in chain for flag in c.get("data_quality_flags", [])})
                rejects.append({"symbol": sym, "category": "options_quality", "reason": "no liquid call contracts passed scorer delta/liquidity filters", "contracts": len(chain), "liquid_contracts": len(liquid_chain), "data_quality_flags": flags})
                continue
            candidates.extend(scored_for_symbol)
        except Exception as exc:
            rejects.append({"symbol": sym, "category": "error", "reason": str(exc)})
    scored = [c for c in candidates if "score" in c]
    scored.sort(key=lambda c: c["score"], reverse=True)
    shortlist = [
        {
            "symbol": c.get("symbol"),
            "contract": c.get("contractSymbol"),
            "expiry": c.get("expiry"),
            "strike": c.get("strike"),
            "mid": c.get("mid"),
            "score": c.get("score"),
            "rationale": f"Liquid bullish call candidate; score {c.get('score')}, delta {round(c.get('delta'), 2) if c.get('delta') is not None else None}, spread {c.get('spread_pct')}%.",
        }
        for c in scored[:top_n]
    ]
    return {
        "universe_count": len(universe),
        "top": scored[:top_n],
        "shortlist": shortlist,
        "total_scored": len(scored),
        "rejects": rejects,
        "errors": [r for r in rejects if r.get("category") in {"error", "data"}],
        "as_of": dt.datetime.now(dt.UTC).isoformat(),
    }


@mcp.tool
def make_paper_trade_candidates(
    tickers: list[str] | None = None,
    watchlist: str | None = None,
    top_n: int = 5,
    contracts: int = 1,
    min_dte: int = 60,
    max_dte: int = 120,
    allowed_sectors: list[str] | None = None,
) -> dict:
    """Return mechanical paper-trade idea templates from the top scored candidates."""
    scored = score_candidates(
        tickers=tickers,
        watchlist=watchlist,
        top_n=top_n,
        min_dte=min_dte,
        max_dte=max_dte,
        allowed_sectors=allowed_sectors,
    )
    return {
        "ideas": make_paper_trade_ideas(scored.get("top", []), max_ideas=top_n, contracts=contracts),
        "source": scored,
        "disclaimer": "Paper-trade planning template only; not financial advice or an instruction to trade.",
    }


@mcp.tool
def analyze_ticker_trade(
    symbol: str,
    bias: str = "auto",
    budget: float = 500.0,
    portfolio_value: float = 5000.0,
    horizon_days: int = 45,
) -> dict:
    """Analyze one ticker end-to-end and return a budget-aware options trade plan."""
    return analyze_trade(symbol, bias=bias, budget=budget, portfolio_value=portfolio_value, horizon_days=horizon_days)


@mcp.tool
def create_paper_trade(plan: dict, thesis: str | None = None, quantity: int | None = None) -> dict:
    """Create an active paper-trade journal entry from an analyze_ticker_trade result."""
    return create_trade(plan, thesis=thesis, quantity=quantity)


@mcp.tool
def update_paper_trade(trade_id: str, fields: dict) -> dict:
    """Update a paper trade's mutable fields (status, prices, thesis, lessons, quantity)."""
    return update_trade(trade_id, **fields)


@mcp.tool
def close_paper_trade(trade_id: str, exit_price: float, lessons: str | None = None) -> dict:
    """Close a paper trade and attach optional lessons learned."""
    return close_trade(trade_id, exit_price=exit_price, lessons=lessons)


@mcp.tool
def list_paper_trades(status: str | None = None) -> dict:
    """List paper trades, optionally filtered by active/closed status."""
    trades = list_trades(status=status)
    return {"count": len(trades), "trades": trades}


@mcp.tool
def paper_trade_stats() -> dict:
    """Return aggregate paper-trading statistics."""
    return trade_stats()


@mcp.tool
def backtest_score(
    tickers: list[str] | None = None,
    watchlist: str | None = None,
    lookback_days: int = 504,
    holding_days: int = 30,
    min_score: float = 70,
) -> dict:
    """Backtest the underlying-stock setup score as a first-pass proxy."""
    return backtest_stock_setup(
        tickers=tickers,
        watchlist=watchlist,
        lookback_days=lookback_days,
        holding_days=holding_days,
        min_score=min_score,
    )


def cli() -> None:
    """Standalone CLI for local smoke tests before using the MCP server."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default="", help="comma-separated tickers; falls back to configured watchlist")
    parser.add_argument("--watchlist", default=None, help="configured watchlist name, defaults to config/universe.json")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--min-dte", type=int, default=60, help="minimum option days to expiration for scoring")
    parser.add_argument("--max-dte", type=int, default=120, help="maximum option days to expiration for scoring")
    parser.add_argument("--contracts", type=int, default=1, help="paper-trade contract count when --paper is used")
    parser.add_argument("--sectors", help="comma-separated sector allowlist", default="")
    parser.add_argument("--paper", action="store_true", help="emit paper-trade idea templates")
    parser.add_argument("--backtest", action="store_true", help="run the stock setup proxy backtest")
    parser.add_argument("--analyze", action="store_true", help="run end-to-end trade engine for the first ticker")
    parser.add_argument("--budget", type=float, default=500.0, help="maximum dollars risked on the idea")
    parser.add_argument("--portfolio", type=float, default=5000.0, help="paper portfolio value for sizing")
    parser.add_argument("--bias", default="auto", choices=["auto", "bullish", "bearish", "neutral"], help="directional bias override")
    parser.add_argument("--horizon-days", type=int, default=45, help="expected trade horizon for the end-to-end analyzer")
    args = parser.parse_args()
    if args.min_dte <= 0 or args.max_dte <= 0:
        parser.error("--min-dte and --max-dte must be positive integers")
    if args.min_dte > args.max_dte:
        parser.error("--min-dte cannot be greater than --max-dte")
    if args.contracts <= 0:
        parser.error("--contracts must be a positive integer")

    sectors = [s.strip() for s in args.sectors.split(",") if s.strip()] or None
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] or None
    if args.analyze:
        selected = (tickers or resolve_tickers(None, args.watchlist))
        if not selected:
            parser.error("--analyze requires --tickers or configured watchlist")
        out = analyze_ticker_trade(selected[0], bias=args.bias, budget=args.budget, portfolio_value=args.portfolio, horizon_days=args.horizon_days)
    elif args.backtest:
        out = backtest_score(tickers=tickers, watchlist=args.watchlist)
    elif args.paper:
        out = make_paper_trade_candidates(
            tickers=tickers,
            watchlist=args.watchlist,
            top_n=args.top,
            contracts=args.contracts,
            min_dte=args.min_dte,
            max_dte=args.max_dte,
            allowed_sectors=sectors,
        )
    else:
        out = score_candidates(
            tickers=tickers,
            watchlist=args.watchlist,
            top_n=args.top,
            min_dte=args.min_dte,
            max_dte=args.max_dte,
            allowed_sectors=sectors,
        )
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli()
    else:
        mcp.run()

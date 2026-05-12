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
from core.paper import make_paper_trade_ideas
from core.scoring import score_contract
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
    universe = resolve_tickers(tickers, watchlist)
    passing = screen_universe(universe, allowed_sectors=allowed_sectors)
    for tech in passing:
        if "error" in tech:
            candidates.append(tech)
            continue
        sym = tech["symbol"]
        try:
            chain = get_call_chain(sym, min_dte, max_dte)
            cat = has_catalyst(sym, max_dte, tech.get("sector"))
            spot = tech["price"]
            atm = min((c for c in chain if c.get("iv")), key=lambda c: abs(c["strike"] - spot), default=None)
            ivr_info = iv_rank(sym, atm["iv"]) if atm and atm.get("iv") else {"ivr": None}
            for c in chain:
                if c.get("delta") is None:
                    continue
                if not (0.30 <= c["delta"] <= 0.90):
                    continue
                score = score_contract(c, tech, cat, ivr_info)
                candidates.append({**c, **score, "technical": tech, "catalyst": cat, "ivr": ivr_info})
        except Exception as exc:
            candidates.append({"symbol": sym, "error": str(exc)})
    scored = [c for c in candidates if "score" in c]
    scored.sort(key=lambda c: c["score"], reverse=True)
    return {
        "universe_count": len(universe),
        "top": scored[:top_n],
        "total_scored": len(scored),
        "errors": [c for c in candidates if "error" in c],
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
    parser.add_argument("--sectors", help="comma-separated sector allowlist", default="")
    parser.add_argument("--paper", action="store_true", help="emit paper-trade idea templates")
    parser.add_argument("--backtest", action="store_true", help="run the stock setup proxy backtest")
    args = parser.parse_args()
    sectors = [s.strip() for s in args.sectors.split(",") if s.strip()] or None
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] or None
    if args.backtest:
        out = backtest_score(tickers=tickers, watchlist=args.watchlist)
    elif args.paper:
        out = make_paper_trade_candidates(tickers=tickers, watchlist=args.watchlist, top_n=args.top, allowed_sectors=sectors)
    else:
        out = score_candidates(tickers=tickers, watchlist=args.watchlist, top_n=args.top, allowed_sectors=sectors)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli()
    else:
        mcp.run()

"""Lightweight price-proxy backtesting for the screener's stock setup score."""

from __future__ import annotations

import statistics
from typing import Any

import pandas as pd

from .config import get_sector_tailwind, resolve_tickers
from .data import yf_history, yf_sector
from .screener import add_indicators


def _rs_series(symbol_close: pd.Series, spy_close: pd.Series, lookback: int = 63) -> pd.Series:
    aligned = pd.concat([symbol_close.rename("symbol"), spy_close.rename("spy")], axis=1).dropna()
    symbol_ret = aligned["symbol"] / aligned["symbol"].shift(lookback) - 1
    spy_ret = aligned["spy"] / aligned["spy"].shift(lookback) - 1
    return symbol_ret - spy_ret


def _setup_score(row: pd.Series, rs: float, sector_bonus: float = 0) -> float:
    trend = 40 if row["Close"] > row["SMA50"] > row["SMA200"] else 0
    rsi = 20 if 40 <= row["RSI14"] <= 70 else 0
    rel_strength = 25 if rs > 0 else 0
    volume = 5 if bool(row.get("VolBreak", False)) else 0
    return min(100.0, trend + rsi + rel_strength + volume + sector_bonus)


def backtest_stock_setup(
    tickers: list[str] | None = None,
    watchlist: str | None = None,
    lookback_days: int = 504,
    holding_days: int = 30,
    min_score: float = 70,
) -> dict[str, Any]:
    """
    Backtest the stock-side setup as a proxy for option candidate quality.

    Free sources generally do not provide historical option chains/IV/Greeks, so
    this backtest evaluates whether the underlying stock setup would have led to
    positive forward underlying returns. It is a first-pass proxy, not an options
    P&L backtest.
    """
    universe = resolve_tickers(tickers, watchlist)
    spy = yf_history("SPY", period="3y")
    spy_close = spy["Close"]
    trades: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for sym in universe:
        try:
            df = add_indicators(yf_history(sym, period="3y")).tail(lookback_days + 220)
            sector = yf_sector(sym)
            tailwind = get_sector_tailwind(sector)
            bonus = float(tailwind.get("score_bonus") or 0) if tailwind.get("active") else 0.0
            rs = _rs_series(df["Close"], spy_close)
            eval_df = df.copy()
            eval_df["RS"] = rs.reindex(eval_df.index)
            eval_df = eval_df.dropna(subset=["SMA200", "RS"])
            eval_df = eval_df.tail(lookback_days)
            for idx in range(0, max(0, len(eval_df) - holding_days), holding_days):
                row = eval_df.iloc[idx]
                score = _setup_score(row, float(row["RS"]), bonus)
                if score < min_score:
                    continue
                exit_row = eval_df.iloc[idx + holding_days]
                entry = float(row["Close"])
                exit_price = float(exit_row["Close"])
                ret = (exit_price / entry - 1) * 100
                trades.append(
                    {
                        "symbol": sym,
                        "entry_date": str(row.name.date()),
                        "exit_date": str(exit_row.name.date()),
                        "entry_price": round(entry, 2),
                        "exit_price": round(exit_price, 2),
                        "return_pct": round(ret, 2),
                        "setup_score": round(score, 1),
                        "sector": sector,
                        "sector_tailwind_active": bool(tailwind.get("active")),
                    }
                )
        except Exception as exc:
            errors.append({"symbol": sym, "error": str(exc)})

    returns = [t["return_pct"] for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    summary = {
        "trade_count": len(trades),
        "win_rate_pct": round(len(wins) / len(returns) * 100, 1) if returns else 0,
        "avg_return_pct": round(statistics.fmean(returns), 2) if returns else 0,
        "median_return_pct": round(statistics.median(returns), 2) if returns else 0,
        "avg_win_pct": round(statistics.fmean(wins), 2) if wins else 0,
        "avg_loss_pct": round(statistics.fmean(losses), 2) if losses else 0,
    }
    return {
        "summary": summary,
        "trades": trades,
        "errors": errors,
        "assumption": "Underlying-stock forward-return proxy; not a historical options P&L backtest.",
    }

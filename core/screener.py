"""Stock-level technical screening and sector filtering."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .data import yf_history, yf_sector


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add SMA, RSI, and volume-breakout columns to OHLCV history."""
    df = df.copy()
    df["SMA50"] = df["Close"].rolling(50).mean()
    df["SMA200"] = df["Close"].rolling(200).mean()

    delta = df["Close"].diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    df["RSI14"] = 100 - (100 / (1 + rs))

    df["VolAvg20"] = df["Volume"].rolling(20).mean()
    df["VolBreak"] = df["Volume"] > 1.5 * df["VolAvg20"]
    return df


def relative_strength(symbol: str, spy_hist: pd.DataFrame, lookback: int = 63) -> float:
    """Return ticker return minus SPY return over the lookback window."""
    s = yf_history(symbol, period="6mo")
    if len(s) <= lookback or len(spy_hist) <= lookback:
        raise ValueError(f"not enough history to compute {lookback}-day relative strength for {symbol}")
    s_ret = s["Close"].iloc[-1] / s["Close"].iloc[-lookback] - 1
    spy_ret = spy_hist["Close"].iloc[-1] / spy_hist["Close"].iloc[-lookback] - 1
    return float(s_ret - spy_ret)


def technical_setup(symbol: str, spy_hist: pd.DataFrame) -> dict:
    """Compute the bullish-trend technical setup for one ticker."""
    df = add_indicators(yf_history(symbol, period="1y"))
    last = df.iloc[-1]
    rs = relative_strength(symbol, spy_hist)
    bullish = bool(
        last["Close"] > last["SMA50"] > last["SMA200"]
        and 40 <= last["RSI14"] <= 70
        and rs > 0
    )
    return {
        "symbol": symbol,
        "sector": yf_sector(symbol),
        "price": float(last["Close"]),
        "sma50": float(last["SMA50"]),
        "sma200": float(last["SMA200"]),
        "rsi14": float(last["RSI14"]),
        "vol_breakout": bool(last["VolBreak"]),
        "rel_strength_vs_spy_63d": rs,
        "bullish": bullish,
    }


def screen_universe(
    symbols: list[str],
    allowed_sectors: list[str] | None = None,
    excluded_sectors: list[str] | None = None,
) -> list[dict]:
    """Return tickers passing the bullish trend screen, optionally sector-filtered."""
    out: list[dict] = []
    try:
        spy = yf_history("SPY", period="6mo")
    except Exception as exc:
        return [
            {"symbol": raw_sym.strip().upper(), "error": f"failed to fetch SPY benchmark history: {exc}"}
            for raw_sym in symbols
            if raw_sym.strip()
        ]
    for raw_sym in symbols:
        sym = raw_sym.strip().upper()
        if not sym:
            continue
        try:
            result = technical_setup(sym, spy)
        except Exception as exc:
            out.append({"symbol": sym, "error": str(exc)})
            continue
        if allowed_sectors and result["sector"] not in allowed_sectors:
            continue
        if excluded_sectors and result["sector"] in excluded_sectors:
            continue
        if result.get("bullish"):
            out.append(result)
    return out

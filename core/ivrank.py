"""Self-collected IV Rank/Percentile with a VIX proxy fallback."""

from __future__ import annotations

import datetime as dt
import os
import sqlite3

from .data import yf_history

DB = os.path.join(os.getenv("CACHE_DIR", ".cache"), "iv_history.sqlite")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS iv_log
           (symbol TEXT, date TEXT, atm_iv REAL,
            PRIMARY KEY (symbol, date))"""
    )
    return conn


def log_atm_iv(symbol: str, atm_iv: float) -> None:
    """Record today's ATM IV observation for future IVR/IVP calculations."""
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO iv_log VALUES (?, ?, ?)",
            (symbol.upper(), dt.date.today().isoformat(), float(atm_iv)),
        )


def iv_rank(symbol: str, current_iv: float, lookback_days: int = 252) -> dict:
    """Compute IV Rank/Percentile from locally collected ATM IV observations."""
    cutoff = (dt.date.today() - dt.timedelta(days=int(lookback_days * 1.5))).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT atm_iv FROM iv_log WHERE symbol=? AND date>=? ORDER BY date",
            (symbol.upper(), cutoff),
        ).fetchall()
    history = [float(row[0]) for row in rows]
    if len(history) < 30:
        return {
            "ivr": None,
            "ivp": None,
            "method": "insufficient_history",
            "n_obs": len(history),
            "fallback": vix_proxy_ivr(current_iv),
        }

    hi = max(history)
    lo = min(history)
    ivr = (current_iv - lo) / (hi - lo) * 100 if hi > lo else 50.0
    ivp = sum(1 for x in history if x < current_iv) / len(history) * 100
    return {"ivr": round(ivr, 1), "ivp": round(ivp, 1), "method": "self_collected", "n_obs": len(history)}


def vix_proxy_ivr(current_iv: float) -> dict:
    """Return a crude VIX-range proxy until local single-name IV history matures."""
    vix = yf_history("^VIX", period="1y")["Close"]
    vix_lo = vix.min() / 100
    vix_hi = vix.max() / 100
    floor = 0.7 * vix_lo
    ceil = 3.0 * vix_hi
    ivr = max(0, min(100, (current_iv - floor) / (ceil - floor) * 100))
    return {
        "proxy_ivr": round(float(ivr), 1),
        "vix_today": float(vix.iloc[-1]),
        "note": "Self-collect 252 days for real IVR",
    }

"""Self-collected IV Rank/Percentile with explicit proxy and history quality labels."""

from __future__ import annotations

import datetime as dt
import os
import sqlite3

from .data import yf_history

DB = os.path.join(os.getenv("CACHE_DIR", ".cache"), "iv_history.sqlite")
MIN_REAL_OBS = 30


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS iv_log
           (symbol TEXT, date TEXT, atm_iv REAL,
            PRIMARY KEY (symbol, date))"""
    )
    return conn


def log_atm_iv(symbol: str, atm_iv: float, date: str | None = None) -> None:
    """Record an ATM IV observation for future IVR/IVP calculations."""
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO iv_log VALUES (?, ?, ?)",
            (symbol.upper(), date or dt.date.today().isoformat(), float(atm_iv)),
        )


def iv_rank_from_history(current_iv: float, history: list[float]) -> dict:
    """Compute IV rank and percentile from an explicit history vector."""
    clean = [float(x) for x in history if x is not None and float(x) >= 0]
    if len(clean) < MIN_REAL_OBS:
        return {
            "iv_rank": None,
            "iv_percentile": None,
            "ivr": None,
            "ivp": None,
            "method": "insufficient_history",
            "n_obs": len(clean),
            "history_quality": "insufficient_history",
            "data_quality_flags": ["insufficient_iv_history"],
        }
    hi = max(clean)
    lo = min(clean)
    ivr = (current_iv - lo) / (hi - lo) * 100 if hi > lo else 50.0
    ivp = sum(1 for x in clean if x < current_iv) / len(clean) * 100
    ivr = round(max(0.0, min(100.0, ivr)), 1)
    ivp = round(max(0.0, min(100.0, ivp)), 1)
    return {
        "iv_rank": ivr,
        "iv_percentile": ivp,
        "ivr": ivr,
        "ivp": ivp,
        "method": "self_collected",
        "n_obs": len(clean),
        "history_quality": "full" if len(clean) >= 252 else "thin_but_usable",
        "data_quality_flags": [] if len(clean) >= 252 else ["thin_iv_history"],
    }


def iv_rank(symbol: str, current_iv: float, lookback_days: int = 252) -> dict:
    """Compute IV Rank/Percentile from local ATM IV observations with VIX fallback."""
    cutoff = (dt.date.today() - dt.timedelta(days=int(lookback_days * 1.5))).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT atm_iv FROM iv_log WHERE symbol=? AND date>=? ORDER BY date",
            (symbol.upper(), cutoff),
        ).fetchall()
    history = [float(row[0]) for row in rows]
    result = iv_rank_from_history(current_iv, history)
    result["current_iv"] = current_iv
    if result["method"] == "insufficient_history":
        result["fallback"] = vix_proxy_ivr(current_iv)
    return result


def vix_proxy_ivr(current_iv: float) -> dict:
    """Return a crude VIX-range proxy until local single-name IV history matures."""
    try:
        vix = yf_history("^VIX", period="1y")["Close"]
        vix_lo = float(vix.min()) / 100
        vix_hi = float(vix.max()) / 100
        floor = 0.7 * vix_lo
        ceil = 3.0 * vix_hi
        ivr = max(0, min(100, (current_iv - floor) / (ceil - floor) * 100)) if ceil > floor else 50.0
        return {
            "proxy_ivr": round(float(ivr), 1),
            "proxy_iv_percentile": None,
            "vix_today": float(vix.iloc[-1]),
            "method": "vix_proxy",
            "note": "Proxy only: self-collect 252 daily ATM IV observations for real single-name IVR/IVP.",
        }
    except Exception as exc:
        return {"proxy_ivr": None, "method": "unavailable", "note": f"VIX proxy unavailable: {exc}"}

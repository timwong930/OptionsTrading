"""Options-chain retrieval and local Greek enrichment."""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable

from py_vollib.black_scholes.greeks.analytical import delta, gamma, theta, vega
from py_vollib.black_scholes.implied_volatility import implied_volatility

from .data import polygon_get, yf_history, yf_option_chain, yf_option_expiries

RISK_FREE = 0.045


def _safe(fn: Callable[..., float], *args: Any) -> float | None:
    try:
        return float(fn(*args))
    except Exception:
        return None


def enrich_with_greeks(c: dict, spot: float, today: dt.date, opt_type: str = "c") -> dict:
    """Add IV, Greeks, DTE, and spread percentage to an option-chain row."""
    strike = float(c["strike"])
    expiry = dt.datetime.strptime(c["expiry"], "%Y-%m-%d").date()
    dte = max((expiry - today).days, 1)
    t_years = dte / 365.0
    sigma = float(c.get("impliedVolatility") or 0.0)
    bid = float(c.get("bid") or 0.0)
    ask = float(c.get("ask") or 0.0)
    mid = (bid + ask) / 2.0

    if (not sigma or sigma <= 0) and mid > 0:
        sigma = _safe(implied_volatility, mid, spot, strike, t_years, RISK_FREE, opt_type) or 0.0

    c["iv"] = sigma
    c["delta"] = _safe(delta, opt_type, spot, strike, t_years, RISK_FREE, sigma) if sigma > 0 else None
    c["gamma"] = _safe(gamma, opt_type, spot, strike, t_years, RISK_FREE, sigma) if sigma > 0 else None
    c["theta"] = _safe(theta, opt_type, spot, strike, t_years, RISK_FREE, sigma) if sigma > 0 else None
    c["vega"] = _safe(vega, opt_type, spot, strike, t_years, RISK_FREE, sigma) if sigma > 0 else None
    c["dte"] = dte
    c["spread_pct"] = ((ask - bid) / mid * 100) if mid else None
    return c


def get_call_chain(symbol: str, min_dte: int = 60, max_dte: int = 120) -> list[dict]:
    """Fetch calls within a DTE window and enrich them with local Greeks."""
    today = dt.date.today()
    expiries = yf_option_expiries(symbol)
    target = [
        expiry
        for expiry in expiries
        if min_dte <= (dt.datetime.strptime(expiry, "%Y-%m-%d").date() - today).days <= max_dte
    ]
    if not target:
        return []

    spot = float(yf_history(symbol, period="5d")["Close"].iloc[-1])
    out: list[dict] = []
    for expiry in target:
        chain = yf_option_chain(symbol, expiry)
        for row in chain["calls"]:
            row["expiry"] = expiry
            row["symbol"] = symbol
            out.append(enrich_with_greeks(row, spot, today, "c"))
    return out


def get_polygon_chain(symbol: str) -> list[dict]:
    """Fetch a Polygon full options snapshot for optional fresher Greeks/IV."""
    data = polygon_get(f"/v3/snapshot/options/{symbol}")
    return data.get("results", [])

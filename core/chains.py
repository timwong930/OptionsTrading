"""Options-chain retrieval, normalization, liquidity checks, and Greek enrichment."""

from __future__ import annotations

import datetime as dt
import math
import re
from typing import Any, Callable

from py_vollib.black_scholes.greeks.analytical import delta, gamma, theta, vega
from py_vollib.black_scholes.implied_volatility import implied_volatility

from .data import polygon_get, yf_history, yf_option_chain, yf_option_expiries

RISK_FREE = 0.045
OCC_RE = re.compile(r"^(?P<root>[A-Z]{1,6})(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<type>[CP])(?P<strike>\d{8})$")


def _safe(fn: Callable[..., float], *args: Any) -> float | None:
    try:
        val = fn(*args)
        return float(val) if val is not None and math.isfinite(float(val)) else None
    except Exception:
        return None


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def parse_occ_contract(contract_symbol: str | None) -> dict[str, Any]:
    """Parse an OCC/yfinance option symbol and report malformed contracts."""
    if not contract_symbol:
        return {"malformed_contract": True, "contract_parse_error": "missing_contract_symbol"}
    match = OCC_RE.match(str(contract_symbol).upper().replace(" ", ""))
    if not match:
        return {"malformed_contract": True, "contract_parse_error": "not_occ_format"}
    parts = match.groupdict()
    year = 2000 + int(parts["yy"])
    try:
        expiry = dt.date(year, int(parts["mm"]), int(parts["dd"])).isoformat()
    except ValueError:
        return {"malformed_contract": True, "contract_parse_error": "invalid_expiry"}
    return {
        "underlying_from_contract": parts["root"],
        "expiry_from_contract": expiry,
        "type_from_contract": "call" if parts["type"] == "C" else "put",
        "strike_from_contract": int(parts["strike"]) / 1000.0,
        "malformed_contract": False,
        "contract_parse_error": None,
    }


def normalize_option_row(
    row: dict[str, Any],
    symbol: str,
    expiry: str,
    option_type: str,
    spot: float | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Normalize one raw option row while preserving source values and data-quality flags."""
    today = today or dt.date.today()
    flags: list[str] = []
    raw = dict(row)
    parsed = parse_occ_contract(row.get("contractSymbol"))
    if parsed.get("malformed_contract"):
        flags.append("malformed_contract")

    raw_strike = _num(row.get("strike"))
    parsed_strike = parsed.get("strike_from_contract")
    strike = raw_strike if raw_strike is not None else parsed_strike
    if strike is None:
        flags.append("missing_strike")
        strike = 0.0
    if parsed_strike is not None and raw_strike is not None and abs(raw_strike - parsed_strike) > 0.01:
        if raw_strike > 1000 and abs(raw_strike / 1000 - parsed_strike) <= 0.01:
            flags.append("strike_scaled_from_raw")
            strike = parsed_strike
        else:
            flags.append("strike_mismatch_contract")
    if spot and strike and (strike > spot * 8 or strike < spot * 0.05):
        flags.append("strike_scaling_suspect")

    bid = max(_num(row.get("bid")) or 0.0, 0.0)
    ask = max(_num(row.get("ask")) or 0.0, 0.0)
    last = _num(row.get("lastPrice"))
    if ask and bid > ask:
        flags.append("crossed_market")
    mid = round((bid + ask) / 2.0, 4) if bid > 0 or ask > 0 else (round(last, 4) if last and last > 0 else None)
    spread = round(max(ask - bid, 0.0), 4) if ask or bid else None
    spread_pct = round(spread / mid * 100, 2) if spread is not None and mid and mid > 0 else None

    volume = int(_num(row.get("volume")) or 0)
    open_interest = int(_num(row.get("openInterest")) or 0)
    iv = _num(row.get("impliedVolatility"))

    last_trade = row.get("lastTradeDate")
    stale = False
    if last_trade is None:
        stale = True
        flags.append("missing_last_trade_date")
    else:
        try:
            if hasattr(last_trade, "to_pydatetime"):
                last_dt = last_trade.to_pydatetime().date()
            elif isinstance(last_trade, dt.datetime):
                last_dt = last_trade.date()
            elif isinstance(last_trade, dt.date):
                last_dt = last_trade
            else:
                last_dt = dt.datetime.fromisoformat(str(last_trade).replace("Z", "+00:00")).date()
            if (today - last_dt).days > 5:
                stale = True
                flags.append("stale_contract")
        except Exception:
            stale = True
            flags.append("unparseable_last_trade_date")

    if bid <= 0 and ask <= 0:
        flags.append("no_quoted_market")
    if spread_pct is None or spread_pct > 20:
        flags.append("wide_spread")
    if open_interest < 100:
        flags.append("low_open_interest")
    if volume < 10:
        flags.append("low_volume")

    liquid = not any(f in flags for f in ["wide_spread", "low_open_interest", "no_quoted_market", "stale_contract", "malformed_contract"])
    expiry_date = dt.date.fromisoformat(expiry)
    return {
        "symbol": symbol.upper(),
        "contractSymbol": row.get("contractSymbol"),
        "option_type": option_type,
        "expiry": expiry,
        "strike": float(strike),
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "lastPrice": last,
        "spread": spread,
        "spread_pct": spread_pct,
        "volume": volume,
        "openInterest": open_interest,
        "iv": iv,
        "impliedVolatility": iv,
        "inTheMoney": bool(row.get("inTheMoney", False)),
        "lastTradeDate": str(last_trade) if last_trade is not None else None,
        "dte": max((expiry_date - today).days, 0),
        "liquid": liquid,
        "stale": stale,
        "data_quality_flags": sorted(set(flags)),
        "raw": raw,
        **parsed,
    }


def enrich_with_greeks(c: dict, spot: float, today: dt.date, opt_type: str = "c") -> dict:
    """Add IV, Greeks, DTE, and normalized market-quality fields to an option row."""
    option_type = "call" if opt_type.lower().startswith("c") else "put"
    normalized = normalize_option_row(c, c.get("symbol", ""), c["expiry"], option_type, spot, today)
    strike = float(normalized["strike"])
    dte = max(int(normalized["dte"]), 1)
    t_years = dte / 365.0
    sigma = float(normalized.get("iv") or 0.0)
    mid = normalized.get("mid") or 0.0

    if (not sigma or sigma <= 0) and mid > 0 and strike > 0:
        sigma = _safe(implied_volatility, mid, spot, strike, t_years, RISK_FREE, opt_type[0].lower()) or 0.0
        if sigma:
            normalized["data_quality_flags"].append("iv_backfilled_from_mid")

    normalized["iv"] = sigma or None
    normalized["impliedVolatility"] = sigma or None
    normalized["delta"] = _safe(delta, opt_type[0].lower(), spot, strike, t_years, RISK_FREE, sigma) if sigma > 0 and strike > 0 else None
    normalized["gamma"] = _safe(gamma, opt_type[0].lower(), spot, strike, t_years, RISK_FREE, sigma) if sigma > 0 and strike > 0 else None
    normalized["theta"] = _safe(theta, opt_type[0].lower(), spot, strike, t_years, RISK_FREE, sigma) if sigma > 0 and strike > 0 else None
    normalized["vega"] = _safe(vega, opt_type[0].lower(), spot, strike, t_years, RISK_FREE, sigma) if sigma > 0 and strike > 0 else None
    normalized["data_quality_flags"] = sorted(set(normalized["data_quality_flags"]))
    return normalized


def get_option_chain(symbol: str, min_dte: int = 30, max_dte: int = 120, option_types: tuple[str, ...] = ("call", "put")) -> list[dict]:
    """Fetch calls/puts within a DTE window and enrich with normalization and Greeks."""
    today = dt.date.today()
    expiries = yf_option_expiries(symbol)
    target = [e for e in expiries if min_dte <= (dt.date.fromisoformat(e) - today).days <= max_dte]
    if not target:
        return []

    spot = float(yf_history(symbol, period="5d")["Close"].iloc[-1])
    out: list[dict] = []
    for expiry in target:
        chain = yf_option_chain(symbol, expiry)
        if "call" in option_types:
            for row in chain.get("calls", []):
                row = {**row, "expiry": expiry, "symbol": symbol}
                out.append(enrich_with_greeks(row, spot, today, "c"))
        if "put" in option_types:
            for row in chain.get("puts", []):
                row = {**row, "expiry": expiry, "symbol": symbol}
                out.append(enrich_with_greeks(row, spot, today, "p"))
    return out


def get_call_chain(symbol: str, min_dte: int = 60, max_dte: int = 120) -> list[dict]:
    """Fetch calls within a DTE window and enrich them with normalized fields and Greeks."""
    return get_option_chain(symbol, min_dte, max_dte, ("call",))


def get_polygon_chain(symbol: str) -> list[dict]:
    """Fetch a Polygon full options snapshot for optional fresher Greeks/IV."""
    data = polygon_get(f"/v3/snapshot/options/{symbol}")
    return data.get("results", [])

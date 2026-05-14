"""Weighted scoring engine for long-call candidates."""

from __future__ import annotations


def _piecewise(x: float, points: list[tuple[float, float]]) -> float:
    """Linear piecewise interpolation across sorted (x, y) points."""
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[0][1] if x < points[0][0] else points[-1][1]


WEIGHTS = {
    "ivr": 0.20,
    "oi": 0.15,
    "spread": 0.15,
    "delta": 0.15,
    "dte": 0.10,
    "tech": 0.15,
    "catalyst": 0.10,
}


def score_contract(c: dict, tech: dict, cat: dict, ivr_info: dict) -> dict:
    """Score a single enriched call contract against the seven-criterion model."""
    ivr = ivr_info.get("ivr")
    if ivr is None:
        ivr = ivr_info.get("iv_rank")
    if ivr is None:
        ivr = ivr_info.get("fallback", {}).get("proxy_ivr")
    if ivr is None:
        ivr = 50

    open_interest = c.get("openInterest") or 0
    spread_pct = c.get("spread_pct") if c.get("spread_pct") is not None else 99
    delta = c.get("delta")
    dte = c.get("dte")
    if dte is None and c.get("expiry"):
        import datetime as dt

        dte = max((dt.date.fromisoformat(c["expiry"]) - dt.date.today()).days, 0)
    dte = dte if dte is not None else 0

    sub = {
        "ivr": _piecewise(float(ivr), [(0, 100), (30, 100), (60, 0), (100, 0)]),
        "oi": 100 if open_interest >= 500 else open_interest / 5,
        "spread": _piecewise(float(spread_pct), [(0, 100), (2, 100), (5, 50), (8, 0), (99, 0)]),
        "delta": 100 if delta is not None and 0.50 <= delta <= 0.85 else 0,
        "dte": _piecewise(float(dte), [(0, 0), (60, 100), (120, 100), (180, 0)]),
        "tech": 100 if tech.get("bullish") else 0,
        "catalyst": 100 if cat.get("catalyst") else 30,
    }
    final = sum(WEIGHTS[k] * sub[k] for k in WEIGHTS)
    return {"score": round(final, 1), "subscores": {k: round(v, 1) for k, v in sub.items()}}

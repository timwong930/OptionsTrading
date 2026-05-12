"""Catalyst detection for options candidates."""

from __future__ import annotations

import datetime as dt

import yfinance as yf

from .config import get_sector_tailwind
from .data import ttl_cache


@ttl_cache(seconds=86400)
def next_earnings(symbol: str) -> str | None:
    """Return the next known earnings date as YYYY-MM-DD, if available."""
    try:
        earnings_dates = yf.Ticker(symbol).get_earnings_dates(limit=8)
        if earnings_dates is None or earnings_dates.empty:
            return None
        future = earnings_dates[earnings_dates.index.date >= dt.date.today()]
        return future.index[0].date().isoformat() if len(future) else None
    except Exception:
        return None


def has_catalyst(symbol: str, dte_max: int, sector: str | None = None) -> dict:
    """Return whether earnings or a configured sector tailwind creates a catalyst."""
    earnings = next_earnings(symbol)
    earnings_in_window = False
    earnings_in_days = None
    if earnings:
        earnings_in_days = (dt.date.fromisoformat(earnings) - dt.date.today()).days
        earnings_in_window = 0 <= earnings_in_days <= dte_max

    sector_tailwind = get_sector_tailwind(sector)
    tailwind_active = bool(sector_tailwind.get("active"))
    return {
        "earnings_date": earnings,
        "earnings_in_days": earnings_in_days,
        "earnings_catalyst": earnings_in_window,
        "sector_tailwind": sector_tailwind,
        "catalyst": earnings_in_window or tailwind_active,
    }

"""Market-data fetchers, cache helpers, and Polygon rate limiting."""

from __future__ import annotations

import logging
import os
import time
from functools import wraps
from typing import Any, Callable, TypeVar

import diskcache as dc
import requests
import yfinance as yf
from tenacity import before_sleep_log, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = logging.getLogger("screener")

CACHE_DIR = os.getenv("CACHE_DIR", ".cache")
cache = dc.Cache(CACHE_DIR)
POLY_KEY = os.getenv("POLYGON_API_KEY", "")

T = TypeVar("T")


def ttl_cache(seconds: int) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Disk-backed TTL cache decorator that survives process restarts."""

    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            key = (fn.__name__, args, tuple(sorted(kwargs.items())))
            hit = cache.get(key)
            if hit is not None:
                ts, val = hit
                if time.time() - ts < seconds:
                    return val
            val = fn(*args, **kwargs)
            cache.set(key, (time.time(), val))
            return val

        return wrapper

    return deco


class PolygonRateLimiter:
    """Hard-enforce N calls per rolling time window for Polygon's free tier."""

    def __init__(self, max_calls: int = 5, window: float = 60.0) -> None:
        self.max_calls = max_calls
        self.window = window
        self.calls: list[float] = []

    def wait(self) -> None:
        now = time.time()
        self.calls = [t for t in self.calls if now - t < self.window]
        if len(self.calls) >= self.max_calls:
            sleep_for = self.window - (now - self.calls[0]) + 0.2
            log.info("Polygon rate limit reached, sleeping %.1fs", sleep_for)
            time.sleep(max(sleep_for, 0))
        self.calls.append(time.time())


_poly_limiter = PolygonRateLimiter()


class PolygonRateError(Exception):
    """Raised when Polygon returns a rate-limit response."""


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((requests.HTTPError, PolygonRateError)),
    before_sleep=before_sleep_log(log, logging.WARNING),
)
def polygon_get(path: str, **params: Any) -> dict[str, Any]:
    """Run a robust Polygon GET with free-tier rate limiting and retries."""
    if not POLY_KEY:
        raise RuntimeError("POLYGON_API_KEY missing — set it in .env or your shell environment")
    _poly_limiter.wait()
    params["apiKey"] = POLY_KEY
    response = requests.get(f"https://api.polygon.io{path}", params=params, timeout=20)
    if response.status_code == 429:
        raise PolygonRateError("429 from Polygon — backing off")
    response.raise_for_status()
    return response.json()


@ttl_cache(seconds=3600)
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    before_sleep=before_sleep_log(log, logging.WARNING),
)
def yf_history(symbol: str, period: str = "1y", interval: str = "1d"):
    """Fetch yfinance OHLCV history, cached by symbol/period/interval."""
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval=interval, auto_adjust=False)
    if df.empty:
        raise ValueError(f"yfinance returned empty history for {symbol}")
    return df


@ttl_cache(seconds=600)
def yf_option_chain(symbol: str, expiry: str) -> dict[str, list[dict[str, Any]]]:
    """Fetch one yfinance options chain and serialize calls/puts to records."""
    chain = yf.Ticker(symbol).option_chain(expiry)
    return {"calls": chain.calls.to_dict("records"), "puts": chain.puts.to_dict("records")}


@ttl_cache(seconds=600)
def yf_option_expiries(symbol: str) -> list[str]:
    """Return available option expiry dates for a ticker."""
    return list(yf.Ticker(symbol).options)


@ttl_cache(seconds=86400)
def yf_sector(symbol: str) -> str:
    """Return the yfinance sector for a ticker, or Unknown if unavailable."""
    try:
        info = yf.Ticker(symbol).info
        return info.get("sector", "Unknown")
    except Exception as exc:
        log.warning("sector lookup failed for %s: %s", symbol, exc)
        return "Unknown"

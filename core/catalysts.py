"""Catalyst intelligence for options candidates with graceful data degradation."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import yfinance as yf

from .config import get_sector_tailwind
from .data import ttl_cache

log = logging.getLogger("screener")


def _iso_date(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if isinstance(value, dt.datetime):
            return value.date().isoformat()
        if isinstance(value, dt.date):
            return value.isoformat()
        return dt.date.fromisoformat(str(value)[:10]).isoformat()
    except Exception:
        return None


@ttl_cache(seconds=86400)
def next_earnings(symbol: str) -> str | None:
    """Return the next known earnings date as YYYY-MM-DD, if available."""
    return catalyst_snapshot(symbol).get("next_earnings_date")


def _earnings_dates(ticker: yf.Ticker) -> dict[str, Any]:
    out: dict[str, Any] = {"next_earnings_date": None, "recent_earnings": None, "flags": []}
    try:
        earnings_dates = ticker.get_earnings_dates(limit=12)
        if earnings_dates is None or earnings_dates.empty:
            out["flags"].append("earnings_dates_missing")
            return out
        today = dt.date.today()
        future = earnings_dates[earnings_dates.index.date >= today]
        past = earnings_dates[earnings_dates.index.date < today]
        if len(future):
            out["next_earnings_date"] = future.index[0].date().isoformat()
        if len(past):
            row = past.iloc[-1]
            actual = row.get("Reported EPS")
            estimate = row.get("EPS Estimate")
            surprise = None
            if actual is not None and estimate not in (None, 0):
                try:
                    surprise = round((float(actual) - float(estimate)) / abs(float(estimate)) * 100, 2)
                except Exception:
                    surprise = None
            out["recent_earnings"] = {
                "date": past.index[-1].date().isoformat(),
                "eps_actual": None if actual is None else float(actual),
                "eps_estimate": None if estimate is None else float(estimate),
                "eps_surprise_pct": surprise,
            }
    except Exception as exc:
        out["flags"].append(f"earnings_lookup_failed:{type(exc).__name__}")
    return out


def _dividend_info(ticker: yf.Ticker) -> dict[str, Any]:
    try:
        cal = getattr(ticker, "calendar", None)
        if isinstance(cal, dict):
            ex_date = _iso_date(cal.get("Ex-Dividend Date") or cal.get("Ex-Dividend_Date"))
            if ex_date:
                return {"ex_dividend_date": ex_date, "dividend_flags": []}
        info = getattr(ticker, "info", {}) or {}
        ex_raw = info.get("exDividendDate")
        if ex_raw:
            return {"ex_dividend_date": dt.datetime.fromtimestamp(int(ex_raw), dt.UTC).date().isoformat(), "dividend_flags": []}
        return {"ex_dividend_date": None, "dividend_flags": ["dividend_date_missing"]}
    except Exception as exc:
        return {"ex_dividend_date": None, "dividend_flags": [f"dividend_lookup_failed:{type(exc).__name__}"]}


def _news(ticker: yf.Ticker, limit: int = 5) -> tuple[list[dict[str, Any]], list[str]]:
    flags: list[str] = []
    headlines: list[dict[str, Any]] = []
    try:
        for item in (getattr(ticker, "news", None) or [])[:limit]:
            content = item.get("content", item) if isinstance(item, dict) else {}
            title = content.get("title") or item.get("title") if isinstance(item, dict) else None
            pub = content.get("pubDate") or content.get("providerPublishTime") or item.get("providerPublishTime") if isinstance(item, dict) else None
            publisher = (content.get("provider") or {}).get("displayName") if isinstance(content.get("provider"), dict) else item.get("publisher") if isinstance(item, dict) else None
            if title:
                headlines.append({"title": title, "publisher": publisher, "published": str(pub) if pub else None})
    except Exception as exc:
        flags.append(f"news_lookup_failed:{type(exc).__name__}")
    if not headlines and not flags:
        flags.append("news_missing")
    return headlines, flags


def _analyst_actions(ticker: yf.Ticker, limit: int = 5) -> tuple[list[dict[str, Any]], list[str]]:
    flags: list[str] = []
    actions: list[dict[str, Any]] = []
    try:
        upgrades = getattr(ticker, "upgrades_downgrades", None)
        if upgrades is not None and not upgrades.empty:
            df = upgrades.tail(limit)
            for idx, row in df.iterrows():
                actions.append({
                    "date": _iso_date(idx),
                    "firm": row.get("Firm"),
                    "action": row.get("Action"),
                    "from_grade": row.get("FromGrade"),
                    "to_grade": row.get("ToGrade"),
                })
    except Exception as exc:
        flags.append(f"analyst_lookup_failed:{type(exc).__name__}")
    if not actions and not flags:
        flags.append("analyst_actions_missing")
    return actions, flags


def summarize_catalyst(snapshot: dict[str, Any]) -> str:
    """Create a concise catalyst decision signal from structured events."""
    pieces: list[str] = []
    days = snapshot.get("earnings_in_days")
    if days is not None:
        pieces.append(f"earnings in {days} days" if days >= 0 else f"last earnings {abs(days)} days ago")
    recent = snapshot.get("recent_earnings") or {}
    surprise = recent.get("eps_surprise_pct")
    if surprise is not None:
        pieces.append(f"recent EPS surprise {surprise:+.1f}%")
    if snapshot.get("ex_dividend_date"):
        pieces.append(f"ex-dividend {snapshot['ex_dividend_date']}")
    if snapshot.get("analyst_actions"):
        latest = snapshot["analyst_actions"][-1]
        pieces.append(f"latest analyst action {latest.get('action') or 'noted'} by {latest.get('firm') or 'unknown firm'}")
    if snapshot.get("headlines"):
        pieces.append(f"{len(snapshot['headlines'])} recent headline(s)")
    if snapshot.get("sector_tailwind", {}).get("active"):
        pieces.append("configured sector tailwind active")
    return "; ".join(pieces) if pieces else "No high-confidence catalyst found from available public data."


@ttl_cache(seconds=3600)
def catalyst_snapshot(symbol: str, dte_max: int = 60, sector: str | None = None) -> dict[str, Any]:
    """Return earnings, dividend, news, analyst and event-risk context without failing hard."""
    sym = symbol.upper().strip()
    ticker = yf.Ticker(sym)
    flags: list[str] = []
    e = _earnings_dates(ticker)
    flags.extend(e.pop("flags", []))
    div = _dividend_info(ticker)
    flags.extend(div.pop("dividend_flags", []))
    headlines, news_flags = _news(ticker)
    actions, analyst_flags = _analyst_actions(ticker)
    flags.extend(news_flags + analyst_flags)

    earnings_date = e.get("next_earnings_date")
    earnings_in_days = None
    earnings_in_window = False
    if earnings_date:
        earnings_in_days = (dt.date.fromisoformat(earnings_date) - dt.date.today()).days
        earnings_in_window = 0 <= earnings_in_days <= dte_max
    sector_tailwind = get_sector_tailwind(sector)
    event_notes = []
    if earnings_in_window:
        event_notes.append("Binary earnings risk inside requested option window; consider defined-risk structures.")
    if div.get("ex_dividend_date"):
        event_notes.append("Dividend timing can affect assignment/exercise economics around ex-date.")
    if not headlines:
        event_notes.append("Headline coverage unavailable or sparse; catalyst read is partial.")

    snapshot = {
        "symbol": sym,
        **e,
        "earnings_in_days": earnings_in_days,
        "earnings_catalyst": earnings_in_window,
        **div,
        "headlines": headlines,
        "analyst_actions": actions,
        "sector_tailwind": sector_tailwind,
        "event_risk_notes": event_notes,
        "catalyst": earnings_in_window or bool(sector_tailwind.get("active")) or bool(actions),
        "data_quality_flags": sorted(set(flags)),
    }
    snapshot["catalyst_summary"] = summarize_catalyst(snapshot)
    return snapshot


def has_catalyst(symbol: str, dte_max: int, sector: str | None = None) -> dict:
    """Backward-compatible catalyst signal used by legacy scoring."""
    snap = catalyst_snapshot(symbol, dte_max, sector)
    return {**snap, "earnings_date": snap.get("next_earnings_date")}

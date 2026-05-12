"""Config/data layer for editable watchlists and sector tailwind flags."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = Path(os.getenv("SCREENER_CONFIG_DIR", ROOT / "config"))
UNIVERSE_FILE = Path(os.getenv("SCREENER_UNIVERSE_FILE", CONFIG_DIR / "universe.json"))
SECTOR_TAILWINDS_FILE = Path(os.getenv("SCREENER_SECTOR_TAILWINDS_FILE", CONFIG_DIR / "sector_tailwinds.json"))


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_universe_config() -> dict[str, Any]:
    """Load the editable universe/watchlist config file."""
    return _load_json(UNIVERSE_FILE)


def load_sector_tailwinds() -> dict[str, Any]:
    """Load the editable sector-tailwind config file."""
    return _load_json(SECTOR_TAILWINDS_FILE)


def normalize_tickers(tickers: list[str] | None) -> list[str]:
    """Normalize, uppercase, de-dupe, and preserve input ticker order."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in tickers or []:
        sym = raw.strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def get_watchlist(name: str | None = None, include_personal: bool = True) -> list[str]:
    """Return a configured watchlist by name, optionally merged with personal tickers."""
    cfg = load_universe_config()
    watchlists = cfg.get("watchlists", {})
    selected = name or cfg.get("default", "starter_100")
    if selected not in watchlists:
        available = ", ".join(sorted(watchlists))
        raise ValueError(f"unknown watchlist '{selected}'. Available watchlists: {available}")

    tickers = normalize_tickers(watchlists.get(selected, []))
    if include_personal and selected != "personal":
        tickers = normalize_tickers([*tickers, *watchlists.get("personal", [])])
    return tickers


def list_watchlists() -> dict[str, Any]:
    """Return watchlist names, counts, and the configured default watchlist."""
    cfg = load_universe_config()
    watchlists = cfg.get("watchlists", {})
    return {
        "default": cfg.get("default", "starter_100"),
        "watchlists": {name: {"count": len(normalize_tickers(tickers))} for name, tickers in watchlists.items()},
    }


def resolve_tickers(tickers: list[str] | None = None, watchlist: str | None = None) -> list[str]:
    """Use explicit tickers when provided; otherwise fall back to the configured watchlist."""
    normalized = normalize_tickers(tickers)
    return normalized if normalized else get_watchlist(watchlist)


def get_sector_tailwind(sector: str | None) -> dict[str, Any]:
    """Return the configured tailwind metadata for one sector."""
    sector_name = sector or "Unknown"
    tailwinds = load_sector_tailwinds()
    default = {"active": False, "score_bonus": 0, "note": "No sector-tailwind config found."}
    return {"sector": sector_name, **tailwinds.get("sectors", {}).get(sector_name, default)}

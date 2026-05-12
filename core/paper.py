"""Paper-trade idea formatting for scored call candidates."""

from __future__ import annotations


def _midpoint(contract: dict) -> float | None:
    bid = contract.get("bid")
    ask = contract.get("ask")
    if bid is None or ask is None:
        return None
    mid = (float(bid or 0) + float(ask or 0)) / 2
    return round(mid, 2) if mid > 0 else None


def make_paper_trade_idea(contract: dict, contracts: int = 1) -> dict:
    """
    Convert a scored candidate into a paper long-call trade plan.

    This is intentionally mechanical and not financial advice: it gives a
    consistent paper-trading template that can be reviewed and modified before
    any real order is considered.
    """
    entry = _midpoint(contract)
    multiplier = 100
    max_debit = round(entry * multiplier * contracts, 2) if entry else None
    stop = round(entry * 0.50, 2) if entry else None
    target = round(entry * 2.00, 2) if entry else None
    technical = contract.get("technical", {})
    catalyst = contract.get("catalyst", {})
    ivr = contract.get("ivr", {})

    return {
        "symbol": contract.get("symbol"),
        "contract_symbol": contract.get("contractSymbol"),
        "action": "PAPER_BUY_TO_OPEN_CALL",
        "contracts": contracts,
        "expiry": contract.get("expiry"),
        "strike": contract.get("strike"),
        "entry_limit_mid": entry,
        "estimated_max_debit": max_debit,
        "paper_stop_option_price": stop,
        "paper_target_option_price": target,
        "risk_note": "Template assumes a 50% option-price stop and 100% option-price target for paper tracking only.",
        "score": contract.get("score"),
        "subscores": contract.get("subscores"),
        "setup_snapshot": {
            "underlying_price": technical.get("price"),
            "sector": technical.get("sector"),
            "delta": contract.get("delta"),
            "dte": contract.get("dte"),
            "spread_pct": contract.get("spread_pct"),
            "open_interest": contract.get("openInterest"),
            "ivr_method": ivr.get("method"),
            "ivr": ivr.get("ivr"),
            "proxy_ivr": ivr.get("fallback", {}).get("proxy_ivr"),
            "earnings_date": catalyst.get("earnings_date"),
            "sector_tailwind": catalyst.get("sector_tailwind"),
        },
    }


def make_paper_trade_ideas(scored_candidates: list[dict], max_ideas: int = 5, contracts: int = 1) -> list[dict]:
    """Return paper-trade templates for the top scored candidates."""
    return [make_paper_trade_idea(c, contracts=contracts) for c in scored_candidates[:max_ideas]]

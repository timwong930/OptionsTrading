"""End-to-end options trade decision engine."""

from __future__ import annotations

import datetime as dt
from statistics import mean
from typing import Any

from .catalysts import catalyst_snapshot
from .chains import get_option_chain
from .data import yf_history, yf_sector
from .ivrank import iv_rank, log_atm_iv
from .scoring import score_contract
from .screener import add_indicators

MULTIPLIER = 100
RISK_BUDGET_CAP_PCT = 0.05
MIN_LIQUID_CONTRACTS = 4


def _mid(c: dict) -> float | None:
    return c.get("mid") or ((c.get("bid", 0) + c.get("ask", 0)) / 2 if c.get("bid") or c.get("ask") else None)


def _nearest(options: list[dict], strike: float, option_type: str | None = None) -> dict | None:
    pool = [o for o in options if option_type is None or o.get("option_type") == option_type]
    return min(pool, key=lambda o: abs(o["strike"] - strike), default=None)


def _technical_snapshot(symbol: str) -> dict[str, Any]:
    df = add_indicators(yf_history(symbol, period="1y"))
    spy = None
    try:
        spy = add_indicators(yf_history("SPY", period="6mo"))
    except Exception:
        spy = None
    last = df.iloc[-1]
    closes = df["Close"].tail(20)
    support = float(closes.min())
    resistance = float(closes.max())
    rs = None
    try:
        if spy is not None and len(df) > 63 and len(spy) > 63:
            rs = float(df["Close"].iloc[-1] / df["Close"].iloc[-63] - 1 - (spy["Close"].iloc[-1] / spy["Close"].iloc[-63] - 1))
    except Exception:
        rs = None
    bullish = bool(last["Close"] > last["SMA50"] > last["SMA200"] and 40 <= last["RSI14"] <= 70 and (rs is None or rs > 0))
    bearish = bool(last["Close"] < last["SMA50"] < last["SMA200"] and last["RSI14"] < 60)
    bias = "bullish" if bullish or last["Close"] > last["SMA50"] else "bearish" if bearish or last["Close"] < last["SMA50"] else "neutral"
    return {
        "price": float(last["Close"]),
        "sma50": float(last["SMA50"]),
        "sma200": float(last["SMA200"]),
        "rsi14": float(last["RSI14"]),
        "support_20d": support,
        "resistance_20d": resistance,
        "avg_close_20d": float(mean(closes)),
        "rel_strength_vs_spy_63d": rs,
        "bullish": bullish,
        "bearish": bearish,
        "bias": bias,
    }


def _liquid(options: list[dict]) -> list[dict]:
    return [o for o in options if o.get("liquid") and not o.get("stale") and o.get("mid")]


def _quality_counts(options: list[dict]) -> dict:
    flags: dict[str, int] = {}
    for opt in options:
        for flag in opt.get("data_quality_flags", []):
            flags[flag] = flags.get(flag, 0) + 1
    spreads = [o.get("spread_pct") for o in options if o.get("spread_pct") is not None]
    return {
        "total_contracts": len(options),
        "liquid_contracts": len(_liquid(options)),
        "stale_contracts": sum(1 for o in options if o.get("stale")),
        "avg_spread_pct": round(mean(spreads), 2) if spreads else None,
        "flag_counts": dict(sorted(flags.items())),
    }


def _best_expiry(options: list[dict], horizon_days: int) -> str | None:
    target = max(30, min(180, int(horizon_days * 1.8)))
    expiries = sorted({o["expiry"] for o in options})
    return min(expiries, key=lambda e: abs((dt.date.fromisoformat(e) - dt.date.today()).days - target), default=None)


def _risk_budget(budget: float, portfolio_value: float) -> tuple[float, dict]:
    requested = max(0.0, float(budget or 0.0))
    cap = max(0.0, float(portfolio_value or 0.0) * RISK_BUDGET_CAP_PCT)
    effective = min(requested, cap) if cap else requested
    return effective, {
        "requested_budget": round(requested, 2),
        "risk_budget": round(effective, 2),
        "portfolio_cap_pct": RISK_BUDGET_CAP_PCT,
        "portfolio_cap_amount": round(cap, 2),
        "capped": bool(cap and requested > cap),
        "cap_explanation": f"Risk budget capped at {RISK_BUDGET_CAP_PCT:.0%} of portfolio value for conservative sizing." if cap and requested > cap else None,
    }


def _fetch_chain_with_salvage(sym: str, horizon_days: int) -> tuple[list[dict], list[dict], list[str]]:
    windows = [
        (max(14, horizon_days // 2), max(45, horizon_days * 3), "primary_horizon_window"),
        (30, 60, "alternate_shorter_dte"),
        (60, 120, "scorer_default_dte"),
        (120, 180, "alternate_longer_dte"),
    ]
    merged: dict[tuple, dict] = {}
    attempts: list[dict] = []
    used: list[str] = []
    for min_dte, max_dte, label in windows:
        try:
            chain = get_option_chain(sym, min_dte=min_dte, max_dte=max_dte, option_types=("call", "put"))
        except Exception as exc:
            attempts.append({"window": label, "min_dte": min_dte, "max_dte": max_dte, "error": str(exc)})
            continue
        attempts.append({"window": label, "min_dte": min_dte, "max_dte": max_dte, "contracts": len(chain), "liquid_contracts": len(_liquid(chain))})
        if chain:
            used.append(label)
        for opt in chain:
            key = (opt.get("contractSymbol"), opt.get("option_type"), opt.get("expiry"), opt.get("strike"))
            merged[key] = opt
    return list(merged.values()), attempts, used


def _add_sizing(rec: dict, budget: float) -> dict:
    risk = rec.get("max_loss") or 0
    contracts = int(budget // risk) if risk else 0
    rec["suggested_contract_count"] = max(1, min(contracts, 10)) if contracts else 0
    rec["budget_fit"] = contracts >= 1
    rec["budget_shortfall"] = round(max(0.0, risk - budget), 2) if risk else 0.0
    return rec


def _make_debit_spread(buy: dict, sell: dict, bias: str, budget: float) -> dict | None:
    debit = round((_mid(buy) or 0) - (_mid(sell) or 0), 2)
    width = abs(sell["strike"] - buy["strike"])
    if debit <= 0 or width <= 0 or debit >= width:
        return None
    opt_type = "call" if bias == "bullish" else "put"
    max_loss = debit * MULTIPLIER
    max_profit = (width - debit) * MULTIPLIER
    breakeven = buy["strike"] + debit if bias == "bullish" else buy["strike"] - debit
    rec = {
        "strategy": "call_debit_spread" if bias == "bullish" else "put_debit_spread",
        "category": "defined_risk_spread",
        "expiry": buy["expiry"],
        "legs": [
            {"side": "buy", "type": opt_type, "strike": buy["strike"], "contract": buy.get("contractSymbol"), "mid": _mid(buy)},
            {"side": "sell", "type": opt_type, "strike": sell["strike"], "contract": sell.get("contractSymbol"), "mid": _mid(sell)},
        ],
        "estimated_debit": debit,
        "max_loss": round(max_loss, 2),
        "max_profit": round(max_profit, 2),
        "breakeven": round(breakeven, 2),
        "liquidity_flags": sorted(set(sum((leg.get("data_quality_flags", []) for leg in [buy, sell]), []))),
        "source_contracts": [buy, sell],
    }
    return _add_sizing(rec, budget)


def _debit_spreads(options: list[dict], spot: float, bias: str, budget: float, horizon_days: int = 45) -> list[dict]:
    opt_type = "call" if bias == "bullish" else "put"
    side_options = _liquid([o for o in options if o["option_type"] == opt_type])
    if not side_options:
        return []
    expiries = sorted({o["expiry"] for o in side_options}, key=lambda e: abs((dt.date.fromisoformat(e) - dt.date.today()).days - max(30, horizon_days * 2)))[:4]
    spreads: list[dict] = []
    for expiry in expiries:
        expiry_options = sorted([o for o in side_options if o["expiry"] == expiry], key=lambda o: o["strike"])
        buys = [o for o in expiry_options if 0.30 <= abs(o.get("delta") or 0) <= 0.75]
        if not buys:
            buys = sorted(expiry_options, key=lambda o: abs(o["strike"] - spot))[:4]
        for buy in sorted(buys, key=lambda o: (abs(abs(o.get("delta") or 0) - 0.55), abs(o["strike"] - spot)))[:6]:
            if bias == "bullish":
                sells = [o for o in expiry_options if o["strike"] > buy["strike"]]
            else:
                sells = [o for o in expiry_options if o["strike"] < buy["strike"]]
            for sell in sorted(sells, key=lambda o: abs(abs(o["strike"] - buy["strike"]) - max(2.5, spot * 0.04)))[:4]:
                spread = _make_debit_spread(buy, sell, bias, budget)
                if spread:
                    spreads.append(spread)
    return spreads


def _debit_spread(options: list[dict], spot: float, bias: str, budget: float, horizon_days: int = 45) -> dict | None:
    candidates = _debit_spreads(options, spot, bias, budget, horizon_days)
    return _rank_structures(candidates)[0] if candidates else None


def _naked_or_csp(options: list[dict], spot: float, bias: str, budget: float) -> dict | None:
    if bias == "bullish":
        calls = _liquid([o for o in options if o["option_type"] == "call" and 0.30 <= (o.get("delta") or 0) <= 0.75])
        contract = min(calls, key=lambda o: (abs((o.get("delta") or 0) - 0.50), _mid(o) or 999), default=None)
        if not contract:
            return None
        debit = round(_mid(contract) or 0, 2)
        risk = debit * MULTIPLIER
        return _add_sizing({
            "strategy": "long_call",
            "category": "bullish_contract",
            "expiry": contract["expiry"],
            "legs": [{"side": "buy", "type": "call", "strike": contract["strike"], "contract": contract.get("contractSymbol"), "mid": debit}],
            "estimated_debit": debit,
            "max_loss": round(risk, 2),
            "max_profit": None,
            "breakeven": round(contract["strike"] + debit, 2),
            "liquidity_flags": contract.get("data_quality_flags", []),
            "source_contracts": [contract],
        }, budget)
    puts = _liquid([o for o in options if o["option_type"] == "put" and abs((o.get("delta") or 0)) <= 0.35 and o["strike"] < spot])
    contract = max(puts, key=lambda o: o["strike"], default=None)
    if not contract:
        return None
    collateral = contract["strike"] * MULTIPLIER
    credit = round(_mid(contract) or 0, 2)
    return _add_sizing({
        "strategy": "cash_secured_put",
        "category": "bullish_income",
        "expiry": contract["expiry"],
        "legs": [{"side": "sell", "type": "put", "strike": contract["strike"], "contract": contract.get("contractSymbol"), "mid": credit}],
        "estimated_credit": credit,
        "max_loss": round(collateral - credit * MULTIPLIER, 2),
        "max_profit": round(credit * MULTIPLIER, 2),
        "breakeven": round(contract["strike"] - credit, 2),
        "liquidity_flags": contract.get("data_quality_flags", []),
        "source_contracts": [contract],
    }, budget)


def _credit_spreads(options: list[dict], spot: float, bias: str, budget: float) -> list[dict]:
    opt_type = "put" if bias == "bullish" else "call"
    candidates = _liquid([o for o in options if o["option_type"] == opt_type])
    expiries = sorted({o["expiry"] for o in candidates}, key=lambda e: abs((dt.date.fromisoformat(e) - dt.date.today()).days - 45))[:3]
    spreads: list[dict] = []
    for expiry in expiries:
        expiry_options = sorted([o for o in candidates if o["expiry"] == expiry], key=lambda o: o["strike"])
        shorts = [o for o in expiry_options if 0.15 <= abs(o.get("delta") or 0) <= 0.40]
        if not shorts:
            short_target = spot * (0.95 if bias == "bullish" else 1.05)
            short = _nearest(expiry_options, short_target, opt_type)
            shorts = [short] if short else []
        for short in shorts[:5]:
            longs = [o for o in expiry_options if (o["strike"] < short["strike"] if opt_type == "put" else o["strike"] > short["strike"])]
            for long in sorted(longs, key=lambda o: abs(abs(o["strike"] - short["strike"]) - max(2.5, spot * 0.04)))[:3]:
                credit = round((_mid(short) or 0) - (_mid(long) or 0), 2)
                width = abs(long["strike"] - short["strike"])
                if credit <= 0 or credit >= width:
                    continue
                max_loss = (width - credit) * MULTIPLIER
                rec = {
                    "strategy": "credit_spread",
                    "category": "defined_risk_spread",
                    "expiry": expiry,
                    "legs": [
                        {"side": "sell", "type": opt_type, "strike": short["strike"], "contract": short.get("contractSymbol"), "mid": _mid(short)},
                        {"side": "buy", "type": opt_type, "strike": long["strike"], "contract": long.get("contractSymbol"), "mid": _mid(long)},
                    ],
                    "estimated_credit": credit,
                    "max_loss": round(max_loss, 2),
                    "max_profit": round(credit * MULTIPLIER, 2),
                    "breakeven": round(short["strike"] - credit if opt_type == "put" else short["strike"] + credit, 2),
                    "liquidity_flags": sorted(set(sum((leg.get("data_quality_flags", []) for leg in [short, long]), []))),
                    "source_contracts": [short, long],
                }
                spreads.append(_add_sizing(rec, budget))
    return spreads


def _credit_spread(options: list[dict], spot: float, bias: str, budget: float) -> dict | None:
    candidates = _credit_spreads(options, spot, bias, budget)
    return _rank_structures(candidates)[0] if candidates else None


def _contract_conviction(contract: dict) -> float:
    spread = contract.get("spread_pct") if contract.get("spread_pct") is not None else 99
    oi = contract.get("openInterest") or 0
    delta = abs(contract.get("delta") or 0)
    return (40 if contract.get("liquid") else 0) + min(25, oi / 20) + max(0, 20 - spread) + max(0, 15 - abs(delta - 0.55) * 50)


def score_option_candidates(chain: list[dict], tech: dict, catalyst: dict, iv_info: dict, bias: str = "bullish") -> list[dict]:
    """Score the same liquid contract universe used by the planner."""
    option_type = "call" if bias == "bullish" else "put"
    scored: list[dict] = []
    tech_for_score = {**tech, "bullish": bias == "bullish" or tech.get("bullish", False)}
    for c in chain:
        if not (c.get("liquid") and not c.get("stale") and c.get("mid")):
            continue
        if c.get("option_type") != option_type or c.get("delta") is None:
            continue
        if not (0.30 <= abs(c["delta"]) <= 0.90):
            continue
        c_for_score = {**c, "delta": abs(c.get("delta") or 0)}
        score = score_contract(c_for_score, tech_for_score, catalyst, iv_info)
        scored.append({**c, **score, "technical": tech, "catalyst": catalyst, "ivr": iv_info})
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored


def _rank_structures(candidates: list[dict]) -> list[dict]:
    def key(c: dict) -> tuple:
        risk = c.get("max_loss") or 10**9
        reward = c.get("max_profit") or risk
        rr = reward / risk if risk else 0
        return (1 if c.get("budget_fit") else 0, 1 if "spread" in c.get("strategy", "") else 0, -len(c.get("liquidity_flags", [])), rr, -risk)
    return sorted(candidates, key=key, reverse=True)


def _recommendation_set(structures: list[dict], scored_contracts: list[dict]) -> dict:
    ranked_structures = _rank_structures(structures)
    best_spread = next((s for s in ranked_structures if "spread" in s.get("strategy", "")), None)
    cheapest = min((s for s in structures if s.get("budget_fit")), key=lambda s: s.get("max_loss") or 10**9, default=None)
    highest_contract = scored_contracts[0] if scored_contracts else None
    best_bullish = next((s for s in ranked_structures if s.get("strategy") in {"long_call", "call_debit_spread", "credit_spread"}), None)
    return {
        "best_bullish_setup": _strip_sources(best_bullish),
        "best_defined_risk_spread": _strip_sources(best_spread),
        "cheapest_viable_alternative": _strip_sources(cheapest),
        "highest_conviction_contract": _contract_summary(highest_contract),
    }


def _strip_sources(rec: dict | None) -> dict | None:
    if not rec:
        return None
    return {k: v for k, v in rec.items() if k != "source_contracts"}


def _contract_summary(contract: dict | None) -> dict | None:
    if not contract:
        return None
    return {k: contract.get(k) for k in ["symbol", "contractSymbol", "option_type", "expiry", "strike", "mid", "delta", "iv", "dte", "score", "subscores", "spread_pct", "openInterest", "volume", "data_quality_flags"]}


def _rejection_reasons(tech: dict, chain: list[dict], iv_info: dict, structures: list[dict], risk_budget: float, catalyst: dict) -> list[dict]:
    reasons: list[dict] = []
    if tech.get("bias") == "neutral":
        reasons.append({"category": "stock_setup", "reason": "neutral technical bias"})
    quality = _quality_counts(chain)
    if quality["liquid_contracts"] < MIN_LIQUID_CONTRACTS:
        reasons.append({"category": "liquidity", "reason": "insufficient liquid contracts", "liquid_contracts": quality["liquid_contracts"]})
    if iv_info.get("method") in {"missing", "insufficient_history"} or iv_info.get("history_quality") == "insufficient_history":
        reasons.append({"category": "iv_history", "reason": "single-name IV history is insufficient; proxy IV used where available"})
    if structures and not any(s.get("budget_fit") for s in structures):
        cheapest_risk = min((s.get("max_loss") or 10**9) for s in structures)
        reasons.append({"category": "budget", "reason": "liquid structures found but none fit effective risk budget", "risk_budget": round(risk_budget, 2), "cheapest_max_loss": round(cheapest_risk, 2)})
    if catalyst.get("event_risk_notes"):
        reasons.append({"category": "catalyst_risk", "reason": "upcoming or recent event risk present", "notes": catalyst.get("event_risk_notes")})
    flags = quality.get("flag_counts", {})
    if flags.get("wide_spread"):
        reasons.append({"category": "spread", "reason": "some contracts have wide quoted spreads", "affected_contracts": flags["wide_spread"]})
    if flags.get("stale_contract"):
        reasons.append({"category": "stale_quote", "reason": "some contracts have stale last-trade dates", "affected_contracts": flags["stale_contract"]})
    return reasons


def _tradeability(tech: dict, chain: list[dict], iv_info: dict, structures: list[dict], risk_budget: float, catalyst: dict) -> dict:
    reasons = _rejection_reasons(tech, chain, iv_info, structures, risk_budget, catalyst)
    fit = any(s.get("budget_fit") for s in structures)
    quality = _quality_counts(chain)
    tradable = fit and quality["liquid_contracts"] >= MIN_LIQUID_CONTRACTS and tech.get("bias") != "neutral"
    blocking = [r for r in reasons if r["category"] in {"stock_setup", "liquidity", "budget"}]
    return {
        "tradable": bool(tradable),
        "blocking_categories": [r["category"] for r in blocking],
        "rejection_reasons": reasons,
        "salvage_attempted": True,
        "salvage_successful": bool(fit),
    }


def _confidence(tech: dict, quality: dict, rec: dict | None, iv_info: dict, catalyst: dict, data_flags: list[str]) -> tuple[float, str]:
    score = 0.35
    score += 0.15 if tech.get("bullish") or tech.get("bearish") else 0.0
    score += 0.15 if quality["liquid_contracts"] >= 10 else 0.05 if quality["liquid_contracts"] >= MIN_LIQUID_CONTRACTS else -0.15
    score += 0.10 if rec and rec.get("budget_fit") else -0.10
    score += 0.08 if rec and "spread" in rec.get("strategy", "") else 0.0
    score += 0.07 if catalyst.get("catalyst") else 0.0
    if iv_info.get("method") != "self_collected":
        score -= 0.07
    if data_flags:
        score -= min(0.10, len(set(data_flags)) * 0.02)
    score = round(max(0.05, min(0.95, score)), 2)
    tier = "high" if score >= 0.75 else "medium" if score >= 0.55 else "low" if score >= 0.35 else "avoid"
    return score, tier


def _trade_plan(rec: dict, tech: dict, catalyst: dict, horizon_days: int) -> dict:
    debit = rec.get("estimated_debit")
    credit = rec.get("estimated_credit")
    price = debit if debit is not None else credit
    support = tech["support_20d"]
    resistance = tech["resistance_20d"]
    bias = "bullish" if "call" in rec["strategy"] or (rec["strategy"] == "credit_spread" and rec["legs"][0]["type"] == "put") else "bearish"
    invalidation = f"Underlying closes below 20-day support near {support:.2f}." if bias == "bullish" else f"Underlying closes above 20-day resistance near {resistance:.2f}."
    return {
        "entry_zone": f"Work a limit order near {price * 0.97:.2f}-{price * 1.03:.2f}; do not chase beyond midpoint +3%.",
        "ideal_limit_price": round(price, 2),
        "profit_target_1": "Take partial/close at 50% of max profit for spreads or +50% option premium for long options.",
        "profit_target_2": "Exit remaining at 70-80% of max profit; avoid holding short spreads through expiration week.",
        "stop_loss": "Exit if premium loses ~50% for debit trades or if spread value reaches 2x credit for credit trades.",
        "invalidation": invalidation,
        "time_stop": f"Reassess/exit after {max(7, int(horizon_days * 0.6))} days if thesis has not started working.",
        "hold_to_expiry_guidance": "Do not hold illiquid or short-option spreads to expiration; close before final week unless intentionally accepting assignment/exercise risk.",
        "event_notes": catalyst.get("event_risk_notes", []),
    }


def analyze_trade(symbol: str, bias: str = "auto", budget: float = 500.0, portfolio_value: float = 5000.0, horizon_days: int = 45) -> dict:
    """Analyze a ticker end-to-end and return budget-aware ranked trade plans."""
    sym = symbol.upper().strip()
    data_flags: list[str] = []
    tech = _technical_snapshot(sym)
    effective_bias = tech["bias"] if bias == "auto" else bias
    sector = yf_sector(sym)
    catalyst = catalyst_snapshot(sym, dte_max=max(30, horizon_days * 2), sector=sector)
    data_flags.extend(catalyst.get("data_quality_flags", []))
    risk_budget, budget_info = _risk_budget(budget, portfolio_value)

    chain, chain_attempts, windows_used = _fetch_chain_with_salvage(sym, horizon_days)
    quality = _quality_counts(chain)
    if not chain:
        return {
            "symbol": sym,
            "recommended_strategy": "no_trade",
            "reason": "no option chain returned in primary or alternate DTE windows",
            "stock_setup": tech,
            "options_quality": quality,
            "tradeability": {"tradable": False, "blocking_categories": ["missing_option_chain"], "rejection_reasons": [{"category": "missing_option_chain", "reason": "no contracts returned"}], "salvage_attempted": True, "salvage_successful": False},
            "risk_budget": round(risk_budget, 2),
            "budget": budget_info,
            "chain_attempts": chain_attempts,
            "data_quality_flags": sorted(set(data_flags + ["missing_option_chain"])),
        }

    atm = min((c for c in chain if c.get("iv")), key=lambda c: abs(c["strike"] - tech["price"]), default=None)
    iv = atm.get("iv") if atm else None
    iv_info = {"method": "missing", "iv_rank": None, "iv_percentile": None, "history_quality": "missing", "data_quality_flags": ["missing_atm_iv"], "fallback": {"method": "unavailable", "proxy_ivr": None, "note": "No ATM IV available; IV-dependent structures downgraded."}}
    if iv:
        log_atm_iv(sym, iv)
        iv_info = iv_rank(sym, iv)
    data_flags.extend(iv_info.get("data_quality_flags", []))

    ivr = iv_info.get("iv_rank") if iv_info.get("iv_rank") is not None else iv_info.get("fallback", {}).get("proxy_ivr")
    structures: list[dict] = []
    structures.extend(_debit_spreads(chain, tech["price"], effective_bias, risk_budget, horizon_days))
    credit_candidates = _credit_spreads(chain, tech["price"], effective_bias, risk_budget) if ivr is not None and ivr >= 50 else []
    structures.extend(credit_candidates)
    naked = _naked_or_csp(chain, tech["price"], effective_bias, risk_budget)
    if naked:
        structures.append(naked)

    scored_contracts = score_option_candidates(chain, tech, catalyst, iv_info, effective_bias)
    structures = _rank_structures(structures)
    target_dte = max(30, min(180, int(horizon_days * 1.8)))

    def horizon_key(c: dict) -> tuple:
        dte = (dt.date.fromisoformat(c["expiry"]) - dt.date.today()).days if c.get("expiry") else 999
        return (abs(dte - target_dte), 0 if "spread" in c.get("strategy", "") else 1, c.get("max_loss") or 10**9)

    fit_structures = sorted([c for c in structures if c.get("budget_fit")], key=horizon_key)
    rec = next((c for c in fit_structures if "spread" in c.get("strategy", "") or c.get("max_loss", 10**9) <= risk_budget * 0.75), None)
    rec = rec or (fit_structures[0] if fit_structures else None)
    tradeability = _tradeability({**tech, "bias": effective_bias}, chain, iv_info, structures, risk_budget, catalyst)
    confidence, tier = _confidence(tech, quality, rec, iv_info, catalyst, data_flags)
    recommendations = _recommendation_set(structures, scored_contracts)

    if rec is None or effective_bias == "neutral" or quality["liquid_contracts"] < MIN_LIQUID_CONTRACTS:
        reasons = tradeability["rejection_reasons"]
        primary = reasons[0]["reason"] if reasons else "no budget-fit tradable structure found after alternate DTE/strike/structure salvage"
        return {
            "symbol": sym,
            "bias": effective_bias,
            "tradable": False,
            "recommended_strategy": "no_trade",
            "reason": primary,
            "stock_setup": tech,
            "options_quality": quality,
            "tradeability": tradeability,
            "recommendations": recommendations,
            "top_contracts": [_contract_summary(c) for c in scored_contracts[:5]],
            "risk_budget": round(risk_budget, 2),
            "budget": budget_info,
            "iv_current": iv,
            "iv_rank": iv_info.get("iv_rank"),
            "iv_percentile": iv_info.get("iv_percentile"),
            "iv_method": iv_info.get("method"),
            "iv_history_quality": iv_info.get("history_quality"),
            "iv_fallback": iv_info.get("fallback"),
            "iv_proxy_label": "PROXY IV ESTIMATE - not single-name IV rank" if iv_info.get("fallback") else None,
            "confidence": confidence,
            "quality_tier": tier,
            "chain_attempts": chain_attempts,
            "dte_windows_used": windows_used,
            "data_quality_flags": sorted(set(data_flags + sum((s.get("liquidity_flags", []) for s in structures[:5]), []))),
        }

    risk = rec.get("max_loss") or 0
    plan = _trade_plan(rec, tech, catalyst, horizon_days)
    return {
        "symbol": sym,
        "bias": effective_bias,
        "tradable": True,
        "stock_setup": tech,
        "options_quality": quality,
        "tradeability": tradeability,
        "catalyst_summary": catalyst.get("catalyst_summary"),
        "next_earnings_date": catalyst.get("next_earnings_date"),
        "recent_earnings": catalyst.get("recent_earnings"),
        "ex_dividend_date": catalyst.get("ex_dividend_date"),
        "headlines": catalyst.get("headlines", [])[:3],
        "analyst_actions": catalyst.get("analyst_actions", [])[:3],
        "iv_current": iv,
        "iv_rank": iv_info.get("iv_rank"),
        "iv_percentile": iv_info.get("iv_percentile"),
        "iv_method": iv_info.get("method"),
        "iv_history_quality": iv_info.get("history_quality"),
        "iv_fallback": iv_info.get("fallback"),
        "iv_proxy_label": "PROXY IV ESTIMATE - not single-name IV rank" if iv_info.get("fallback") else None,
        "recommended_strategy": rec["strategy"],
        "recommended_expiry": rec["expiry"],
        "recommended_legs": rec["legs"],
        "estimated_debit": rec.get("estimated_debit"),
        "estimated_credit": rec.get("estimated_credit"),
        "max_loss": rec.get("max_loss"),
        "max_profit": rec.get("max_profit"),
        "breakeven": rec.get("breakeven"),
        "budget_fit": rec.get("budget_fit"),
        "budget_shortfall": rec.get("budget_shortfall"),
        "suggested_contract_count": rec.get("suggested_contract_count"),
        "risk_budget": round(risk_budget, 2),
        "budget": budget_info,
        "risk_pct_of_portfolio": round((risk * rec.get("suggested_contract_count", 1)) / portfolio_value * 100, 2) if portfolio_value else None,
        "recommendations": recommendations,
        "ranked_structures": [_strip_sources(s) for s in structures[:5]],
        "top_contracts": [_contract_summary(c) for c in scored_contracts[:5]],
        "warnings": [w for w in ["illiquid legs" if rec.get("liquidity_flags") else None, "uses proxy/partial IV" if iv_info.get("method") != "self_collected" else None, budget_info.get("cap_explanation")] if w],
        "entry_plan": plan["entry_zone"],
        "exit_plan": f"{plan['profit_target_1']} {plan['profit_target_2']}",
        "invalidation": plan["invalidation"],
        "trade_plan": plan,
        "confidence": confidence,
        "quality_tier": tier,
        "chain_attempts": chain_attempts,
        "dte_windows_used": windows_used,
        "data_quality_flags": sorted(set(data_flags + rec.get("liquidity_flags", []))),
        "technical": tech,
    }

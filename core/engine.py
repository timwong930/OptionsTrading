"""End-to-end options trade decision engine."""

from __future__ import annotations

import datetime as dt
from statistics import mean
from typing import Any

from .catalysts import catalyst_snapshot
from .chains import get_option_chain
from .data import yf_history, yf_sector
from .ivrank import iv_rank, log_atm_iv
from .screener import add_indicators

MULTIPLIER = 100


def _mid(c: dict) -> float | None:
    return c.get("mid") or ((c.get("bid", 0) + c.get("ask", 0)) / 2 if c.get("bid") or c.get("ask") else None)


def _nearest(options: list[dict], strike: float, option_type: str | None = None) -> dict | None:
    pool = [o for o in options if option_type is None or o.get("option_type") == option_type]
    return min(pool, key=lambda o: abs(o["strike"] - strike), default=None)


def _technical_snapshot(symbol: str) -> dict[str, Any]:
    df = add_indicators(yf_history(symbol, period="1y"))
    last = df.iloc[-1]
    closes = df["Close"].tail(20)
    support = float(closes.min())
    resistance = float(closes.max())
    bias = "bullish" if last["Close"] > last["SMA50"] else "bearish" if last["Close"] < last["SMA50"] else "neutral"
    return {
        "price": float(last["Close"]),
        "sma50": float(last["SMA50"]),
        "sma200": float(last["SMA200"]),
        "rsi14": float(last["RSI14"]),
        "support_20d": support,
        "resistance_20d": resistance,
        "avg_close_20d": float(mean(closes)),
        "bias": bias,
    }


def _liquid(options: list[dict]) -> list[dict]:
    return [o for o in options if o.get("liquid") and not o.get("stale") and o.get("mid")]


def _best_expiry(options: list[dict], horizon_days: int) -> str | None:
    target = max(30, min(120, int(horizon_days * 1.8)))
    expiries = sorted({o["expiry"] for o in options})
    return min(expiries, key=lambda e: abs((dt.date.fromisoformat(e) - dt.date.today()).days - target), default=None)


def _debit_spread(options: list[dict], spot: float, bias: str, budget: float, horizon_days: int = 45) -> dict | None:
    opt_type = "call" if bias == "bullish" else "put"
    side_options = _liquid([o for o in options if o["option_type"] == opt_type])
    if not side_options:
        return None
    expiry = _best_expiry(side_options, horizon_days)
    expiry_options = [o for o in side_options if o["expiry"] == expiry]
    buy = _nearest(expiry_options, spot, opt_type)
    if not buy:
        return None
    width = max(2.5, round(spot * 0.04 / 2.5) * 2.5)
    sell_strike = buy["strike"] + width if bias == "bullish" else buy["strike"] - width
    sell = _nearest(expiry_options, sell_strike, opt_type)
    if not sell or sell["strike"] == buy["strike"]:
        return None
    debit = round((_mid(buy) or 0) - (_mid(sell) or 0), 2)
    width = abs(sell["strike"] - buy["strike"])
    if debit <= 0 or debit >= width:
        return None
    max_loss = debit * MULTIPLIER
    max_profit = (width - debit) * MULTIPLIER
    breakeven = buy["strike"] + debit if bias == "bullish" else buy["strike"] - debit
    contracts = max(0, int(budget // max_loss)) if max_loss else 0
    return {
        "strategy": "call_debit_spread" if bias == "bullish" else "put_debit_spread",
        "expiry": expiry,
        "legs": [
            {"side": "buy", "type": opt_type, "strike": buy["strike"], "contract": buy.get("contractSymbol"), "mid": _mid(buy)},
            {"side": "sell", "type": opt_type, "strike": sell["strike"], "contract": sell.get("contractSymbol"), "mid": _mid(sell)},
        ],
        "estimated_debit": debit,
        "max_loss": round(max_loss, 2),
        "max_profit": round(max_profit, 2),
        "breakeven": round(breakeven, 2),
        "suggested_contract_count": max(1, min(contracts, 10)) if contracts else 0,
        "budget_fit": contracts >= 1,
        "liquidity_flags": sorted(set(sum((leg.get("data_quality_flags", []) for leg in [buy, sell]), []))),
        "source_contracts": [buy, sell],
    }


def _naked_or_csp(options: list[dict], spot: float, bias: str, budget: float) -> dict | None:
    if bias == "bullish":
        opt_type = "call"
        contract = _nearest(_liquid([o for o in options if o["option_type"] == opt_type and 0.45 <= (o.get("delta") or 0) <= 0.75]), spot, opt_type)
        if not contract:
            return None
        debit = round(_mid(contract) or 0, 2)
        risk = debit * MULTIPLIER
        return {
            "strategy": "naked_call",
            "expiry": contract["expiry"],
            "legs": [{"side": "buy", "type": "call", "strike": contract["strike"], "contract": contract.get("contractSymbol"), "mid": debit}],
            "estimated_debit": debit,
            "max_loss": round(risk, 2),
            "max_profit": None,
            "breakeven": round(contract["strike"] + debit, 2),
            "suggested_contract_count": int(budget // risk) if risk else 0,
            "budget_fit": risk <= budget,
            "liquidity_flags": contract.get("data_quality_flags", []),
            "source_contracts": [contract],
        }
    puts = _liquid([o for o in options if o["option_type"] == "put" and abs((o.get("delta") or 0)) <= 0.35 and o["strike"] < spot])
    contract = max(puts, key=lambda o: o["strike"], default=None)
    if not contract:
        return None
    collateral = contract["strike"] * MULTIPLIER
    credit = round(_mid(contract) or 0, 2)
    return {
        "strategy": "cash_secured_put",
        "expiry": contract["expiry"],
        "legs": [{"side": "sell", "type": "put", "strike": contract["strike"], "contract": contract.get("contractSymbol"), "mid": credit}],
        "estimated_credit": credit,
        "max_loss": round(collateral - credit * MULTIPLIER, 2),
        "max_profit": round(credit * MULTIPLIER, 2),
        "breakeven": round(contract["strike"] - credit, 2),
        "suggested_contract_count": int(budget // collateral),
        "budget_fit": collateral <= budget,
        "liquidity_flags": contract.get("data_quality_flags", []),
        "source_contracts": [contract],
    }


def _credit_spread(options: list[dict], spot: float, bias: str, budget: float) -> dict | None:
    opt_type = "put" if bias == "bullish" else "call"
    candidates = _liquid([o for o in options if o["option_type"] == opt_type])
    expiry = _best_expiry(candidates, 35)
    expiry_options = [o for o in candidates if o["expiry"] == expiry]
    short_target = spot * (0.95 if bias == "bullish" else 1.05)
    short = _nearest(expiry_options, short_target, opt_type)
    if not short:
        return None
    width = max(2.5, round(spot * 0.04 / 2.5) * 2.5)
    long_strike = short["strike"] - width if opt_type == "put" else short["strike"] + width
    long = _nearest(expiry_options, long_strike, opt_type)
    if not long or long["strike"] == short["strike"]:
        return None
    credit = round((_mid(short) or 0) - (_mid(long) or 0), 2)
    width = abs(long["strike"] - short["strike"])
    if credit <= 0 or credit >= width:
        return None
    max_loss = (width - credit) * MULTIPLIER
    contracts = int(budget // max_loss) if max_loss else 0
    return {
        "strategy": "credit_spread",
        "expiry": expiry,
        "legs": [
            {"side": "sell", "type": opt_type, "strike": short["strike"], "contract": short.get("contractSymbol"), "mid": _mid(short)},
            {"side": "buy", "type": opt_type, "strike": long["strike"], "contract": long.get("contractSymbol"), "mid": _mid(long)},
        ],
        "estimated_credit": credit,
        "max_loss": round(max_loss, 2),
        "max_profit": round(credit * MULTIPLIER, 2),
        "breakeven": round(short["strike"] - credit if opt_type == "put" else short["strike"] + credit, 2),
        "suggested_contract_count": max(1, min(contracts, 10)) if contracts else 0,
        "budget_fit": contracts >= 1,
        "liquidity_flags": sorted(set(sum((leg.get("data_quality_flags", []) for leg in [short, long]), []))),
        "source_contracts": [short, long],
    }


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
        "profit_target_1": "Take partial/close at 50% of max profit for spreads or +50% option premium for naked long options.",
        "profit_target_2": "Exit remaining at 70-80% of max profit; avoid holding short spreads through expiration week.",
        "stop_loss": "Exit if premium loses ~50% for debit trades or if spread value reaches 2x credit for credit trades.",
        "invalidation": invalidation,
        "time_stop": f"Reassess/exit after {max(7, int(horizon_days * 0.6))} days if thesis has not started working.",
        "hold_to_expiry_guidance": "Do not hold illiquid or short-option spreads to expiration; close before final week unless intentionally accepting assignment/exercise risk.",
        "event_notes": catalyst.get("event_risk_notes", []),
    }


def analyze_trade(symbol: str, bias: str = "auto", budget: float = 500.0, portfolio_value: float = 5000.0, horizon_days: int = 45) -> dict:
    """Analyze a ticker end-to-end and return one actionable, budget-aware trade plan."""
    sym = symbol.upper().strip()
    data_flags: list[str] = []
    tech = _technical_snapshot(sym)
    effective_bias = tech["bias"] if bias == "auto" else bias
    if effective_bias == "neutral":
        return {"symbol": sym, "recommended_strategy": "no_trade", "reason": "neutral technical bias", "technical": tech}
    sector = yf_sector(sym)
    catalyst = catalyst_snapshot(sym, dte_max=max(30, horizon_days * 2), sector=sector)
    chain = get_option_chain(sym, min_dte=max(14, horizon_days // 2), max_dte=max(45, horizon_days * 3), option_types=("call", "put"))
    if not chain:
        return {"symbol": sym, "recommended_strategy": "no_trade", "reason": "no option chain returned", "data_quality_flags": ["missing_option_chain"]}
    data_flags.extend(catalyst.get("data_quality_flags", []))
    liquid_count = len(_liquid(chain))
    if liquid_count < 4:
        return {"symbol": sym, "recommended_strategy": "no_trade", "reason": "insufficient liquid contracts", "liquid_contracts": liquid_count, "data_quality_flags": sorted(set(data_flags + ["poor_option_liquidity"]))}

    atm = min((c for c in chain if c.get("iv")), key=lambda c: abs(c["strike"] - tech["price"]), default=None)
    iv = atm.get("iv") if atm else None
    iv_info = {"method": "missing", "iv_rank": None, "iv_percentile": None, "data_quality_flags": ["missing_atm_iv"]}
    if iv:
        log_atm_iv(sym, iv)
        iv_info = iv_rank(sym, iv)
    data_flags.extend(iv_info.get("data_quality_flags", []))

    risk_budget = min(float(budget), float(portfolio_value) * 0.02)
    ivr = iv_info.get("iv_rank") if iv_info.get("iv_rank") is not None else (iv_info.get("fallback", {}).get("proxy_ivr"))
    candidates: list[dict] = []
    naked = _naked_or_csp(chain, tech["price"], effective_bias, risk_budget)
    spread = _debit_spread(chain, tech["price"], effective_bias, risk_budget, horizon_days)
    credit = _credit_spread(chain, tech["price"], effective_bias, risk_budget) if ivr is not None and ivr >= 50 else None
    if spread:
        candidates.append(spread)
    if credit:
        candidates.append(credit)
    if naked and naked.get("budget_fit") and naked.get("max_loss", 10**9) <= risk_budget * 0.75:
        candidates.append(naked)
    rec = next((c for c in candidates if c.get("budget_fit")), None)
    if rec is None:
        return {"symbol": sym, "recommended_strategy": "no_trade", "reason": "no liquid defined-risk structure fits budget", "risk_budget": risk_budget, "data_quality_flags": sorted(set(data_flags))}

    risk = rec.get("max_loss") or 0
    confidence = 0.45 + (0.1 if catalyst.get("catalyst") else 0) + (0.1 if liquid_count >= 10 else 0) + (0.1 if rec.get("budget_fit") else 0) + (0.05 if not data_flags else -0.05)
    plan = _trade_plan(rec, tech, catalyst, horizon_days)
    return {
        "symbol": sym,
        "bias": effective_bias,
        "tradable": True,
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
        "iv_fallback": iv_info.get("fallback"),
        "recommended_strategy": rec["strategy"],
        "recommended_expiry": rec["expiry"],
        "recommended_legs": rec["legs"],
        "estimated_debit": rec.get("estimated_debit"),
        "estimated_credit": rec.get("estimated_credit"),
        "max_loss": rec.get("max_loss"),
        "max_profit": rec.get("max_profit"),
        "breakeven": rec.get("breakeven"),
        "budget_fit": rec.get("budget_fit"),
        "suggested_contract_count": rec.get("suggested_contract_count"),
        "risk_budget": round(risk_budget, 2),
        "risk_pct_of_portfolio": round((risk * rec.get("suggested_contract_count", 1)) / portfolio_value * 100, 2) if portfolio_value else None,
        "warnings": [w for w in ["illiquid legs" if rec.get("liquidity_flags") else None, "uses proxy/partial IV" if iv_info.get("method") != "self_collected" else None] if w],
        "entry_plan": plan["entry_zone"],
        "exit_plan": f"{plan['profit_target_1']} {plan['profit_target_2']}",
        "invalidation": plan["invalidation"],
        "trade_plan": plan,
        "confidence": round(max(0.0, min(0.95, confidence)), 2),
        "data_quality_flags": sorted(set(data_flags + rec.get("liquidity_flags", []))),
        "technical": tech,
    }

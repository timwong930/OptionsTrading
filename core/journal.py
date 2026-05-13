"""Persistent paper-trade journal for recommendation lifecycle tracking."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from typing import Any
from uuid import uuid4

DB = os.path.join(os.getenv("CACHE_DIR", ".cache"), "paper_trades.sqlite")


def _conn(path: str | None = None) -> sqlite3.Connection:
    db = path or DB
    os.makedirs(os.path.dirname(db), exist_ok=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_trades (
            id TEXT PRIMARY KEY, symbol TEXT, strategy TEXT, status TEXT,
            opened_at TEXT, updated_at TEXT, closed_at TEXT,
            entry_price REAL, exit_price REAL, quantity INTEGER,
            max_risk REAL, thesis TEXT, plan_json TEXT, lessons TEXT
        )"""
    )
    return conn


def _row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["plan"] = json.loads(out.pop("plan_json") or "{}")
    return out


def create_trade(plan: dict[str, Any], thesis: str | None = None, quantity: int | None = None, db_path: str | None = None) -> dict[str, Any]:
    """Create an active paper trade from an engine recommendation."""
    trade_id = str(uuid4())
    now = dt.datetime.now(dt.UTC).isoformat()
    qty = int(quantity or plan.get("suggested_contract_count") or 1)
    entry = plan.get("estimated_debit") or plan.get("estimated_credit") or plan.get("trade_plan", {}).get("ideal_limit_price")
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO paper_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trade_id, plan.get("symbol"), plan.get("recommended_strategy"), "active", now, now, None, entry, None, qty, plan.get("max_loss"), thesis or plan.get("catalyst_summary"), json.dumps(plan, default=str), None),
        )
    return get_trade(trade_id, db_path)


def get_trade(trade_id: str, db_path: str | None = None) -> dict[str, Any]:
    with _conn(db_path) as conn:
        row = conn.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
    if not row:
        raise KeyError(f"paper trade not found: {trade_id}")
    return _row(row)


def update_trade(trade_id: str, db_path: str | None = None, **fields: Any) -> dict[str, Any]:
    """Update mutable paper-trade fields such as status, thesis, lessons or prices."""
    allowed = {"status", "entry_price", "exit_price", "quantity", "max_risk", "thesis", "lessons"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_trade(trade_id, db_path)
    updates["updated_at"] = dt.datetime.now(dt.UTC).isoformat()
    sql = ", ".join(f"{k}=?" for k in updates)
    with _conn(db_path) as conn:
        conn.execute(f"UPDATE paper_trades SET {sql} WHERE id=?", (*updates.values(), trade_id))
    return get_trade(trade_id, db_path)


def close_trade(trade_id: str, exit_price: float, lessons: str | None = None, db_path: str | None = None) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC).isoformat()
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE paper_trades SET status='closed', exit_price=?, lessons=?, closed_at=?, updated_at=? WHERE id=?",
            (float(exit_price), lessons, now, now, trade_id),
        )
    return get_trade(trade_id, db_path)


def list_trades(status: str | None = None, db_path: str | None = None) -> list[dict[str, Any]]:
    with _conn(db_path) as conn:
        if status:
            rows = conn.execute("SELECT * FROM paper_trades WHERE status=? ORDER BY opened_at DESC", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM paper_trades ORDER BY opened_at DESC").fetchall()
    return [_row(r) for r in rows]


def trade_stats(db_path: str | None = None) -> dict[str, Any]:
    trades = list_trades(db_path=db_path)
    closed = [t for t in trades if t["status"] == "closed" and t.get("entry_price") is not None and t.get("exit_price") is not None]
    pnls = []
    for t in closed:
        multiplier = 100
        direction = -1 if t["strategy"] in {"cash_secured_put", "credit_spread"} else 1
        pnls.append(round((t["exit_price"] - t["entry_price"]) * multiplier * t["quantity"] * direction, 2))
    wins = [p for p in pnls if p > 0]
    return {
        "total_trades": len(trades),
        "active_trades": len([t for t in trades if t["status"] == "active"]),
        "closed_trades": len(closed),
        "win_rate": round(len(wins) / len(pnls) * 100, 2) if pnls else None,
        "total_pnl": round(sum(pnls), 2) if pnls else 0.0,
        "average_pnl": round(sum(pnls) / len(pnls), 2) if pnls else None,
    }

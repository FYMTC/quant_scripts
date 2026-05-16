#!/config/quant_env/bin/python3
"""
trade_outbox.py — v5 买卖请示出站（P3）

Hermes gate APPROVE 后调用，写入 agent_state.pending_trade_requests；
供用户确认后由 Hermes 调 stock_kb.record_trade 并 mark_resolved。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

STATE_PATH = "/config/quant_scripts/data/agent_state.json"
OUTBOX_PATH = "/config/quant_scripts/data/trade_request_pending.json"


def _load_state() -> dict:
    if not os.path.isfile(STATE_PATH):
        return {"version": 1, "pending_trade_requests": []}
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"version": 1, "pending_trade_requests": []}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    pending = state.get("pending_trade_requests") or []
    with open(OUTBOX_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "updated_at": datetime.now().isoformat(),
                "count": len([p for p in pending if p.get("status") == "pending"]),
                "requests": pending,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


def propose(
    code: str,
    direction: str,
    *,
    name: str = "",
    price: float = None,
    shares: int = None,
    gate_verdict: str = "APPROVE",
    gate_summary: str = "",
    event_id: str = None,
    signal_id: str = None,
    expires_hours: int = 4,
) -> dict:
    """登记一条待用户确认的买卖请示。"""
    direction = direction.upper()
    if direction not in ("BUY", "SELL"):
        return {"error": f"invalid direction: {direction}"}

    state = _load_state()
    pending: List[dict] = state.setdefault("pending_trade_requests", [])
    rid = str(uuid.uuid4())[:10]
    exp = (datetime.now() + timedelta(hours=expires_hours)).isoformat()
    row = {
        "request_id": rid,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "expires_at": exp,
        "code": code,
        "name": name or code,
        "direction": direction,
        "price": price,
        "shares": shares,
        "gate_verdict": gate_verdict,
        "gate_summary": (gate_summary or "")[:500],
        "event_id": event_id,
        "signal_id": signal_id,
    }
    row["wechat_template"] = _format_wechat(row)
    pending.append(row)
    _save_state(state)
    return {"ok": True, "request_id": rid, "wechat_template": row.get("wechat_template")}


def _format_wechat(row: dict) -> str:
    p = row.get("price")
    sh = row.get("shares")
    return (
        f"【买卖请示】{row.get('direction')} {row.get('name')}({row.get('code')})\n"
        f"建议: 价{p if p else '市价'} 量{sh if sh else '待定'}股\n"
        f"门禁: {(row.get('gate_summary') or 'APPROVE')[:200]}\n"
        f"有效期至 {row.get('expires_at', '')[:16]}\n"
        f"请回复: 同意 / 拒绝 (id={row.get('request_id')})"
    )


def resolve(request_id: str, outcome: str, *, note: str = "") -> dict:
    """用户确认后：resolved | rejected | expired"""
    state = _load_state()
    for p in state.get("pending_trade_requests") or []:
        if p.get("request_id") == request_id:
            p["status"] = outcome
            p["resolved_at"] = datetime.now().isoformat()
            p["note"] = note
            _save_state(state)
            return {"ok": True, "request_id": request_id}
    return {"error": "request_id not found"}


def list_pending() -> List[dict]:
    state = _load_state()
    return [p for p in state.get("pending_trade_requests") or [] if p.get("status") == "pending"]


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("propose")
    p.add_argument("code")
    p.add_argument("direction", choices=["BUY", "SELL"])
    p.add_argument("--name", default="")
    p.add_argument("--price", type=float)
    p.add_argument("--shares", type=int)
    p.add_argument("--summary", default="")
    r = sub.add_parser("resolve")
    r.add_argument("request_id")
    r.add_argument("outcome", choices=["resolved", "rejected", "expired"])
    l = sub.add_parser("list")
    args = ap.parse_args()
    if args.cmd == "propose":
        print(json.dumps(propose(args.code, args.direction, name=args.name, price=args.price, shares=args.shares, gate_summary=args.summary), ensure_ascii=False, indent=2))
    elif args.cmd == "resolve":
        print(json.dumps(resolve(args.request_id, args.outcome), ensure_ascii=False))
    elif args.cmd == "list":
        print(json.dumps(list_pending(), ensure_ascii=False, indent=2))

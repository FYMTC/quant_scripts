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
    lineage_id: str = None,
    lineage_stages: list = None,
    expires_hours: int = 4,
    account_id: str = None,
    decision_gate: dict = None,
) -> dict:
    """登记一条待用户确认的买卖请示。"""
    direction = direction.upper()
    if direction not in ("BUY", "SELL"):
        return {"error": f"invalid direction: {direction}"}

    try:
        from trade_accounts import HermesTradingError, get_account, resolve_trading_account

        aid = resolve_trading_account(account_id)
        acct = get_account(aid)
        account_label = acct.get("label") or aid
    except HermesTradingError as exc:
        return {"error": str(exc), "hermes_trading_stopped": True}
    except Exception as exc:
        return {"error": f"trade account: {exc}"}

    state = _load_state()
    pending: List[dict] = state.setdefault("pending_trade_requests", [])
    rid = str(uuid.uuid4())[:10]
    exp = (datetime.now() + timedelta(hours=expires_hours)).isoformat()
    lid = lineage_id
    try:
        from core.engines import signal_lineage as sl

        if not lid:
            lid = sl.new_lineage_id("trade")
        sl.append(
            "PROPOSE",
            "trade_outbox",
            code=code,
            lineage_id=lid,
            payload={
                "summary": (gate_summary or "")[:200],
                "direction": direction,
                "gate_verdict": gate_verdict,
                "event_id": event_id,
                "signal_id": signal_id,
            },
        )
        for st in lineage_stages or []:
            if isinstance(st, dict):
                sl.append(
                    st.get("stage", "STEP"),
                    st.get("source", "hermes"),
                    code=code,
                    lineage_id=lid,
                    payload=st.get("payload") or st,
                )
    except Exception:
        pass

    row = {
        "request_id": rid,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "expires_at": exp,
        "account_id": aid,
        "account_label": account_label,
        "code": code,
        "name": name or code,
        "direction": direction,
        "price": price,
        "shares": shares,
        "gate_verdict": gate_verdict,
        "gate_summary": (gate_summary or "")[:500],
        "decision_gate": decision_gate if isinstance(decision_gate, dict) else None,
        "event_id": event_id,
        "signal_id": signal_id,
        "lineage_id": lid,
    }
    row["wechat_template"] = _format_wechat(row)
    pending.append(row)
    _save_state(state)
    return {
        "ok": True,
        "request_id": rid,
        "account_id": aid,
        "wechat_template": row.get("wechat_template"),
    }


def propose_and_notify(
    code: str,
    direction: str,
    **kwargs,
) -> dict:
    out = propose(code, direction, **kwargs)
    if not out.get("ok"):
        return out

    body = out.get("wechat_template") or ""
    if body.strip().lower() in {"tpl", "buy tpl", "sell tpl"}:
        state = _load_state()
        row = _find_request(state, out.get("request_id", ""))
        if row:
            body = _format_wechat(row)
            row["wechat_template"] = body
            _save_state(state)
            out["wechat_template"] = body
    notify = None
    if body:
        try:
            from trade_notify import enqueue_wechat

            notify = enqueue_wechat(
                body,
                kind="trade_request",
                meta={
                    "request_id": out.get("request_id"),
                    "account_id": out.get("account_id"),
                    "direction": direction.upper(),
                    "code": code,
                },
            )
        except Exception as exc:
            notify = {"ok": False, "error": str(exc)[:300]}

    out["wechat_notify"] = notify
    out["wechat_sent"] = bool((notify or {}).get("ok"))
    return out


def _format_wechat(row: dict) -> str:
    p = row.get("price")
    sh = row.get("shares")
    acct = row.get("account_label") or row.get("account_id") or ""
    acct_line = f"账户: {acct}\n" if acct else ""
    head = (
        f"【买卖请示】{row.get('direction')} {row.get('name')}({row.get('code')})\n"
        f"{acct_line}"
        f"建议: 价{p if p else '市价'} 量{sh if sh else '待定'}股\n"
        f"门禁: {(row.get('gate_summary') or 'APPROVE')[:200]}\n"
        f"有效期至 {row.get('expires_at', '')[:16]}\n"
        f"追溯ID: {row.get('lineage_id') or '-'}\n"
    )
    trail = ""
    lid = row.get("lineage_id")
    if lid:
        try:
            from core.engines.signal_lineage import format_timeline

            trail = "\n" + format_timeline(lid, max_chars=1200)
        except Exception:
            trail = ""
    return head + trail + f"\n请回复: 同意 / 拒绝 (id={row.get('request_id')})"


def _find_request(state: dict, request_id: str) -> Optional[dict]:
    for p in state.get("pending_trade_requests") or []:
        if p.get("request_id") == request_id:
            return p
    return None


def resolve(request_id: str, outcome: str, *, note: str = "") -> dict:
    """用户确认后：resolved | rejected | expired（不自动下单）。"""
    state = _load_state()
    p = _find_request(state, request_id)
    if not p:
        return {"error": "request_id not found"}
    p["status"] = outcome
    p["resolved_at"] = datetime.now().isoformat()
    p["note"] = note
    _save_state(state)
    return {"ok": True, "request_id": request_id, "account_id": p.get("account_id")}


def resolve_and_execute(request_id: str, outcome: str, *, note: str = "") -> dict:
    """微信同意后：resolve + 按账户自动执行（paper_easyths）+ 成交回报推微信。"""
    res = resolve(request_id, outcome, note=note)
    if not res.get("ok"):
        return res
    state = _load_state()
    p = _find_request(state, request_id)
    if not p:
        return {"error": "request_id not found after resolve"}

    from trade_execution import after_resolve

    follow = after_resolve(p, outcome, note=note)
    p["post_resolve"] = follow
    if follow.get("execution"):
        p["execution"] = follow["execution"]
    _save_state(state)
    return {**res, **follow}


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
    p.add_argument("--lineage-id", default="")
    p.add_argument("--account", default="", help="account_id，默认 trade_accounts.yaml")
    r = sub.add_parser("resolve")
    r.add_argument("request_id")
    r.add_argument("outcome", choices=["resolved", "rejected", "expired"])
    r.add_argument("--note", default="")
    rexe = sub.add_parser("resolve-and-execute", help="同意并自动执行（多账户）")
    rexe.add_argument("request_id")
    rexe.add_argument("outcome", choices=["resolved", "rejected", "expired"])
    rexe.add_argument("--note", default="")
    acc = sub.add_parser("accounts")
    l = sub.add_parser("list")
    args = ap.parse_args()
    if args.cmd == "propose":
        print(
            json.dumps(
                propose(
                    args.code,
                    args.direction,
                    name=args.name,
                    price=args.price,
                    shares=args.shares,
                    gate_summary=args.summary,
                    lineage_id=args.lineage_id or None,
                    account_id=args.account or None,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.cmd == "resolve":
        print(json.dumps(resolve(args.request_id, args.outcome, note=args.note), ensure_ascii=False))
    elif args.cmd == "resolve-and-execute":
        print(
            json.dumps(
                resolve_and_execute(args.request_id, args.outcome, note=args.note),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.cmd == "accounts":
        from trade_accounts import status_report

        print(json.dumps(status_report(), ensure_ascii=False, indent=2))
    elif args.cmd == "list":
        print(json.dumps(list_pending(), ensure_ascii=False, indent=2))

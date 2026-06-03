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

ARCHIVE_PATH = "/config/quant_scripts/data/trade_request_history_archive.json"
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


def _trim_history_rows(rows: List[dict], *, now: Optional[datetime] = None) -> List[dict]:
    current = now or datetime.now()
    kept: List[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "")
        if status == "pending":
            kept.append(row)
            continue
        resolved_at = str(row.get("resolved_at") or row.get("created_at") or "")
        try:
            ts = datetime.fromisoformat(resolved_at)
        except ValueError:
            kept.append(row)
            continue
        age_days = (current - ts).days
        if age_days <= 7:
            kept.append(row)
    return kept


def _load_archive() -> dict:
    if not os.path.isfile(ARCHIVE_PATH):
        return {"version": 1, "archived_trade_requests": []}
    try:
        with open(ARCHIVE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("version", 1)
        data.setdefault("archived_trade_requests", [])
        return data
    except Exception:
        return {"version": 1, "archived_trade_requests": []}


def _save_archive(data: dict) -> None:
    os.makedirs(os.path.dirname(ARCHIVE_PATH), exist_ok=True)
    with open(ARCHIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _archive_legacy_rows(rows: List[dict]) -> List[dict]:
    archive = _load_archive()
    archived = archive.get("archived_trade_requests") or []
    archived_ids = {str(row.get("request_id") or "") for row in archived if isinstance(row, dict)}
    kept: List[dict] = []
    changed = False
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        account_id = str(row.get("account_id") or "")
        if account_id != "manual_wechat":
            kept.append(row)
            continue
        request_id = str(row.get("request_id") or "")
        if request_id and request_id not in archived_ids:
            archived.append(row)
            archived_ids.add(request_id)
            changed = True
        elif not request_id:
            archived.append(row)
            changed = True
    if changed:
        archive["version"] = 1
        archive["archived_trade_requests"] = archived
        archive["updated_at"] = datetime.now().isoformat()
        _save_archive(archive)
    return kept
def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    pending = _trim_history_rows(state.get("pending_trade_requests") or [])
    pending = _archive_legacy_rows(pending)
    state["pending_trade_requests"] = pending
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
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
        from trade_accounts import HermesTradingError, auto_execute_on_resolve, get_account, resolve_trading_account

        aid = resolve_trading_account(account_id)
        acct = get_account(aid)
        account_label = acct.get("label") or aid
        auto_execute = auto_execute_on_resolve(aid)
    except HermesTradingError as exc:
        return {"error": str(exc), "hermes_trading_stopped": True}
    except Exception as exc:
        return {"error": f"trade account: {exc}"}

    # ── same-day dedup: reject if already have pending/resolved for same code+direction+account ──
    state = _load_state()
    today = datetime.now().strftime("%Y-%m-%d")
    for row in state.get("pending_trade_requests") or []:
        if str(row.get("code") or "") != code:
            continue
        if str(row.get("direction") or "").upper() != direction:
            continue
        if str(row.get("account_id") or "") != aid:
            continue
        if today not in str(row.get("created_at") or ""):
            continue
        status = str(row.get("status") or "")
        if status in ("pending", "resolved"):
            return {
                "ok": False,
                "duplicate": True,
                "error": f"duplicate {direction} {code}: already {status} today (request_id={row.get('request_id')})",
            }

    # ── same-day reversal block: don't BUY a stock sold today ──
    if direction == "BUY":
        try:
            from post_execution_rescan import get_executed_sell_codes_today
            if code in get_executed_sell_codes_today():
                return {
                    "ok": False,
                    "duplicate": True,
                    "reversal_blocked": True,
                    "error": f"same-day reversal blocked: {code} was sold today, cannot re-buy",
                }
        except Exception:
            pass

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
        "proposal_generated_at": ((decision_gate or {}).get("proposal_generated_at") if isinstance(decision_gate, dict) else None),
        "event_id": event_id,
        "signal_id": signal_id,
        "lineage_id": lid,
        "auto_execute": auto_execute,
    }
    row["wechat_template"] = _format_wechat(row)
    pending.append(row)
    _save_state(state)
    return {
        "ok": True,
        "request_id": rid,
        "account_id": aid,
        "auto_execute": auto_execute,
        "wechat_template": row.get("wechat_template"),
    }


def _trading_hours_now() -> bool:
    """A股连续竞价时段：09:30-11:30, 13:00-15:00 CST (= UTC+8)"""
    import time as _time_module
    now = datetime.fromtimestamp(_time_module.time())
    hhmm = now.hour * 100 + now.minute
    # UTC hours for A-share sessions: 01:30-03:30 and 05:00-07:00
    in_morning = 130 <= hhmm < 330   # 01:30-03:29 UTC = 09:30-11:29 CST
    in_afternoon = 500 <= hhmm < 700  # 05:00-06:59 UTC = 13:00-14:59 CST
    return in_morning or in_afternoon


def propose_and_notify(
    code: str,
    direction: str,
    **kwargs,
) -> dict:
    out = propose(code, direction, **kwargs)
    if not out.get("ok"):
        return out

    auto_execute = bool(out.get("auto_execute"))
    notify = None
    if auto_execute:
        if not _trading_hours_now():
            out["deferred"] = True
            out["deferred_reason"] = "outside_trading_hours"
            out["wechat_notify"] = {"ok": False, "reason": "deferred: outside A-share trading hours (09:30-15:00 CST)"}
            out["wechat_sent"] = False
            return out
        executed = resolve_and_execute(out.get("request_id", ""), "resolved", note="auto-executed for paper trading")
        out["auto_resolved"] = True
        out["execution"] = executed.get("execution")
        out["wechat_notify"] = executed.get("wechat_notify")
        out["wechat_body"] = executed.get("wechat_body")
        out["executed"] = executed.get("executed")
        out["resolved_status"] = "resolved"
        out["wechat_sent"] = bool((executed.get("wechat_notify") or {}).get("ok"))
        return out

    body = out.get("wechat_template") or ""
    placeholder_bodies = {"tpl", "buy tpl", "sell tpl", "template", "placeholder"}
    if body.strip().lower() in placeholder_bodies:
        state = _load_state()
        row = _find_request(state, out.get("request_id", ""))
        if row:
            body = _format_wechat(row)
            row["wechat_template"] = body
            _save_state(state)
            out["wechat_template"] = body
        else:
            body = ""
    if not body:
        state = _load_state()
        row = _find_request(state, out.get("request_id", ""))
        if row:
            body = _format_wechat(row)
            row["wechat_template"] = body
            _save_state(state)
            out["wechat_template"] = body
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
    else:
        notify = {"ok": False, "error": "wechat_template_unavailable"}

    out["wechat_notify"] = notify
    out["wechat_sent"] = bool((notify or {}).get("ok"))
    return out


def _format_wechat(row: dict) -> str:
    p = row.get("price")
    sh = row.get("shares")
    acct = row.get("account_label") or row.get("account_id") or ""
    acct_line = f"账户: {acct}\n" if acct else ""
    auto_execute = bool(row.get("auto_execute"))
    reply_line = "模拟盘自动执行，无需人工回复" if auto_execute else f"请回复: 同意 / 拒绝 (id={row.get('request_id')})"
    lid = row.get("lineage_id") or row.get("event_id") or ""
    sid = row.get("signal_id") or ""
    dg = row.get("decision_gate") or {}

    # ── strategy source label ──
    source_labels = {
        "morning_plan": "📊 早盘量化候选",
        "de_risk_plan": "🛡 风控减仓计划",
        "rolling_decline": "📉 连续阴跌强减",
        "rapid_drop": "⚡ 急跌强减",
        "price_below": "🔻 破位强减",
    }
    source_label = source_labels.get(sid, f"信号决策" if sid else "手动/外部")

    # ── gate verdict badge ──
    verdict_icons = {"APPROVE": "✅", "MODIFY": "🟡", "REJECT": "❌"}
    verdict_icon = verdict_icons.get(row.get("gate_verdict", ""), "•")

    # ── build the head ──
    lines = [
        f"【买卖请示】{row.get('direction')} {row.get('name')}({row.get('code')})",
        f"{acct_line}",
        f"来源: {source_label}",
        f"建议: 价{p if p else '市价'} 量{sh if sh else '待定'}股",
        f"门禁裁决: {verdict_icon} {row.get('gate_verdict', 'APPROVE')}",
    ]

    # ── gate-by-gate details ──
    gates = dg.get("gates") or []
    if gates:
        gate_names = {0: "RG:研究特征", 1: "G1:评分映射", 2: "G2:T+1合规", 3: "G3:风控校验", 4: "G4:仓位评估"}
        lines.append("门禁流程:")
        for i, g in enumerate(gates[:5]):
            icon = "✓" if g.get("pass") else "✗"
            msg = str(g.get("message") or "")[:80]
            name = gate_names.get(i, f"G{i}")
            lines.append(f"  {icon} {name}: {msg}")
        # scores if available
        composite = dg.get("composite_score")
        if composite:
            lines.append(f"  综合评分: {composite}")

    # ── rationale ──
    summary = (row.get("gate_summary") or "").strip()
    if summary and summary != "APPROVE" and summary != row.get("gate_verdict", ""):
        lines.append(f"决策摘要: {summary[:200]}")
    reasons = dg.get("reasons") or []
    if reasons and reasons != [summary]:
        for reason in reasons[:3]:
            if reason.strip() and reason.strip() != summary:
                lines.append(f"  · {str(reason)[:120]}")

    # ── trace ──
    lines.append(f"有效期至 {str(row.get('expires_at', ''))[:16]}")
    lines.append(f"追溯ID: {lid or '-'}")

    # ── lineage timeline ──
    trail = ""
    if lid:
        try:
            from core.engines.signal_lineage import format_timeline
            trail = "\n" + format_timeline(lid, max_chars=1200)
        except Exception:
            trail = ""
    return "\n".join(lines) + trail + f"\n\n{reply_line}"


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

    # ── record SELL execution for post-sell rescan ──
    if follow.get("executed") and str(p.get("direction") or "").upper() == "SELL":
        try:
            import post_execution_rescan as psr
            psr.record_executed_sell(
                code=str(p.get("code") or ""),
                shares=int(p.get("shares") or 0),
                account_id=str(p.get("account_id") or ""),
                signal_id=str(p.get("signal_id") or ""),
            )
        except Exception:
            pass

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

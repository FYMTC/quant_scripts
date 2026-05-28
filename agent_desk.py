#!/config/quant_env/bin/python3
"""
agent_desk.py — v5 Agent Desk：消费 agent_queue，跑 signal_loop 硬过滤 + 量化上下文。

stdout JSON 供 Hermes 短 prompt 使用：
  needs_hermes: false → 无待分析事件或全部 SKIP，Cron 应静默
  needs_hermes: true  → 含 analyze_tasks，由 Hermes 跑 TradingAgents + decision_gate
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(__file__))

from agent_queue import ack, list_pending, pending_count

RUNTIME_DATA_DIR = os.environ.get("QUANT_RUNTIME_DATA_DIR") or "/config/quant_scripts/data"
PLAYBOOK_DIR = os.path.join(RUNTIME_DATA_DIR, "playbooks")
STATE_PATH = os.path.join(RUNTIME_DATA_DIR, "agent_state.json")
MORNING_OUTPUT_PATH = os.path.join(RUNTIME_DATA_DIR, "morning_output.json")


def _load_json(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_agent_state(patch: dict) -> None:
    state = _load_json(STATE_PATH)
    state["updated_at"] = datetime.now().isoformat()
    state.update(patch)
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _load_playbook(code: str) -> List[dict]:
    path = os.path.join(PLAYBOOK_DIR, f"{code}.yaml")
    if not os.path.isfile(path):
        return []
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, list) else data.get("patterns", []) if isinstance(data, dict) else []
    except Exception:
        return []


def _run_registry_plugins(code: str, triggers: tuple = ("decide",)) -> List[dict]:
    """P5：对 experimental/production 插件执行 run(ctx)。"""
    reg_path = "/config/quant_scripts/data/quant_registry.yaml"
    if not os.path.isfile(reg_path):
        return []
    try:
        import yaml
        with open(reg_path, encoding="utf-8") as f:
            reg = yaml.safe_load(f) or {}
    except Exception:
        return []
    plugins = reg.get("plugins") or []
    results = []
    ctx_base = {"code": code}
    try:
        from data_converter import fetch_kline_baostock
        from datetime import date
        end = date.today().strftime("%Y%m%d")
        start = date.today().replace(year=date.today().year - 1).strftime("%Y%m%d")
        rec = fetch_kline_baostock(code, start, end)
        ctx_base["closes"] = [float(r["收盘"]) for r in rec] if rec else []
    except Exception:
        ctx_base["closes"] = []

    for pl in plugins:
        if pl.get("status") not in ("production", "experimental"):
            continue
        tr = pl.get("triggers") or []
        if not any(t in tr for t in triggers):
            continue
        mod = pl.get("module", "")
        if ":" not in mod:
            continue
        mod_path, func_name = mod.split(":", 1)
        try:
            import importlib
            m = importlib.import_module(mod_path.replace("/", "."))
            fn = getattr(m, func_name)
            out = fn(ctx_base)
            results.append({"plugin_id": pl.get("id"), "result": out})
        except Exception as e:
            results.append({"plugin_id": pl.get("id"), "error": str(e)[:200]})
    return results


def _fetch_quant_context(code: str) -> dict:
    try:
        from tradingagents_runner import fetch_quant_context
        ctx = fetch_quant_context(code)
        return ctx if isinstance(ctx, dict) else {"note": str(ctx)[:500]}
    except Exception as e:
        return {"error": str(e)[:200]}


def _stock_insights(code: str, limit: int = 8) -> List[dict]:
    try:
        from stock_kb import StockKB
        rows = StockKB().get_insights(code, limit=limit)
        return [
            {
                "date": r.get("insight_date"),
                "category": r.get("category"),
                "content": (r.get("content") or "")[:300],
                "confidence": r.get("confidence"),
            }
            for r in rows
        ]
    except Exception as e:
        return [{"error": str(e)[:120]}]


def _latest_apps_snapshot() -> dict:
    """合并最近一档盘中 JSON 路径（供 Hermes 只读）。"""
    paths = [
        "afternoon_output.json",
        "noon_output.json",
        "midday_output.json",
        "flash_output.json",
        "morning_output.json",
    ]
    base = "/config/quant_scripts/data"
    out = {}
    for name in paths:
        p = os.path.join(base, name)
        if os.path.isfile(p):
            out[name.replace("_output.json", "")] = _load_json(p)
    return out


def _position_from_snapshot(account_snapshot: dict, code: str) -> Optional[dict]:
    for row in account_snapshot.get("positions") or []:
        if str(row.get("code") or "").strip() == code:
            return row
    return None


FORCED_SELL_KEYWORDS = ("连跌", "累计", "止损", "急跌", "大跌", "跌破")


def _extract_research_features(code: str, quant_context: dict) -> dict:
    if not isinstance(quant_context, dict):
        return {}

    feature = quant_context.get("feature_snapshot")
    if isinstance(feature, dict) and any(k in feature for k in ("feature_fresh", "risk_level", "market_regime", "cvar")):
        return feature

    per_stock = quant_context.get("per_stock") or {}
    row = per_stock.get(code) or per_stock.get(str(code)) or {}
    portfolio = quant_context.get("portfolio") or {}
    market_regime = (portfolio.get("market_regime") or {}).get("current_state") or quant_context.get("market_regime")
    feature_fresh = quant_context.get("feature_fresh")
    if feature_fresh is None:
        runtime_flags = quant_context.get("runtime_flags") or {}
        feature_fresh = runtime_flags.get("feature_fresh")

    out = {
        "feature_fresh": bool(feature_fresh) if feature_fresh is not None else False,
        "risk_level": row.get("risk_level"),
        "market_regime": market_regime,
        "cvar": row.get("cvar"),
        "risk_reasons": row.get("risk_reasons") or [],
    }
    return out if any(v is not None and v != [] for v in out.values()) else {}


def _run_decision_gate_for_event(*, event: dict, quant_context: dict) -> dict:
    try:
        from decision_gate import DecisionGate

        scores = (quant_context or {}).get("analyst_scores") or {}
        direction = "SELL" if event.get("signal_id") in ("rolling_decline", "rapid_drop", "price_below") else "BUY"
        result = DecisionGate().check(
            ticker=event.get("code", ""),
            direction=direction,
            analyst_scores=scores,
            current_price=float(event.get("price") or 0),
            research_features=_extract_research_features(event.get("code", ""), quant_context),
        )
        try:
            from decision_explainer import build_counterfactual_from_gate

            result["counterfactual"] = build_counterfactual_from_gate(result)
        except Exception as exc:
            result["counterfactual"] = {"summary": f"counterfactual unavailable: {str(exc)[:120]}"}
        return result
    except Exception as exc:
        return {"verdict": "ERROR", "reasons": [str(exc)[:200]], "error": str(exc)[:200]}


def _build_trade_request_from_decision(
    *,
    event: dict,
    handle_result: dict,
    trading_account: str,
    decision_gate_result: dict,
) -> Optional[dict]:
    if not isinstance(decision_gate_result, dict):
        return None
    if decision_gate_result.get("verdict") != "APPROVE":
        return None

    direction = str(decision_gate_result.get("direction") or "").upper()
    if direction not in ("BUY", "SELL"):
        return None

    suggested_shares = int(decision_gate_result.get("suggested_shares") or 0)
    price = float(event.get("price") or 0) or None
    counterfactual = (decision_gate_result.get("counterfactual") or {}).get("summary") or ""
    mapped_action = decision_gate_result.get("mapped_action") or direction
    reasons = decision_gate_result.get("reasons") or []
    summary_parts = [
        f"门禁通过: {mapped_action}",
        reasons[0] if reasons else "",
        counterfactual,
    ]
    gate_summary = "；".join([p for p in summary_parts if p])[:500]

    try:
        import trade_outbox

        proposal = trade_outbox.propose_and_notify(
            event.get("code", ""),
            direction,
            name=event.get("name", event.get("code", "")),
            price=price,
            shares=suggested_shares or None,
            gate_verdict="APPROVE",
            gate_summary=gate_summary,
            event_id=event.get("event_id"),
            signal_id=event.get("signal_id"),
            lineage_id=handle_result.get("lineage_id"),
            account_id=trading_account,
            decision_gate=decision_gate_result,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300], "direction": direction}

    if not proposal.get("ok"):
        return proposal

    return {
        "ok": True,
        "direction": direction,
        "request_id": proposal.get("request_id"),
        "wechat_template": proposal.get("wechat_template"),
        "wechat_notify": proposal.get("wechat_notify"),
        "wechat_sent": proposal.get("wechat_sent"),
        "shares": suggested_shares or None,
    }


def _build_trade_request_from_plan(*, proposal: dict, trading_account: str) -> Optional[dict]:
    if not isinstance(proposal, dict):
        return None
    code = str(proposal.get("code") or "").strip()
    direction = str(proposal.get("direction") or "BUY").upper()
    shares = int(proposal.get("shares") or 0)
    if not code or direction not in ("BUY", "SELL") or shares <= 0:
        return None

    rationale = str(proposal.get("rationale") or "")[:500]
    try:
        import trade_outbox

        out = trade_outbox.propose_and_notify(
            code,
            direction,
            name=proposal.get("name") or code,
            price=float(proposal.get("price") or 0) or None,
            shares=shares,
            gate_verdict="APPROVE",
            gate_summary=rationale or f"morning_plan {direction}",
            signal_id="morning_plan",
            lineage_id=proposal.get("lineage_id"),
            account_id=trading_account,
            decision_gate={
                "verdict": "APPROVE",
                "direction": direction,
                "mapped_action": direction,
                "reasons": [rationale] if rationale else [],
                "suggested_shares": shares,
                "source": "morning_plan",
                "proposal_generated_at": proposal.get("proposal_generated_at"),
            },
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300], "direction": direction, "code": code}

    if not out.get("ok"):
        return out

    return {
        "ok": True,
        "source": "morning_plan",
        "code": code,
        "direction": direction,
        "shares": shares,
        "request_id": out.get("request_id"),
        "wechat_template": out.get("wechat_template"),
        "wechat_notify": out.get("wechat_notify"),
        "wechat_sent": out.get("wechat_sent"),
    }


def _expire_stale_pending_requests(*, now: Optional[datetime] = None) -> int:
    state = _load_json(STATE_PATH)
    rows = state.get("pending_trade_requests") or []
    if not isinstance(rows, list) or not rows:
        return 0

    current = now or datetime.now()
    changed = 0
    for row in rows:
        if str(row.get("status") or "") != "pending":
            continue
        expires_at = str(row.get("expires_at") or "")
        if not expires_at:
            continue
        try:
            expired = datetime.fromisoformat(expires_at) < current
        except ValueError:
            continue
        if not expired:
            continue
        row["status"] = "expired"
        row["resolved_at"] = current.isoformat()
        row.setdefault("note", "")
        note = str(row.get("note") or "").strip()
        row["note"] = "auto-expired by agent_desk" if not note else note
        changed += 1

    if changed:
        state["pending_trade_requests"] = rows
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        try:
            import trade_outbox

            trade_outbox._save_state(state)
        except Exception:
            pass
    return changed


def _emit_morning_plan_requests(*, trading_account: Optional[str], account_snapshot: dict) -> List[dict]:
    if not trading_account:
        return []
    data = _load_json(MORNING_OUTPUT_PATH)
    proposals = data.get("buy_proposals") or []
    if not proposals:
        return []

    proposal_generated_at = str(data.get("generated_at") or "")
    existing_codes = set()
    state = _load_json(STATE_PATH)
    now = datetime.now()
    for row in state.get("pending_trade_requests") or []:
        if row.get("signal_id") != "morning_plan":
            continue
        if str(row.get("account_id") or "") != str(trading_account):
            continue
        row_generated_at = str(row.get("proposal_generated_at") or "")
        if proposal_generated_at and row_generated_at and row_generated_at != proposal_generated_at:
            continue
        status = str(row.get("status") or "")
        if status == "pending":
            expires_at = str(row.get("expires_at") or "")
            if expires_at:
                try:
                    if datetime.fromisoformat(expires_at) < now:
                        continue
                except ValueError:
                    pass
            existing_codes.add(str(row.get("code") or ""))
            continue
        if status == "resolved":
            existing_codes.add(str(row.get("code") or ""))

    emitted = []
    for proposal in proposals:
        code = str(proposal.get("code") or "")
        if not code or code in existing_codes:
            continue
        proposal_payload = dict(proposal)
        if proposal_generated_at and not proposal_payload.get("proposal_generated_at"):
            proposal_payload["proposal_generated_at"] = proposal_generated_at
        result = _build_trade_request_from_plan(proposal=proposal_payload, trading_account=trading_account)
        if result:
            emitted.append(result)
            existing_codes.add(code)
    return emitted


def _build_forced_risk_request(
    *,
    event: dict,
    handle_result: dict,
    account_snapshot: dict,
    trading_account: str,
    decision_gate_result: dict,
) -> Optional[dict]:
    code = event.get("code", "")
    position = _position_from_snapshot(account_snapshot, code)
    if not position:
        return None

    reason = event.get("reason", "")
    signal_id = event.get("signal_id", "")
    if not any(k in reason for k in FORCED_SELL_KEYWORDS) and not any(
        k in signal_id for k in ("rolling_decline", "rapid_drop", "price_below")
    ):
        return None

    shares = int(position.get("shares") or 0)
    if shares <= 0:
        return None

    lot_shares = shares if shares < 100 else max(100, (shares // 2 // 100) * 100)
    if lot_shares <= 0:
        lot_shares = shares

    gate_summary = f"风险事件强制减仓请示: {reason[:120]}"

    try:
        import trade_outbox

        proposal = trade_outbox.propose_and_notify(
            code,
            "SELL",
            name=event.get("name", code),
            price=float(event.get("price") or 0) or None,
            shares=lot_shares,
            gate_verdict="APPROVE",
            gate_summary=gate_summary,
            event_id=event.get("event_id"),
            signal_id=signal_id,
            lineage_id=handle_result.get("lineage_id"),
            lineage_stages=[
                {
                    "stage": "DESK_FORCED_RISK",
                    "source": "agent_desk",
                    "payload": {
                        "summary": gate_summary,
                        "reason": reason[:200],
                        "position_shares": shares,
                        "suggested_shares": lot_shares,
                    },
                }
            ],
            account_id=trading_account,
            decision_gate=decision_gate_result if isinstance(decision_gate_result, dict) else None,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc)[:300],
            "reason": reason,
            "forced_risk": True,
        }

    if proposal.get("ok"):
        return {
            "ok": True,
            "forced_risk": True,
            "direction": "SELL",
            "shares": lot_shares,
            "request_id": proposal.get("request_id"),
            "wechat_template": proposal.get("wechat_template"),
            "wechat_notify": proposal.get("wechat_notify"),
            "wechat_sent": proposal.get("wechat_sent"),
            "reason": reason,
        }
    return {"forced_risk": True, **proposal, "reason": reason}


def process_pending(*, max_events: int = 5, trading_account_id: str = None) -> Dict[str, Any]:
    from signal_loop import handle_trigger

    expired_count = _expire_stale_pending_requests()

    trading_account = None
    account_snapshot = {}
    desk_account_error = None
    try:
        from trade_accounts import HermesTradingError, resolve_trading_account
        from trade_account_context import load_account_snapshot

        trading_account = resolve_trading_account(trading_account_id)
        account_snapshot = load_account_snapshot(trading_account)
    except Exception as exc:
        desk_account_error = str(exc)[:500]

    pending = list_pending(limit=max_events)
    skipped: List[dict] = []
    analyze_tasks: List[dict] = []
    forced_trade_requests: List[dict] = []
    planned_trade_requests = _emit_morning_plan_requests(
        trading_account=trading_account,
        account_snapshot=account_snapshot,
    ) if not desk_account_error else []

    for ev in pending:
        eid = ev.get("event_id", "")
        if desk_account_error:
            ack(eid, result={"action": "SKIP", "reason": "hermes_trading_stopped"})
            skipped.append({"event_id": eid, "reason": "hermes_trading_stopped", "error": desk_account_error})
            continue
        if not ev.get("parse_ok", True):
            ack(eid, result={"action": "SKIP", "reason": "parse_failed"})
            skipped.append({"event_id": eid, "reason": "parse_failed"})
            continue

        code = ev.get("code", "")
        sid = ev.get("signal_id", "")
        price = float(ev.get("price") or 0)
        pct = float(ev.get("change_pct") or 0)
        vol = float(ev.get("volume") or 0)

        hr = handle_trigger(sid, code, price, pct, vol)
        quant_context = _fetch_quant_context(code)
        decision_gate_result = _run_decision_gate_for_event(event=ev, quant_context=quant_context)
        forced_request = _build_forced_risk_request(
            event=ev,
            handle_result=hr,
            account_snapshot=account_snapshot,
            trading_account=trading_account,
            decision_gate_result=decision_gate_result,
        )
        if forced_request:
            ack_result = {**hr, "forced_trade_request": forced_request, "decision_gate": decision_gate_result}
            ack(eid, result=ack_result)
            forced_trade_requests.append(
                {
                    "event_id": eid,
                    "code": code,
                    "name": ev.get("name", code),
                    **forced_request,
                }
            )
            continue
        action = hr.get("action", "SKIP")

        if action != "ANALYZE":
            ack(eid, result=hr)
            skipped.append({"event_id": eid, "code": code, **hr})
            continue

        lineage_id = hr.get("lineage_id") or ""
        try:
            from core.engines import signal_lineage as sl

            if not lineage_id:
                lineage_id = sl.new_lineage_id("desk")
            sl.append(
                "DESK_ENQUEUE",
                "agent_desk",
                code=code,
                lineage_id=lineage_id,
                payload={
                    "summary": ev.get("reason", "")[:200],
                    "event_id": eid,
                    "signal_id": sid,
                    "action": action,
                },
            )
        except Exception:
            pass

        normal_request = _build_trade_request_from_decision(
            event=ev,
            handle_result=hr,
            trading_account=trading_account,
            decision_gate_result=decision_gate_result,
        )

        task = {
            "event_id": eid,
            "signal_id": sid,
            "lineage_id": lineage_id,
            "trading_account_id": trading_account,
            "account_snapshot": account_snapshot,
            "code": code,
            "name": ev.get("name", code),
            "reason": ev.get("reason", ""),
            "price": price,
            "change_pct": pct,
            "handle_trigger": hr,
            "decision_gate": decision_gate_result,
            "counterfactual": (decision_gate_result or {}).get("counterfactual") or {},
            "trade_request": normal_request,
            "quant_context": quant_context,
            "stock_insights": _stock_insights(code),
            "playbook_patterns": _load_playbook(code),
            "registry_plugins": _run_registry_plugins(code),
        }
        analyze_tasks.append(task)

    needs = (
        len(analyze_tasks) > 0
        or len(forced_trade_requests) > 0
        or len(planned_trade_requests) > 0
    ) and trading_account is not None and not desk_account_error

    result = {
        "generated_at": datetime.now().isoformat(),
        "trading_account_id": trading_account,
        "account_snapshot": account_snapshot if needs else {},
        "desk_account_error": desk_account_error,
        "pending_in": pending_count(),
        "processed": len(pending),
        "expired_pending_requests": expired_count,
        "skipped": skipped,
        "forced_trade_requests": forced_trade_requests if needs else [],
        "planned_trade_requests": planned_trade_requests if needs else [],
        "analyze_tasks": analyze_tasks if needs else [],
        "needs_hermes": needs,
        "apps_snapshot_keys": list(_latest_apps_snapshot().keys()),
        "apps_snapshot": _latest_apps_snapshot() if analyze_tasks else {},
        "agent_state_path": STATE_PATH,
        "instruction": (
            "若 needs_hermes=false：完全静默，不输出。"
            "若 desk_account_error：一行说明并停止，禁止跨账户用 guard/实盘持仓替代表账户。"
            "若存在 forced_trade_requests：优先逐条输出请示，不得静默吞掉。"
            "若 analyze_tasks 非空：仅依据本任务 trading_account_id 与 account_snapshot 评估仓位/T+1；"
            "若已有 trade_request：优先引用已有 request_id/微信请示，不得重复创建；"
            "propose 必须带该 account_id；BUY/SELL 走 trade_outbox；WAIT 则 close。"
        ),
    }

    _save_agent_state(
        {
            "last_desk_run": result["generated_at"],
            "last_pending": pending_count(),
            "last_analyze_count": len(analyze_tasks),
        }
    )
    return result


def main():
    import argparse

    p = argparse.ArgumentParser(description="Agent Desk v5")
    p.add_argument("--json", action="store_true", help="stdout JSON only")
    p.add_argument("--max", type=int, default=5)
    p.add_argument("--ack-all-skipped", action="store_true", help="dev: ack remaining pending")
    args = p.parse_args()

    if args.ack_all_skipped:
        for ev in list_pending():
            ack(ev.get("event_id", ""), result={"action": "SKIP", "reason": "runtime_clear"})
        print(json.dumps({"cleared": True}, ensure_ascii=False))
        return

    out = process_pending(max_events=args.max)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

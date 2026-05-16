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
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(__file__))

from agent_queue import ack, list_pending, pending_count

PLAYBOOK_DIR = "/config/quant_scripts/data/playbooks"
STATE_PATH = "/config/quant_scripts/data/agent_state.json"


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


def process_pending(*, max_events: int = 5) -> Dict[str, Any]:
    from signal_loop import handle_trigger

    pending = list_pending(limit=max_events)
    skipped: List[dict] = []
    analyze_tasks: List[dict] = []

    for ev in pending:
        eid = ev.get("event_id", "")
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
        action = hr.get("action", "SKIP")

        if action != "ANALYZE":
            ack(eid, result=hr)
            skipped.append({"event_id": eid, "code": code, **hr})
            continue

        task = {
            "event_id": eid,
            "signal_id": sid,
            "code": code,
            "name": ev.get("name", code),
            "reason": ev.get("reason", ""),
            "price": price,
            "change_pct": pct,
            "handle_trigger": hr,
            "quant_context": _fetch_quant_context(code),
            "stock_insights": _stock_insights(code),
            "playbook_patterns": _load_playbook(code),
            "registry_plugins": _run_registry_plugins(code),
        }
        analyze_tasks.append(task)

    result = {
        "generated_at": datetime.now().isoformat(),
        "pending_in": pending_count(),
        "processed": len(pending),
        "skipped": skipped,
        "analyze_tasks": analyze_tasks,
        "needs_hermes": len(analyze_tasks) > 0,
        "apps_snapshot_keys": list(_latest_apps_snapshot().keys()),
        "apps_snapshot": _latest_apps_snapshot() if analyze_tasks else {},
        "agent_state_path": STATE_PATH,
        "instruction": (
            "若 needs_hermes=false：完全静默，不输出。"
            "若 true：对每个 analyze_tasks 跑 TradingAgents+decision_gate；"
            "BUY/SELL 仅输出请示模板；WAIT 调用 signal_loop close WAIT+新信号。"
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
            ack(ev.get("event_id", ""), result={"action": "SKIP", "reason": "manual_clear"})
        print(json.dumps({"cleared": True}, ensure_ascii=False))
        return

    out = process_pending(max_events=args.max)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

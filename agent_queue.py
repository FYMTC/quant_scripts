#!/config/quant_env/bin/python3
"""
agent_queue.py — v5 信号唤醒事件队列（JSONL）

guard 命中 [AGENT_ALERT] → enqueue；Agent Desk 消费 → ack。
同 code 去抖：DEBOUNCE_SEC 内重复事件合并为一条（更新 last_event）。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

QUEUE_PATH = "/config/quant_scripts/data/agent_queue.jsonl"
LOCK_PATH = "/config/quant_scripts/data/agent_desk_wake.lock"
DEBOUNCE_SEC = 45
WAKE_COOLDOWN_SEC = 60


def _ensure_dir():
    os.makedirs(os.path.dirname(QUEUE_PATH), exist_ok=True)


def parse_agent_alert_line(line: str) -> Optional[Dict[str, Any]]:
    """解析 smart_guard 的 [AGENT_ALERT] 行。"""
    if "[AGENT_ALERT]" not in line:
        return None
    body = line.split("[AGENT_ALERT]", 1)[-1].strip()
    parts = body.split("|")
    if len(parts) < 4:
        return {"raw": body, "parse_ok": False}
    try:
        vol_part = parts[6] if len(parts) > 6 else "0"
        vol = float(vol_part.replace("万手", "").strip()) * 10000 if "万" in vol_part else float(vol_part or 0)
    except (ValueError, IndexError):
        vol = 0.0
    price = 0.0
    pct = 0.0
    if len(parts) > 4 and "现价" in parts[4]:
        try:
            price = float(parts[4].replace("现价", "").strip())
        except ValueError:
            pass
    if len(parts) > 5 and "涨" in parts[5]:
        try:
            pct = float(parts[5].replace("涨", "").replace("%", "").strip())
        except ValueError:
            pass
    return {
        "parse_ok": True,
        "signal_id": parts[0].strip(),
        "code": parts[1].strip(),
        "name": parts[2].strip(),
        "reason": parts[3].strip(),
        "price": price,
        "change_pct": pct,
        "volume": vol,
        "raw": body,
    }


def enqueue(
    event: Dict[str, Any],
    *,
    source: str = "smart_guard",
) -> str:
    """写入一条待处理事件，返回 event_id。"""
    _ensure_dir()
    eid = event.get("event_id") or str(uuid.uuid4())[:12]
    row = {
        "event_id": eid,
        "enqueued_at": datetime.now().isoformat(),
        "source": source,
        "status": "pending",
        **event,
    }
    with open(QUEUE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return eid


def enqueue_from_alert_message(msg: str, *, source: str = "smart_guard") -> Optional[str]:
    parsed = parse_agent_alert_line(msg)
    if not parsed:
        return None
    if not parsed.get("parse_ok"):
        return enqueue({"raw_alert": msg, "parse_ok": False}, source=source)
    code = parsed["code"]
    # 去抖：若队列末尾同 code 且未 ack 且在窗口内，更新而非追加
    pending = list_pending(limit=50)
    now = time.time()
    for p in reversed(pending):
        if p.get("code") == code and p.get("status") == "pending":
            try:
                ts = datetime.fromisoformat(p["enqueued_at"]).timestamp()
            except Exception:
                ts = 0
            if now - ts < DEBOUNCE_SEC:
                p.update(parsed)
                p["enqueued_at"] = datetime.now().isoformat()
                p["debounced"] = True
                _rewrite_pending_tail(p, code)
                return p.get("event_id")
    return enqueue(parsed, source=source)


def _rewrite_pending_tail(updated: Dict[str, Any], code: str) -> None:
    """简化：追加新版本并标记旧同 code pending 为 superseded（消费时跳过）。"""
    updated = dict(updated)
    updated["status"] = "pending"
    updated["supersedes_debounce"] = True
    with open(QUEUE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(updated, ensure_ascii=False) + "\n")


def list_pending(limit: int = 100) -> List[Dict[str, Any]]:
    if not os.path.isfile(QUEUE_PATH):
        return []
    rows: List[Dict[str, Any]] = []
    with open(QUEUE_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    acked_ids = {r.get("event_id") for r in rows if r.get("status") == "acked" and r.get("event_id")}
    # 同 code 只保留最新 pending（已 ack 的 event_id 排除）
    latest_by_code: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if r.get("status") != "pending":
            continue
        if r.get("event_id") in acked_ids:
            continue
        code = r.get("code") or r.get("event_id", "")
        latest_by_code[code] = r
    out = list(latest_by_code.values())
    return out[:limit]


def ack(event_id: str, *, result: Optional[Dict[str, Any]] = None) -> None:
    row = {
        "event_id": event_id,
        "acked_at": datetime.now().isoformat(),
        "status": "acked",
        "result": result or {},
    }
    with open(QUEUE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def pending_count() -> int:
    return len(list_pending())


def should_wake_desk() -> bool:
    """唤醒 Agent Desk job 的冷却锁。"""
    if pending_count() == 0:
        return False
    try:
        if os.path.isfile(LOCK_PATH):
            if time.time() - os.path.getmtime(LOCK_PATH) < WAKE_COOLDOWN_SEC:
                return False
    except OSError:
        pass
    try:
        with open(LOCK_PATH, "w") as f:
            f.write(datetime.now().isoformat())
    except OSError:
        return False
    return True


def touch_wake_lock() -> None:
    try:
        with open(LOCK_PATH, "w") as f:
            f.write(datetime.now().isoformat())
    except OSError:
        pass


def clear_pending_mark_acked() -> int:
    """开发/运维：将当前 pending 全部 ack（不写 Hermes）。"""
    n = 0
    for ev in list_pending():
        ack(ev.get("event_id", ""), {"action": "SKIP", "reason": "manual_clear_pending"})
        n += 1
    return n


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="agent_queue CLI")
    ap.add_argument("cmd", choices=["status", "clear", "enqueue-test"], nargs="?")
    args = ap.parse_args()
    cmd = args.cmd or "status"
    if cmd == "status":
        p = list_pending()
        print(json.dumps({"pending": len(p), "events": p}, ensure_ascii=False, indent=2))
    elif cmd == "clear":
        print(json.dumps({"cleared": clear_pending_mark_acked()}, ensure_ascii=False))
    elif cmd == "enqueue-test":
        eid = enqueue_from_alert_message(
            "[AGENT_ALERT] cli_test|000063|中兴|CLI测试|突破|现价38.0|涨+1.0%|量100万手"
        )
        print(json.dumps({"enqueued": eid}, ensure_ascii=False))

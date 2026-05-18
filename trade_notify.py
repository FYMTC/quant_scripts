"""买卖请示/成交结果 — 微信出站队列（主路径：Hermes Agent send_message / 对话回复）。"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from trade_accounts import default_wechat_chat_id, load_registry

DATA = Path(__file__).resolve().parent / "data"
OUTBOX_JSONL = DATA / "trade_wechat_outbox.jsonl"


def enqueue_wechat(body: str, *, kind: str = "trade", meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """记录待推微信正文。正常由 Hermes 在对话里 send_message；jsonl 仅作审计/备用。

    webhook（企业微信机器人）为可选：仅当未走 Hermes、且配置了 WECHAT_WEBHOOK_URL 时才会 curl。
    """
    row = {
        "at": datetime.now().isoformat(),
        "kind": kind,
        "body": body,
        "chat_id": default_wechat_chat_id(),
        "meta": meta or {},
    }
    DATA.mkdir(parents=True, exist_ok=True)
    with OUTBOX_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    webhook = (load_registry().get("wechat") or {}).get("webhook_url") or os.environ.get(
        "WECHAT_WEBHOOK_URL", ""
    )
    if webhook:
        try:
            payload = json.dumps(
                {"msgtype": "markdown", "markdown": {"content": body[:4000]}},
                ensure_ascii=False,
            )
            subprocess.run(
                ["curl", "-s", "-X", "POST", webhook, "-H", "Content-Type: application/json", "-d", payload],
                capture_output=True,
                timeout=10,
                check=False,
            )
            row["webhook_sent"] = True
        except Exception as exc:
            row["webhook_error"] = str(exc)[:200]

    return {"ok": True, "queued": True, "path": str(OUTBOX_JSONL), **row}


def format_execution_result(
    request: Dict[str, Any],
    execution: Dict[str, Any],
    *,
    account_label: str,
) -> str:
    ok = execution.get("ok")
    code = request.get("code")
    name = request.get("name") or code
    direction = request.get("direction")
    rid = request.get("request_id")
    if ok:
        fill = (execution.get("result") or {}).get("data") or {}
        price = fill.get("price") or fill.get("fill_price") or request.get("price")
        shares = fill.get("shares") or request.get("shares")
        src = fill.get("price_source") or ""
        extra = f"\n行情来源: {src}" if src else ""
        return (
            f"【成交回报·{account_label}】\n"
            f"{direction} {name}({code}) 已提交/成交\n"
            f"价{price} 量{shares}股\n"
            f"request_id={rid}{extra}\n"
            f"追溯: {request.get('lineage_id') or '-'}"
        )
    err = execution.get("error") or execution.get("message") or "unknown"
    return (
        f"【执行失败·{account_label}】\n"
        f"{direction} {name}({code})\n"
        f"原因: {str(err)[:400]}\n"
        f"request_id={rid}"
    )

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
HERMES_ENV_PATH = Path("/config/.hermes/.env")
HERMES_SEND_CLI = Path("/config/.hermes/hermes-agent/venv/bin/hermes")


def _load_hermes_env() -> Dict[str, str]:
    env: Dict[str, str] = {}
    if not HERMES_ENV_PATH.is_file():
        return env
    for raw in HERMES_ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def _send_via_native_weixin(body: str, *, chat_id: str) -> Dict[str, Any]:
    env = os.environ.copy()
    env.update(_load_hermes_env())
    env.setdefault("WEIXIN_HOME_CHANNEL", chat_id)

    if not env.get("WEIXIN_TOKEN") or not env.get("WEIXIN_ACCOUNT_ID"):
        return {"ok": False, "skipped": True, "reason": "weixin_credentials_missing"}

    try:
        import sys

        hermes_root = "/config/.hermes/hermes-agent"
        if hermes_root not in sys.path:
            sys.path.insert(0, hermes_root)
        old_env = os.environ.copy()
        os.environ.update(env)
        try:
            from tools.send_message_tool import send_message_tool

            raw = send_message_tool({"target": f"weixin:{chat_id}" if chat_id else "weixin", "message": body[:4000]})
        finally:
            os.environ.clear()
            os.environ.update(old_env)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}

    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        payload = {"raw": str(raw)[:500]}
    if payload.get("success") or payload.get("ok"):
        return {"ok": True, **payload}
    if payload.get("error"):
        return {"ok": False, "error": str(payload.get("error"))[:300]}
    return {"ok": True, **payload}


def enqueue_wechat(body: str, *, kind: str = "trade", meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """记录待推微信正文。正常优先走原生 Hermes Weixin；jsonl 仍保留作审计/备用。"""
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

    native = _send_via_native_weixin(body, chat_id=row["chat_id"])
    row["native_send"] = native
    if native.get("ok"):
        row["native_sent"] = True
        return {"ok": True, "queued": True, "path": str(OUTBOX_JSONL), **row}

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

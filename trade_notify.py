"""买卖请示/成交结果 + 报告 — 企业微信 Webhook 出站队列。

所有通知（报告、交易请求、成交结果）统一通过企业微信 Webhook 发送。
微信（原生 Weixin）仅用于 Hermes 对话交互，不再接收系统通知。"""

from __future__ import annotations

import json
import os
import subprocess
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

_rate_limit_cooldown_until: float = 0.0

from trade_accounts import default_wechat_chat_id, load_registry

RUNTIME_ROOT = Path(os.environ.get("QUANT_RUNTIME_ROOT", "") or ".")
RUNTIME_DATA_DIR = Path(os.environ.get("QUANT_RUNTIME_DATA_DIR", "") or ".")
NOTIFY_MODE = (os.environ.get("QUANT_NOTIFY_MODE", "") or "").strip().lower()
DATA = RUNTIME_DATA_DIR if os.environ.get("QUANT_RUNTIME_DATA_DIR") else Path(__file__).resolve().parent / "data"
OUTBOX_JSONL = (RUNTIME_ROOT / "trade_wechat_outbox.jsonl") if os.environ.get("QUANT_RUNTIME_ROOT") else DATA / "trade_wechat_outbox.jsonl"
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
    global _rate_limit_cooldown_until

    env = os.environ.copy()
    env.update(_load_hermes_env())
    env.setdefault("WEIXIN_HOME_CHANNEL", chat_id)

    if not env.get("WEIXIN_TOKEN") or not env.get("WEIXIN_ACCOUNT_ID"):
        return {"ok": False, "skipped": True, "reason": "weixin_credentials_missing"}

    if _time.monotonic() < _rate_limit_cooldown_until:
        remaining = int(_rate_limit_cooldown_until - _time.monotonic())
        return {"ok": False, "skipped": True, "reason": f"rate_limit_cooldown_{remaining}s"}

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
        _rate_limit_cooldown_until = 0.0
        return {"ok": True, **payload}

    err = str(payload.get("error") or "")
    if "rate limit" in err.lower():
        _rate_limit_cooldown_until = _time.monotonic() + 600
        return {"ok": False, "rate_limited": True, "cooldown_until": int(_rate_limit_cooldown_until), "error": err[:200]}
    return {"ok": False, "error": err[:300]}


def enqueue_wechat(body: str, *, kind: str = "trade", meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """通过企业微信 Webhook 发送通知。jsonl 保留作审计/备用。

    报告 (kind=work_report) 和交易通知 (kind=trade/execution_result) 均走此通道。
    微信原生通道已停用 —— 微信仅用于 Hermes 对话。"""
    text = str(body or "")
    if text.strip().lower() in {"tpl", "buy tpl", "sell tpl", "template", "placeholder"}:
        return {
            "ok": False,
            "queued": False,
            "skipped": True,
            "reason": "placeholder_body_blocked",
            "body": text,
            "kind": kind,
            "meta": meta or {},
        }
    row = {
        "at": datetime.now().isoformat(),
        "kind": kind,
        "body": text,
        "chat_id": default_wechat_chat_id(),
        "meta": meta or {},
    }
    DATA.mkdir(parents=True, exist_ok=True)
    OUTBOX_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with OUTBOX_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    if NOTIFY_MODE == "record-only":
        row["record_only"] = True
        return {"ok": True, "queued": True, "path": str(OUTBOX_JSONL), **row}

    # ── 企业微信 webhook 是唯一通知通道 ──
    # 微信（原生 Weixin）仅用于 Hermes 对话，不接收报告和通知。
    webhook_url = os.environ.get("WECHAT_WEBHOOK_URL", "")
    if not webhook_url:
        hermes_env = _load_hermes_env()
        webhook_url = hermes_env.get("WECHAT_WEBHOOK_URL", "")
    if not webhook_url:
        try:
            webhook_url = (load_registry().get("wechat") or {}).get("webhook_url") or ""
        except Exception:
            pass
    if not webhook_url:
        row["webhook_sent"] = False
        row["webhook_error"] = "webhook_url_missing"
        return {"ok": False, "queued": True, "error": "webhook_url_not_configured", "path": str(OUTBOX_JSONL), **row}

    try:
        payload = json.dumps(
            {"msgtype": "markdown", "markdown": {"content": body[:4000]}},
            ensure_ascii=False,
        )
        wh_result = subprocess.run(
            ["curl", "-s", "-X", "POST", webhook_url, "-H", "Content-Type: application/json", "-d", payload],
            capture_output=True,
            timeout=10,
            check=False,
        )
        row["webhook_sent"] = True
        row["webhook_response"] = wh_result.stdout[:200]
    except Exception as exc:
        row["webhook_error"] = str(exc)[:200]
        return {"ok": False, "queued": True, "error": str(exc)[:300], "path": str(OUTBOX_JSONL), **row}

    return {"ok": True, "queued": True, "path": str(OUTBOX_JSONL), **row}


def format_execution_result(
    request: Dict[str, Any],
    execution: Dict[str, Any],
    *,
    account_label: str,
    portfolio_snapshot: str = "",
) -> str:
    ok = execution.get("ok")
    code = request.get("code")
    name = request.get("name") or code
    direction = request.get("direction")
    direction_cn = "买入" if direction == "BUY" else ("卖出" if direction == "SELL" else direction)
    rid = request.get("request_id")
    price = request.get("price") or 0
    shares = request.get("shares") or 0
    dg = request.get("decision_gate") or {}
    sid = request.get("signal_id") or ""

    # ── source label ──
    source_labels = {
        "morning_plan": "📊 早盘量化候选",
        "de_risk_plan": "🛡 风控减仓计划",
        "rolling_decline": "📉 连续阴跌强减",
        "rapid_drop": "⚡ 急跌强减",
        "price_below": "🔻 破位强减",
    }
    source_label = source_labels.get(sid, "信号决策") if sid else ""

    # ── gate flow ──
    verdict_icons = {"APPROVE": "✅", "MODIFY": "🟡", "REJECT": "❌"}
    verdict = request.get("gate_verdict", dg.get("verdict", "APPROVE"))
    verdict_icon = verdict_icons.get(verdict, "•")

    lines = [
        f"【买卖请示·模拟盘自动成交】{direction_cn} {name}({code})",
        f"",
        f"价¥{price} 量{shares}股 金额¥{price * shares:,.0f}",
        f"门禁裁决: {verdict_icon} {verdict}",
    ]
    if source_label:
        lines.append(f"来源: {source_label}")

    # ── gate details ──
    gates = dg.get("gates") or []
    if gates:
        gate_names = {0: "RG:研究特征", 1: "G1:评分映射", 2: "G2:T+1合规", 3: "G3:风控校验", 4: "G4:仓位评估"}
        lines.append(f"")
        for i, g in enumerate(gates[:5]):
            icon = "✓" if g.get("pass") else "✗"
            name_g = gate_names.get(i, f"G{i}")
            lines.append(f"  {icon} {name_g}: {str(g.get('message',''))[:80]}")
        composite = dg.get("composite_score")
        if composite:
            lines.append(f"  综合评分: {composite}")

    # ── factors ──
    summary = (request.get("gate_summary") or "").strip()
    if summary and summary != verdict and summary != "APPROVE":
        lines.append(f"")
        lines.append(f"因子: {summary[:200]}")

    # ── execution result ──
    if ok:
        fill = (execution.get("result") or {}).get("data") or {}
        actual_price = fill.get("price") or fill.get("fill_price") or price
        actual_shares = fill.get("shares") or shares
        src = fill.get("price_source") or ""
        lines.append(f"")
        lines.append(f"成交: ✅ 已执行 @¥{actual_price}")
        if src:
            lines.append(f"行情来源: {src}")
    else:
        err = execution.get("error") or execution.get("message") or "unknown"
        lines.append(f"")
        lines.append(f"成交: ❌ 失败 — {str(err)[:200]}")

    # ── portfolio snapshot ──
    if portfolio_snapshot:
        lines.append(f"")
        lines.append(f"当前持仓:")
        for line in portfolio_snapshot.split("\n")[:10]:
            lines.append(f"  {line}")

    lines.append(f"")
    lines.append(f"追溯: {request.get('lineage_id') or '-'}")
    lines.append(f"request_id: {rid}")

    return "\n".join(lines)

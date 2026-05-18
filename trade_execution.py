"""多账户成交路由：manual 不下单 / easyths 调 TradeClient。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from trade_accounts import (
    auto_execute_on_resolve,
    easyths_config_path,
    execution_provider,
    get_account,
    should_notify_execution_wechat,
)


def execute_request(
    request: Dict[str, Any],
    *,
    account_id: Optional[str] = None,
    record_kb: bool = True,
) -> Dict[str, Any]:
    """按账户配置执行 pending/resolved 请示。"""
    aid = account_id or request.get("account_id")
    if not aid:
        return {"ok": False, "error": "missing account_id"}

    provider = execution_provider(aid)
    if provider == "none":
        return {
            "ok": True,
            "skipped": True,
            "account_id": aid,
            "message": "manual account: no auto execution",
        }

    if provider == "easyths":
        import ths_trade_executor as ex

        cfg_path = easyths_config_path(aid)
        return ex.execute_from_outbox(
            request["request_id"],
            record_kb=record_kb,
            config_path=cfg_path,
        )

    return {"ok": False, "error": f"unsupported provider: {provider}"}


def after_resolve(
    request: Dict[str, Any],
    outcome: str,
    *,
    note: str = "",
) -> Dict[str, Any]:
    """resolve 之后：按账户自动下单并可选推微信成交回报。"""
    if outcome != "resolved":
        return {"ok": True, "executed": False, "reason": f"outcome={outcome}"}

    aid = request.get("account_id")
    if not aid:
        return {"ok": False, "error": "missing account_id on request"}

    acct = get_account(aid)
    label = acct.get("label") or aid

    if not auto_execute_on_resolve(aid):
        return {
            "ok": True,
            "executed": False,
            "account_id": aid,
            "message": "用户自行在券商成交",
        }

    execution = execute_request(request, account_id=aid)
    result = {
        "ok": bool(execution.get("ok") or execution.get("skipped")),
        "executed": not execution.get("skipped"),
        "account_id": aid,
        "execution": execution,
    }

    if should_notify_execution_wechat(aid):
        from trade_notify import enqueue_wechat, format_execution_result

        body = format_execution_result(request, execution, account_label=label)
        notify = enqueue_wechat(body, kind="execution_result", meta={"request_id": request.get("request_id")})
        result["wechat_notify"] = notify
        result["wechat_body"] = body

    return result

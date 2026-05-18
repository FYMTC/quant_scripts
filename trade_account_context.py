"""按账户加载持仓/资金快照（禁止跨账户混用）。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from trade_accounts import easyths_config_path, get_account

GUARD_CONFIG = Path("/config/quant_scripts/guard_config.json")


def _load_guard_positions() -> Dict[str, Any]:
    if not GUARD_CONFIG.is_file():
        return {}
    try:
        with GUARD_CONFIG.open(encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("positions") or {}
    except Exception:
        return {}


def _snapshot_from_guard(account_id: str, label: str) -> Dict[str, Any]:
    positions = _load_guard_positions()
    rows = []
    for code, info in positions.items():
        if not isinstance(info, dict):
            continue
        rows.append(
            {
                "code": code,
                "name": info.get("name", code),
                "shares": info.get("shares") or info.get("quantity") or 0,
                "cost": info.get("cost") or info.get("cost_price"),
                "market_value": info.get("market_value"),
            }
        )
    return {
        "account_id": account_id,
        "account_label": label,
        "position_source": "guard_config",
        "positions": rows,
        "position_count": len(rows),
        "note": "与 smart_guard / trade_log 绑定的微信实盘视图",
    }


def _snapshot_from_stock_kb(account_id: str, label: str) -> Dict[str, Any]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from stock_kb import StockKB

        truth = StockKB().read_portfolio_truth()
    except Exception as exc:
        return {
            "account_id": account_id,
            "account_label": label,
            "position_source": "stock_kb",
            "error": str(exc)[:300],
            "positions": [],
        }
    pos_in = truth.get("positions") or {}
    rows = []
    for code, info in pos_in.items():
        if not isinstance(info, dict):
            continue
        rows.append(
            {
                "code": code,
                "name": info.get("name", code),
                "shares": info.get("shares") or info.get("current_shares") or 0,
                "cost": info.get("cost") or info.get("avg_cost"),
                "market_value": info.get("market_value"),
            }
        )
    return {
        "account_id": account_id,
        "account_label": label,
        "position_source": "stock_kb",
        "cash": truth.get("cash"),
        "total_value": truth.get("total_value"),
        "positions": rows,
        "position_count": len(rows),
    }


def _snapshot_from_easyths(account_id: str, label: str) -> Dict[str, Any]:
    import ths_trade_executor as ex

    cfg = ex.load_trade_config(easyths_config_path(account_id))
    client = ex.build_client(cfg)
    ex.verify_server_mode(client, cfg.get("expected_mode", ""))
    resp = client.query_holdings()
    data = (resp or {}).get("data") or {}
    holdings = data.get("holdings") or data.get("positions") or []
    if isinstance(holdings, dict):
        holdings = list(holdings.values())
    rows = []
    for h in holdings:
        if not isinstance(h, dict):
            continue
        code = h.get("stock_code") or h.get("code") or ""
        rows.append(
            {
                "code": code,
                "name": h.get("stock_name") or h.get("name") or code,
                "shares": h.get("quantity") or h.get("shares") or 0,
                "cost": h.get("cost_price") or h.get("cost"),
                "last_price": h.get("last_price") or h.get("price"),
                "market_value": h.get("market_value"),
                "profit": h.get("profit"),
            }
        )
    return {
        "account_id": account_id,
        "account_label": label,
        "position_source": "easyths",
        "mode": cfg.get("expected_mode"),
        "summary": data.get("summary") or {},
        "positions": rows,
        "position_count": len(rows),
    }


def load_account_snapshot(account_id: str) -> Dict[str, Any]:
    """返回该账户专属持仓视图；决策/请示必须只引用此快照。"""
    acct = get_account(account_id)
    label = acct.get("label") or account_id
    src = (acct.get("position_source") or "guard_config").lower()
    if src in ("easyths", "easyths_paper", "paper"):
        return _snapshot_from_easyths(account_id, label)
    if src in ("stock_kb", "stock_kb_guard"):
        snap = _snapshot_from_stock_kb(account_id, label)
        if not snap.get("positions") and _load_guard_positions():
            snap["guard_fallback"] = _snapshot_from_guard(account_id, label)
        return snap
    return _snapshot_from_guard(account_id, label)


def format_snapshot_brief(snap: Dict[str, Any], *, max_positions: int = 8) -> str:
    lines = [
        f"账户 {snap.get('account_label')} ({snap.get('account_id')})",
        f"持仓源: {snap.get('position_source')} · 共 {snap.get('position_count', 0)} 只",
    ]
    if snap.get("error"):
        lines.append(f"⚠️ {snap['error']}")
    if snap.get("cash") is not None:
        lines.append(f"现金: {snap.get('cash')}")
    if snap.get("total_value") is not None:
        lines.append(f"总资产: {snap.get('total_value')}")
    for p in (snap.get("positions") or [])[:max_positions]:
        lines.append(
            f"  {p.get('code')} {p.get('name')} {p.get('shares')}股 "
            f"成本{p.get('cost')} 市值{p.get('market_value')}"
        )
    extra = (snap.get("position_count") or 0) - max_positions
    if extra > 0:
        lines.append(f"  …另有 {extra} 只")
    return "\n".join(lines)

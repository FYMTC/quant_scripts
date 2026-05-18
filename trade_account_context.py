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


def _coerce_shares(val: Any) -> int:
    if val is None:
        return 0
    try:
        return int(float(str(val).replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0


def _holdings_list_from_query_data(raw: Any) -> tuple[list, Dict[str, Any]]:
    """EasyTHS holding_query：paper 常为 list；live 常为 dict 含 holdings。"""
    if isinstance(raw, list):
        return raw, {}
    if isinstance(raw, dict):
        return (
            raw.get("holdings") or raw.get("positions") or [],
            raw.get("summary") or {},
        )
    return [], {}


def _snapshot_from_easyths(account_id: str, label: str) -> Dict[str, Any]:
    import ths_trade_executor as ex

    cfg = ex.load_trade_config(easyths_config_path(account_id))
    client = ex.build_client(cfg)
    mode_data = ex.verify_server_mode(client, cfg.get("expected_mode", ""))
    paper_meta = (mode_data or {}).get("paper") or {}

    resp = client.query_holdings()
    raw = (resp or {}).get("data")
    holdings, summary = _holdings_list_from_query_data(raw)

    rows = []
    for h in holdings:
        if not isinstance(h, dict):
            continue
        code = (
            h.get("stock_code")
            or h.get("code")
            or h.get("证券代码")
            or ""
        )
        name = (
            h.get("stock_name")
            or h.get("name")
            or h.get("证券名称")
            or code
        )
        shares = _coerce_shares(
            h.get("quantity")
            or h.get("shares")
            or h.get("股票余额")
            or h.get("可用余额")
        )
        cost_raw = h.get("cost_price") or h.get("cost") or h.get("成本价")
        try:
            cost = float(str(cost_raw).replace(",", "")) if cost_raw is not None else None
        except (TypeError, ValueError):
            cost = None
        last_raw = h.get("last_price") or h.get("price") or h.get("市价")
        try:
            last_price = float(str(last_raw).replace(",", "")) if last_raw is not None else None
        except (TypeError, ValueError):
            last_price = None
        mv_raw = h.get("market_value") or h.get("市值")
        try:
            market_value = float(str(mv_raw).replace(",", "")) if mv_raw is not None else None
        except (TypeError, ValueError):
            market_value = None
        profit_raw = h.get("profit") or h.get("盈亏")
        try:
            profit = float(str(profit_raw).replace(",", "")) if profit_raw is not None else None
        except (TypeError, ValueError):
            profit = None
        rows.append(
            {
                "code": str(code).strip(),
                "name": name,
                "shares": shares,
                "cost": cost,
                "last_price": last_price,
                "market_value": market_value,
                "profit": profit,
            }
        )

    initial_cash = paper_meta.get("initial_cash")
    cash = paper_meta.get("cash")
    total_value: Optional[float] = None
    try:
        fr = client.query_funds()
        fd = (fr or {}).get("data")
        if isinstance(fd, dict) and fd.get("总资产") is not None:
            total_value = float(str(fd["总资产"]).replace(",", ""))
    except Exception:
        if cash is not None and not rows:
            try:
                total_value = float(cash)
            except (TypeError, ValueError):
                total_value = None

    return {
        "account_id": account_id,
        "account_label": label,
        "position_source": "easyths",
        "mode": cfg.get("expected_mode"),
        "summary": summary,
        "initial_cash": initial_cash,
        "cash": cash,
        "total_value": total_value,
        "positions": rows,
        "position_count": len(rows),
    }


def load_account_snapshot(account_id: str) -> Dict[str, Any]:
    """返回该账户专属持仓视图；决策/请示必须只引用此快照。"""
    acct = get_account(account_id)
    label = acct.get("label") or account_id
    src = (acct.get("position_source") or "guard_config").lower()
    if src in ("easyths", "easyths_paper", "paper"):
        try:
            return _snapshot_from_easyths(account_id, label)
        except Exception as exc:
            # 失败时仍返回结构化 dict，避免调用方把「异常无输出」误判成空仓
            return {
                "account_id": account_id,
                "account_label": label,
                "position_source": "easyths",
                "error": str(exc)[:500],
                "positions": [],
                "position_count": 0,
            }
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
    if snap.get("initial_cash") is not None:
        lines.append(f"初始资金: {snap.get('initial_cash')}")
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

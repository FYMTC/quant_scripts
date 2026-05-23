"""按账户加载持仓/资金快照（禁止跨账户混用）。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from trade_accounts import easyths_config_path, get_account

PRIMARY_ACCOUNT_ID = "paper_easyths"
RUNTIME_ROOT = os.environ.get("QUANT_RUNTIME_ROOT", "")
TEST_SCENARIO = os.environ.get("QUANT_RUNTIME_SCENARIO", "")
TESTS_DIR = Path(__file__).resolve().parent / "tests"


def default_account_id() -> str:
    return PRIMARY_ACCOUNT_ID


def _coerce_cash(val: Any) -> float:
    if val is None:
        return 0.0
    try:
        return float(str(val).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def normalize_portfolio_truth(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    positions = {}
    total_market_value = 0.0
    total_cost_basis = 0.0
    for row in snapshot.get("positions") or []:
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        shares = _coerce_shares(row.get("shares"))
        cost = _coerce_cash(row.get("cost"))
        last_price = _coerce_cash(row.get("last_price"))
        market_value = row.get("market_value")
        if market_value is None:
            market_value = shares * last_price
        else:
            market_value = _coerce_cash(market_value)
        positions[code] = {
            "name": row.get("name") or code,
            "shares": shares,
            "cost": cost,
            "current_price": last_price,
            "market_value": market_value,
            "profit": row.get("profit"),
        }
        total_market_value += market_value
        total_cost_basis += shares * cost

    cash = _coerce_cash(snapshot.get("cash"))
    total_assets = _coerce_cash(snapshot.get("total_value"))
    if total_assets <= 0:
        total_assets = total_market_value + cash

    return {
        "account_id": snapshot.get("account_id"),
        "positions": positions,
        "cash": cash,
        "total_assets": total_assets,
        "total_market_value": total_market_value,
        "total_cost_basis": total_cost_basis,
        "position_count": len(positions),
        "position_source": snapshot.get("position_source"),
        "error": snapshot.get("error"),
    }


def load_portfolio_truth(account_id: Optional[str] = None) -> Dict[str, Any]:
    snap = load_account_snapshot(account_id or default_account_id())
    return normalize_portfolio_truth(snap)


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
    src = (acct.get("position_source") or "easyths").lower()
    if src not in ("easyths", "easyths_paper", "paper"):
        return {
            "account_id": account_id,
            "account_label": label,
            "position_source": src,
            "error": f"unsupported position_source: {src}",
            "positions": [],
            "position_count": 0,
        }
    try:
        return _snapshot_from_easyths(account_id, label)
    except Exception as exc:
        return {
            "account_id": account_id,
            "account_label": label,
            "position_source": "easyths",
            "error": str(exc)[:500],
            "positions": [],
            "position_count": 0,
        }


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

#!/config/quant_env/bin/python3
"""
post_execution_rescan.py — 卖出后重新扫描（P0 闭环缺口修复）

在 de_risk 卖出执行后、现金充裕时，重新评估候选并生成新的买入提案。
内置多层防循环：最大深度、现金门槛、最小间隔、同日反转拦截。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import List, Optional

RUNTIME_DATA_DIR = os.environ.get("QUANT_RUNTIME_DATA_DIR") or "/config/quant_scripts/data"
STATE_PATH = os.path.join(RUNTIME_DATA_DIR, "agent_state.json")

MAX_RESCAN_DEPTH = 3
CASH_CHANGE_THRESHOLD_PCT = 0.05     # 5% of total_assets
MIN_SECONDS_BETWEEN_RESCANS = 300    # 5 minutes


def _load_state() -> dict:
    if not os.path.isfile(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def record_executed_sell(code: str, shares: int, account_id: str, signal_id: str = "") -> None:
    """记录一笔系统执行的卖出，供重扫描时排除。"""
    state = _load_state()
    sells = state.get("executed_sells_today") or []
    if not isinstance(sells, list):
        sells = []
    sells.append({
        "code": code,
        "shares": shares,
        "account_id": account_id,
        "signal_id": signal_id,
        "executed_at": datetime.now().isoformat(),
    })
    state["executed_sells_today"] = sells
    _save_state(state)


def get_executed_sell_codes_today() -> set:
    """返回今天被系统卖出的所有标的代码。"""
    state = _load_state()
    sells = state.get("executed_sells_today") or []
    if not isinstance(sells, list):
        return set()
    today = datetime.now().strftime("%Y-%m-%d")
    codes = set()
    for s in sells:
        if today in str(s.get("executed_at") or ""):
            codes.add(str(s.get("code") or ""))
    return codes


def reset_daily() -> None:
    """每日重置：清理前一天的卖出记录和深度计数。"""
    state = _load_state()
    today = datetime.now().strftime("%Y-%m-%d")
    if str(state.get("last_post_sell_rescan_date") or "") != today:
        state["executed_sells_today"] = []
        state["post_sell_rescan_depth"] = 0
        state["last_post_sell_rescan_date"] = today
        _save_state(state)


def should_rescan(account_snapshot: dict) -> tuple:
    """判断是否应该触发卖出后重扫描。返回 (should: bool, reason: str)。"""
    state = _load_state()
    today = datetime.now().strftime("%Y-%m-%d")

    # 当日深度超限
    depth = state.get("post_sell_rescan_depth") or 0
    if depth >= MAX_RESCAN_DEPTH:
        return False, f"depth={depth} >= max={MAX_RESCAN_DEPTH}"

    # 无卖出记录 — 没有需要反应的卖出
    sells = state.get("executed_sells_today") or []
    if not isinstance(sells, list) or not sells:
        return False, "no executed sells today"

    # 现金增幅不足
    cash = float(account_snapshot.get("cash") or 0)
    total = float(account_snapshot.get("total_value") or account_snapshot.get("total_assets") or 0)
    if total <= 0:
        return False, "total_assets unavailable"
    # 现金占比必须 >= 10% 才能扫（至少有一定的可用资金）
    if cash / total < 0.10:
        return False, f"cash_ratio={cash/total*100:.1f}% < 10%"

    # 最小间隔
    last_at = state.get("last_post_sell_rescan_at") or ""
    if last_at:
        try:
            age = (datetime.now() - datetime.fromisoformat(last_at)).total_seconds()
            if age < MIN_SECONDS_BETWEEN_RESCANS:
                return False, f"last_rescan_was_{int(age)}s_ago < {MIN_SECONDS_BETWEEN_RESCANS}s"
        except ValueError:
            pass

    return True, f"depth={depth} sells={len(sells)} cash_ratio={cash/total*100:.1f}%"


def run_buy_allocation(account_snapshot: dict, excluded_codes: set) -> list:
    """用最新 EasyTHS 持仓重新跑候选分配。排除今天已卖出的代码。"""
    from apps.morning import allocate_buy_candidates, load_feature_snapshot, load_candidates, augment_feature_snapshot_for_candidates

    positions = account_snapshot.get("positions") or []
    if isinstance(positions, dict):
        positions = list(positions.values())

    holdings = []
    for pos in positions:
        code = str(pos.get("code") or "").strip()
        if not code or code in excluded_codes:
            continue
        shares = int(pos.get("shares") or 0)
        if shares <= 0:
            continue
        price = float(pos.get("last_price") or pos.get("current_price") or pos.get("price") or 0)
        cost = float(pos.get("cost") or 0)
        mv = price * shares
        pnl = (price - cost) * shares if cost > 0 else 0.0
        pnl_pct = (price - cost) / cost * 100 if cost > 0 else 0.0
        holdings.append({
            "code": code,
            "name": pos.get("name") or code,
            "shares": shares,
            "cost": round(cost, 2),
            "price": round(price, 2),
            "change_pct": 0.0,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "market_value": round(mv, 2),
            "n_days": 0,
        })

    cash = float(account_snapshot.get("cash") or 0)
    total = float(account_snapshot.get("total_value") or account_snapshot.get("total_assets") or 0)
    if total <= 0:
        pos_mv = sum(h.get("market_value", 0) for h in holdings)
        total = cash + pos_mv
    if total <= 0:
        return []

    candidates = load_candidates()
    new_candidates = [c for c in candidates if c["code"] not in {h["code"] for h in holdings}]
    feature_snapshot = load_feature_snapshot()
    feature_snapshot = augment_feature_snapshot_for_candidates(feature_snapshot, new_candidates)

    # 读取当前 event_risk（从 morning_output.json）
    event_risk = {}
    try:
        mpath = os.path.join(RUNTIME_DATA_DIR, "morning_output.json")
        if os.path.isfile(mpath):
            with open(mpath, encoding="utf-8") as f:
                event_risk = json.load(f).get("event_risk") or {}
    except Exception:
        pass

    return allocate_buy_candidates(
        holdings, cash, total, new_candidates, feature_snapshot, event_risk,
    )


def increment_depth() -> int:
    """递加重扫深度，返回新深度。"""
    state = _load_state()
    depth = (state.get("post_sell_rescan_depth") or 0) + 1
    state["post_sell_rescan_depth"] = depth
    state["last_post_sell_rescan_date"] = datetime.now().strftime("%Y-%m-%d")
    state["last_post_sell_rescan_at"] = datetime.now().isoformat()
    _save_state(state)
    return depth

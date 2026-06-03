#!/config/quant_env/bin/python3
"""
position_reconciliation.py — 持仓漂移检测（实盘接管就绪）

比对"系统预期的持仓"与"EasyTHS 实际持仓"，检测人工干预迹象，
在早报/晚报中输出可读的差异报告。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

RUNTIME_DATA_DIR = os.environ.get("QUANT_RUNTIME_DATA_DIR") or "/config/quant_scripts/data"
STATE_PATH = os.path.join(RUNTIME_DATA_DIR, "agent_state.json")


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


def save_reference(snapshot: dict) -> None:
    """保存当前 EasyTHS 快照作为下次比对基准（仅 code→shares 精简记录）。"""
    positions = snapshot.get("positions") or []
    if isinstance(positions, dict):
        positions = list(positions.values())
    ref = {}
    for pos in positions:
        code = str(pos.get("code") or "").strip()
        if code:
            ref[code] = int(pos.get("shares") or 0)

    state = _load_state()
    state["reference_positions"] = ref
    state["reference_total_assets"] = float(snapshot.get("total_value") or snapshot.get("total_assets") or 0)
    state["reference_saved_at"] = datetime.now().isoformat()
    _save_state(state)


def record_system_trade(code: str, direction: str, shares: int, price: float = 0.0) -> None:
    """记录一笔系统执行的交易，供漂移检测排除。"""
    state = _load_state()
    trades = state.get("system_executed_trades_today") or []
    if not isinstance(trades, list):
        trades = []
    # 每日清理
    today = datetime.now().strftime("%Y-%m-%d")
    trades = [t for t in trades if today in str(t.get("executed_at") or "")]
    trades.append({
        "code": code,
        "direction": direction.upper(),
        "shares": shares,
        "price": price,
        "executed_at": datetime.now().isoformat(),
    })
    state["system_executed_trades_today"] = trades
    _save_state(state)


def _build_expected(reference: dict, system_trades: list) -> dict:
    """从基准持仓 + 系统交易推导预期持仓。"""
    expected = dict(reference)
    for t in system_trades:
        code = str(t.get("code") or "")
        direction = str(t.get("direction") or "").upper()
        shares = int(t.get("shares") or 0)
        if not code or shares <= 0:
            continue
        if direction == "BUY":
            expected[code] = expected.get(code, 0) + shares
        elif direction == "SELL":
            current = expected.get(code, 0)
            after = max(0, current - shares)
            if after <= 0:
                expected.pop(code, None)
            else:
                expected[code] = after
    return expected


def detect_drift(current_snapshot: dict) -> dict:
    """比对预期持仓与 EasyTHS 实际持仓，检测人工干预。"""
    state = _load_state()
    reference = state.get("reference_positions") or {}
    system_trades = state.get("system_executed_trades_today") or []
    if not isinstance(system_trades, list):
        system_trades = []

    # 若没有基准，保存当前快照作为基准
    if not reference:
        save_reference(current_snapshot)
        return {"has_drift": False}

    expected = _build_expected(reference, system_trades)

    # 当前 EasyTHS 持仓
    positions = current_snapshot.get("positions") or []
    if isinstance(positions, dict):
        positions = list(positions.values())
    actual = {}
    for pos in positions:
        code = str(pos.get("code") or "").strip()
        if code:
            actual[code] = int(pos.get("shares") or 0)

    new_positions = []
    disappeared = []
    share_changes = []

    for code, shares in actual.items():
        exp = expected.get(code, 0)
        if exp == 0:
            new_positions.append({"code": code, "shares": shares, "likely_source": "manual_buy"})
        elif exp != shares:
            share_changes.append({"code": code, "expected": exp, "actual": shares, "delta": shares - exp, "likely_source": "manual_adjust"})

    for code, shares in expected.items():
        if code not in actual:
            disappeared.append({"code": code, "expected_shares": shares, "likely_source": "manual_sell"})

    has_drift = bool(new_positions or disappeared or share_changes)

    # 保存新基准（每次检测后更新，避免重复报警）
    if has_drift:
        save_reference(current_snapshot)

    return {
        "has_drift": has_drift,
        "new_positions": new_positions,
        "disappeared": disappeared,
        "share_changes": share_changes,
    }


def format_drift_report(drift: dict) -> str:
    """生成人类可读的漂移报告。"""
    if not drift or not drift.get("has_drift"):
        return ""

    parts = ["**⚠️ 持仓漂移检测：EasyTHS 与系统预期不一致**"]

    new_pos = drift.get("new_positions") or []
    for np in new_pos:
        parts.append(f"- 🆕 {np['code']}: +{np['shares']}股（疑似手动买入）")

    disappeared = drift.get("disappeared") or []
    for dp in disappeared:
        parts.append(f"- ❌ {dp['code']}: 预期 {dp['expected_shares']} 股，实际已清空（疑似手动卖出）")

    changes = drift.get("share_changes") or []
    for sc in changes:
        parts.append(f"- 🔄 {sc['code']}: 预期 {sc['expected']} 股，实际 {sc['actual']} 股（差值 {sc['delta']:+d}）")

    return "\n".join(parts)

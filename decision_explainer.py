#!/usr/local/bin/python3
"""反事实解释：把 gate / constraints 结果转换成结构化“还差什么”。"""

from __future__ import annotations

from typing import Any, Dict, List

from core.constraints import CVAR_BLOCK_NEW_OPEN, MAX_SINGLE_POSITION_RATIO_ADD


BUY_SCORE_THRESHOLD = 0.8


def _factor(rule: str, message: str, *, current: Any = None, required: Any = None, delta: Any = None) -> dict:
    return {
        "rule": rule,
        "current": current,
        "required": required,
        "delta": delta,
        "message": message,
    }


def build_counterfactual_from_gate(gate_result: dict) -> dict:
    gate_result = gate_result or {}
    verdict = gate_result.get("verdict") or "UNKNOWN"
    target_action = gate_result.get("direction") or gate_result.get("mapped_action") or "HOLD"
    blocking_factors: List[dict] = []

    research_gate = gate_result.get("research_gate") or {}
    composite = gate_result.get("composite_score")
    suggested_shares = gate_result.get("suggested_shares")

    if research_gate and not research_gate.get("pass", True):
        msg = research_gate.get("message") or "research gate blocked"
        if "缺少 research_features" in msg:
            blocking_factors.append(_factor("research_features", msg, current="missing", required="present"))
        elif "feature snapshot 不新鲜" in msg:
            blocking_factors.append(_factor("feature_fresh", msg, current=False, required=True))
        elif "risk_level=danger" in msg:
            blocking_factors.append(_factor("risk_level", msg, current="danger", required="safe|warning"))
        elif "market_regime=bear" in msg:
            blocking_factors.append(_factor("market_regime", msg, current="bear", required="sideways|bull"))
        elif "cvar=" in msg:
            blocking_factors.append(
                _factor(
                    "cvar",
                    msg,
                    current=msg.split("cvar=")[-1].split("%", 1)[0] + "%",
                    required=f"> {CVAR_BLOCK_NEW_OPEN * 100:.0f}%",
                )
            )
        else:
            blocking_factors.append(_factor("research_gate", msg))

    if composite is not None and target_action in ("BUY", "OVERWEIGHT") and composite < BUY_SCORE_THRESHOLD:
        blocking_factors.append(
            _factor(
                "composite_score",
                f"综合评分 {composite:+.2f} 低于 BUY 阈值 {BUY_SCORE_THRESHOLD:+.2f}",
                current=round(composite, 2),
                required=BUY_SCORE_THRESHOLD,
                delta=round(BUY_SCORE_THRESHOLD - composite, 2),
            )
        )

    for reason in gate_result.get("reasons") or []:
        if "T+1" in reason:
            blocking_factors.append(_factor("t_plus_one", reason, current="locked", required="next trading day"))
        elif "风控拒绝" in reason:
            blocking_factors.append(_factor("risk_check", reason))
        elif "仓位评估风险" in reason:
            blocking_factors.append(_factor("position_sizer", reason, current=suggested_shares, required=0))

    next_best_action = "HOLD"
    if target_action in ("SELL", "UNDERWEIGHT"):
        next_best_action = "WAIT"
    elif verdict == "APPROVE":
        next_best_action = target_action

    confidence = "high" if blocking_factors else "low"
    summary = (
        f"当前可执行 {target_action}。"
        if not blocking_factors
        else f"当前不能直接执行 {target_action}；需先解决 {len(blocking_factors)} 个阻塞因子。"
    )

    return {
        "summary": summary,
        "target_action": target_action,
        "current_verdict": verdict,
        "blocking_factors": blocking_factors,
        "next_best_action": next_best_action,
        "confidence": confidence,
    }


def build_counterfactual_from_constraints(rows: list) -> dict:
    rows = rows or []
    blocking_factors: List[dict] = []

    for row in rows:
        if isinstance(row, dict):
            check = row.get("check")
            passed = row.get("pass", True)
            message = row.get("message", "")
        else:
            check, passed, message = row
        if passed:
            continue
        if check == "cash_buffer":
            blocking_factors.append(_factor(check, message, required=">= 5% cash buffer"))
        elif check == "position_limit":
            blocking_factors.append(_factor(check, message, required=f"<= {MAX_SINGLE_POSITION_RATIO_ADD:.0%}"))
        elif check == "cvar_block":
            blocking_factors.append(_factor(check, message, required=f"> {CVAR_BLOCK_NEW_OPEN * 100:.0f}%"))
        elif check == "market_regime":
            blocking_factors.append(_factor(check, message, current="bear", required="sideways|bull"))
        elif check == "macro_event":
            blocking_factors.append(_factor(check, message, required="event_level below HIGH and allow_new_buy=true"))
        elif check == "macro_de_risk":
            blocking_factors.append(_factor(check, message, required="gross exposure within CRITICAL limit"))
        elif check == "feature_snapshot":
            blocking_factors.append(_factor(check, message, required="fresh feature snapshot"))
        else:
            blocking_factors.append(_factor(check or "unknown", message))

    return {
        "summary": "硬约束全部通过。" if not blocking_factors else f"硬约束存在 {len(blocking_factors)} 个阻塞因子。",
        "target_action": "BUY",
        "current_verdict": "ALLOWED" if not blocking_factors else "BLOCKED",
        "blocking_factors": blocking_factors,
        "next_best_action": "READY" if not blocking_factors else "HOLD",
        "confidence": "high" if blocking_factors else "low",
    }

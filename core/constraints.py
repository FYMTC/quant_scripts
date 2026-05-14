#!/config/quant_env/bin/python3
"""
core/constraints.py — 硬约束层（代码强制，不可被 LLM 覆盖）

与 hermes-quant-software-spec.md v2.0「五、硬约束可测试性」及 system-architecture Layer1 对齐。

阈值与 TODO / 架构文档一致：
- 单标仓位 > 30% → 禁止加仓
- CVaR < -5%（更负）→ 禁止该标的新开仓
- T+1 当日买入 → 禁止卖出
- 可用资金不足 → 禁止买入
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

# ========== 阈值（与 spec / TODO 一致，后续可迁 shared/config）==========

# 日频 CVaR（如历史模拟）为负值表示尾部损失；更负 = 更差
CVAR_BLOCK_NEW_OPEN = -0.05  # 新开仓：CVaR 劣于此值则拒绝

MAX_SINGLE_POSITION_RATIO_ADD = 0.30  # 加仓：当前单标占组合市值比例超过则拒绝

MAX_ORDER_TO_AVAILABLE_RATIO = 1.0  # 买单金额不得超过可用现金（含等于边界）


class ConstraintVerdict(str, Enum):
    ALLOWED = "ALLOWED"
    BLOCKED = "BLOCKED"
    PLAN_ONLY = "PLAN_ONLY"  # 非交易时段：仅允许计划单语义（由调用方解释）


@dataclass
class ConstraintResult:
    """单次检查结果（便于日志与 JSON 输出）。"""

    verdict: ConstraintVerdict
    rule: str
    message: str
    code: str = ""

    def blocked(self) -> bool:
        return self.verdict == ConstraintVerdict.BLOCKED


# ---- 规格书伪代码对应的四个原子检查 ----


def check_new_position(code: str, cvar: Optional[float]) -> ConstraintVerdict:
    """
    新开仓：标的组合 CVaR 已劣于 -5% 则禁止继续在该标的上新开/加仓方向暴露。

    cvar: 该标的或组合层面的日 CVaR（负值），None 表示未知 → 不据此拦截。
    """
    if cvar is None:
        return ConstraintVerdict.ALLOWED
    if cvar < CVAR_BLOCK_NEW_OPEN:
        return ConstraintVerdict.BLOCKED
    return ConstraintVerdict.ALLOWED


def check_add_position(code: str, current_ratio: float) -> ConstraintVerdict:
    """加仓：当前单标市值占 **总资产** 比例已超过 30% 则禁止再加。"""
    _ = code
    if current_ratio > MAX_SINGLE_POSITION_RATIO_ADD:
        return ConstraintVerdict.BLOCKED
    return ConstraintVerdict.ALLOWED


def check_sell(code: str, bought_today: bool) -> ConstraintVerdict:
    """卖出：T+1 当日买入的标的禁止卖出。"""
    _ = code
    if bought_today:
        return ConstraintVerdict.BLOCKED
    return ConstraintVerdict.ALLOWED


def check_buy(available: float, order_value: float) -> ConstraintVerdict:
    """买入：订单金额超过可用现金则拒绝。"""
    if order_value > available * MAX_ORDER_TO_AVAILABLE_RATIO:
        return ConstraintVerdict.BLOCKED
    return ConstraintVerdict.ALLOWED


# ---- 组合评估（供 apps / decision_gate 调用）----


def evaluate_buy(
    code: str,
    *,
    order_value: float,
    available_cash: float,
    cvar: Optional[float] = None,
    current_single_ratio: Optional[float] = None,
) -> ConstraintResult:
    """
    买入路径上的硬约束串联。
    current_single_ratio: 当前该标占组合总市值比例；未知则不拦截加仓比例。
    """
    reasons: List[str] = []

    r = check_buy(available_cash, order_value)
    if r == ConstraintVerdict.BLOCKED:
        reasons.append(f"资金不足: 需要{order_value:.2f} 可用{available_cash:.2f}")

    r = check_new_position(code, cvar)
    if r == ConstraintVerdict.BLOCKED:
        reasons.append(f"CVaR{cvar:.4f} 劣于阈值 {CVAR_BLOCK_NEW_OPEN}，禁止新开/加仓")

    if current_single_ratio is not None:
        r = check_add_position(code, current_single_ratio)
        if r == ConstraintVerdict.BLOCKED:
            reasons.append(
                f"单标仓位 {current_single_ratio:.1%} 已超过上限 {MAX_SINGLE_POSITION_RATIO_ADD:.0%}"
            )

    if reasons:
        return ConstraintResult(
            verdict=ConstraintVerdict.BLOCKED,
            rule="evaluate_buy",
            message="; ".join(reasons),
            code=code,
        )
    return ConstraintResult(
        verdict=ConstraintVerdict.ALLOWED,
        rule="evaluate_buy",
        message="OK",
        code=code,
    )


def evaluate_sell(code: str, *, bought_today: bool) -> ConstraintResult:
    r = check_sell(code, bought_today)
    if r == ConstraintVerdict.BLOCKED:
        return ConstraintResult(
            verdict=ConstraintVerdict.BLOCKED,
            rule="t_plus_one",
            message="T+1 锁定：当日买入不可卖",
            code=code,
        )
    return ConstraintResult(
        verdict=ConstraintVerdict.ALLOWED, rule="evaluate_sell", message="OK", code=code
    )


@dataclass
class GateSnapshot:
    """可选：供 morning pipeline JSON 嵌入 constraints 块。"""

    allowed: bool
    details: List[ConstraintResult] = field(default_factory=list)


def snapshot_for_holdings(
    *,
    worst_cvar: Optional[float] = None,
) -> GateSnapshot:
    """无买卖意向时的只读快照（例如仅展示是否允许新开仓）。"""
    details: List[ConstraintResult] = []
    if worst_cvar is not None and worst_cvar < CVAR_BLOCK_NEW_OPEN:
        details.append(
            ConstraintResult(
                ConstraintVerdict.BLOCKED,
                "cvar_portfolio",
                f"组合最差 CVaR {worst_cvar:.4f} 已劣于 {CVAR_BLOCK_NEW_OPEN}",
            )
        )
    allowed = not any(d.blocked() for d in details)
    return GateSnapshot(allowed=allowed, details=details)

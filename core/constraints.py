#!python3
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
from typing import List, Optional, Tuple

# check_all 返回 (check_id, pass, message)，与 apps/morning.py 契约一致

# ========== 阈值（从 deployment_tiers.json 热加载，不再硬编码）==========
def _load_cvar_floor() -> float:
    """从 deployment_tiers.json 读取当前档位的 cvar_floor，返回小数形式。"""
    import json, os
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "deployment_tiers.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        tiers = cfg.get("tiers") or {}
        tier_name = cfg.get("default_tier", "WATCH")
        tier = tiers.get(tier_name, tiers.get("WATCH", {}))
        cvar_pct = float(tier.get("cvar_floor", -10))
        return cvar_pct / 100.0  # 百分数 → 小数
    except Exception:
        return -0.10

CVAR_BLOCK_NEW_OPEN = _load_cvar_floor()  # 新开仓：CVaR 劣于此值则拒绝（v5.13: deployment_tiers 驱动）

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


def check_all(
    holdings: List[dict],
    cash: float,
    total_assets: float,
    quant: dict,
    feature_snapshot: Optional[dict] = None,
) -> List[Tuple[str, bool, str]]:
    """
    盘前/组合级硬约束扫描（供 apps/morning.py 等调用）。

    quant["per_stock"][code]["cvar"] 与 morning 管线一致：为**百分数**（如 -6.5 表示 -6.5%）。
    """
    rows: List[Tuple[str, bool, str]] = []
    if total_assets <= 0:
        rows.append(("total_assets", False, "总资产无效"))
        return rows

    cash_ratio = cash / total_assets if total_assets > 0 else 0.0
    if cash_ratio < 0.05:
        rows.append(
            ("cash_buffer", False, f"现金占比{cash_ratio:.1%}低于5%红线"),
        )

    per = (quant or {}).get("per_stock") or {}
    runtime_flags = (feature_snapshot or {}).get("runtime_flags") or {}
    portfolio_features = (feature_snapshot or {}).get("portfolio") or {}

    if feature_snapshot:
        if not runtime_flags.get("feature_fresh", False):
            rows.append(("feature_snapshot", False, "research feature snapshot 缺失或不新鲜"))
        market_regime = (portfolio_features.get("market_regime") or {}).get("current_state")
        if market_regime == "bear":
            rows.append(("market_regime", True, "市场状态为 bear：仅允许保守缩仓位新开仓（非硬拒绝）"))

    for h in holdings:
        code = str(h.get("code") or "")
        price = float(h.get("price") or 0)
        sh = int(h.get("shares") or 0)
        ratio = (price * sh) / total_assets if total_assets > 0 else 0.0
        if check_add_position(code, ratio) == ConstraintVerdict.BLOCKED:
            rows.append(
                (
                    "position_limit",
                    False,
                    f"{code}单标仓位{ratio:.1%}超过上限{MAX_SINGLE_POSITION_RATIO_ADD:.0%}",
                )
            )

        q = per.get(code) or {}
        cvar_pct = q.get("cvar")
        if cvar_pct is not None:
            try:
                cvar_dec = float(cvar_pct) / 100.0
            except (TypeError, ValueError):
                cvar_dec = None
            if cvar_dec is not None and check_new_position(code, cvar_dec) == ConstraintVerdict.BLOCKED:
                rows.append(
                    (
                        "cvar_block",
                        False,
                        f"{code} CVaR {float(cvar_pct):.2f}% 劣于 {CVAR_BLOCK_NEW_OPEN * 100:.0f}% 禁止新开/加仓",
                    )
                )

    if 0.05 <= cash_ratio < 0.20:
        rows.append(
            (
                "cash_deploy",
                True,
                f"现金占比{cash_ratio:.1%}低于20%，建议暂缓新开仓扫描（非硬拒绝）",
            ),
        )

    # 宏观/地缘 playbook（cron_state / event_calendar）
    try:
        import json
        import os

        cron_state_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "data", "cron_state.json"
        )
        if os.path.isfile(cron_state_path):
            with open(cron_state_path, encoding="utf-8") as f:
                cs = json.load(f)
            level = cs.get("event_level") or "NORMAL"
            pb = cs.get("playbook") or {}
            if level in ("HIGH", "CRITICAL") or not pb.get("allow_new_buy", True):
                rows.append(
                    (
                        "macro_event",
                        level == "HIGH",
                        (
                            f"宏观风险 {level}：仅允许保守限量新开仓（{pb.get('message', '')[:80]}）"
                            if level == "HIGH"
                            else f"宏观风险 {level}：禁止新开仓（{pb.get('message', '')[:80]}）"
                        ),
                    )
                )
            gross = (total_assets - cash) / total_assets if total_assets > 0 else 0
            max_gross = float(pb.get("max_gross_exposure") or 1.0)
            if level == "CRITICAL" and gross > max_gross + 0.02:
                rows.append(
                    (
                        "macro_de_risk",
                        False,
                        f"总仓位{gross:.0%}超过 CRITICAL 上限{max_gross:.0%}，须执行 de_risk_plan",
                    )
                )
    except Exception:
        pass

    if not rows:
        rows.append(("all", True, "OK"))
    return rows


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

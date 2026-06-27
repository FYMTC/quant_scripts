"""T1.6 量化动态止损规则（2026-06-26）

用买入成本回撤修复时间动态决定止损松紧，替代固定百分比止损。

核心思路：
    基于近期日均收益 mean(daily_returns) 估算 days_to_recover = |loss_pct| / mean_ret，
    按修复天数分档决定止损%：
        快速（≤14天回本）  → -8%  高动量/高日均收益标的，给足波动空间
        中速（15-60天回本） → -5%  中等动量，平衡保护与容忍
        慢速（>60天回本）  → -3%  低日均收益标的，紧止损防深套

集成点：
    - signal_loop.auto_generate() 生成 rapid_drop 信号时用动态阈值
    - smart_guard_v3 触发判断时用动态阈值
    - decision_gate SELL 决策时附加止损检查
"""

from __future__ import annotations

import math
from typing import Optional, Tuple


# 分档配置
STOP_LOSS_FAST = -8.0   # 快速修复档（≤14天）
STOP_LOSS_MID = -5.0    # 中速修复档（15-60天）
STOP_LOSS_SLOW = -3.0   # 慢速修复档（>60天）

DAYS_FAST_MAX = 14      # ≤14天算快速
DAYS_MID_MAX = 60       # 15-60天算中速

# 默认止损（无价格历史时）
DEFAULT_STOP_LOSS = -5.0

# 最小止损空间（避免极端高动量标的止损过宽）
MIN_STOP_LOSS = -3.0
# 最大止损空间（避免极端高动量标的止损过宽）
MAX_STOP_LOSS = -10.0


def classify_recovery_speed(daily_returns: list) -> Tuple[str, float]:
    """根据近期日均收益分类修复速度。

    Args:
        daily_returns: 近期日收益率列表（如 [0.01, -0.005, 0.02, ...]）

    Returns:
        (speed_label, mean_daily_return)
        speed_label: "fast" / "mid" / "slow"
    """
    if not daily_returns or len(daily_returns) < 3:
        return ("mid", 0.0)

    mean_ret = sum(daily_returns) / len(daily_returns)

    # 日均收益为负或接近0 → 慢速（难以回本）
    if mean_ret <= 0.0005:  # 日均 < 0.05%
        return ("slow", mean_ret)

    # 估算回本天数：假设亏 5%，需要多少天回本
    loss_pct = 0.05
    days_to_recover = loss_pct / mean_ret

    if days_to_recover <= DAYS_FAST_MAX:
        return ("fast", mean_ret)
    elif days_to_recover <= DAYS_MID_MAX:
        return ("mid", mean_ret)
    else:
        return ("slow", mean_ret)


def compute_stop_loss_pct(
    code: str,
    avg_cost: float,
    current_price: Optional[float] = None,
    daily_returns: Optional[list] = None,
) -> Tuple[float, dict]:
    """计算标的的动态止损百分比。

    Args:
        code: 股票代码
        avg_cost: 持仓成本价（用于判断浮亏程度）
        current_price: 当前价（可选，用于附加上下文）
        daily_returns: 近期日收益率列表（可选，无则用默认档）

    Returns:
        (stop_loss_pct, details)
        stop_loss_pct: 止损百分比（负数，如 -5.0 表示 -5%）
        details: 详细信息 dict
    """
    speed, mean_ret = classify_recovery_speed(daily_returns or [])

    if speed == "fast":
        stop_pct = STOP_LOSS_FAST
    elif speed == "mid":
        stop_pct = STOP_LOSS_MID
    else:
        stop_pct = STOP_LOSS_SLOW

    # 无价格历史时用默认
    if not daily_returns or len(daily_returns) < 3:
        stop_pct = DEFAULT_STOP_LOSS
        speed = "default"

    # 截断到合理范围
    stop_pct = max(stop_pct, MAX_STOP_LOSS)  # 不超过 -10%
    stop_pct = min(stop_pct, MIN_STOP_LOSS)  # 不紧于 -3%

    # 计算止损价
    stop_price = avg_cost * (1 + stop_pct / 100.0) if avg_cost > 0 else None

    details = {
        "code": code,
        "speed": speed,
        "mean_daily_return": round(mean_ret, 6),
        "stop_loss_pct": stop_pct,
        "stop_loss_price": round(stop_price, 3) if stop_price else None,
        "avg_cost": avg_cost,
        "current_price": current_price,
        "drawdown_from_cost": round((current_price - avg_cost) / avg_cost * 100, 2) if (avg_cost and current_price) else None,
        "days_history": len(daily_returns) if daily_returns else 0,
    }

    return stop_pct, details


def is_stop_loss_triggered(
    code: str,
    avg_cost: float,
    current_price: float,
    daily_returns: Optional[list] = None,
) -> Tuple[bool, dict]:
    """判断当前价是否触发动态止损。

    Returns:
        (triggered, details)
    """
    stop_pct, details = compute_stop_loss_pct(code, avg_cost, current_price, daily_returns)
    stop_price = details["stop_loss_price"]

    if stop_price is None or current_price <= 0:
        return False, details

    triggered = current_price <= stop_price
    details["triggered"] = triggered
    details["margin_to_stop"] = round(current_price - stop_price, 3)
    details["margin_to_stop_pct"] = round((current_price - stop_price) / current_price * 100, 2)

    return triggered, details


def cli():
    """CLI 入口：python3 dynamic_stop_loss.py 000063 --cost 36.385 --price 35.0"""
    import argparse
    import json
    import sys

    sys.path.insert(0, "/root/ai_trading_package/quant/quant_scripts")

    p = argparse.ArgumentParser(description="动态止损计算器")
    p.add_argument("code", help="股票代码")
    p.add_argument("--cost", type=float, required=True, help="持仓成本价")
    p.add_argument("--price", type=float, default=0, help="当前价（可选）")
    p.add_argument("--returns", nargs="*", type=float, help="近期日收益率列表（可选）")
    args = p.parse_args()

    stop_pct, details = compute_stop_loss_pct(
        args.code, args.cost, args.price or None, args.returns
    )
    print(json.dumps(details, ensure_ascii=False, indent=2))

    if args.price > 0:
        triggered, trig_details = is_stop_loss_triggered(
            args.code, args.cost, args.price, args.returns
        )
        print(f"\n触发状态: {'🚨 触发止损' if triggered else '✅ 未触发'}")
        print(f"距止损价: {trig_details.get('margin_to_stop', 'N/A')} 元 ({trig_details.get('margin_to_stop_pct', 'N/A')}%)")


if __name__ == "__main__":
    cli()

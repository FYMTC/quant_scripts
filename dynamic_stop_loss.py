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

# T1.10 二期：三件套止损常量
SUPPORT_LOOKBACK = 20        # 支撑位止损取近 20 日低点
VOL_STOP_K = 1.5             # 波动率止损：entry - k×ATR
ATR_LOOKBACK = 14            # ATR(14) 计算窗口


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


def compute_support_stop(code: str, avg_cost: float, *,
                         low_20: Optional[float] = None,
                         round_number: bool = False) -> Tuple[float, dict]:
    """T1.10 二期：支撑位止损。

    stop_price = low_20 - 0.01（近 20 日低点下方 1 分钱）；
    low_20 为 None 时回退到动态止损档（DEFAULT_STOP_LOSS）。
    round_number=True 时把 low_20 对齐到近整数关口（如 70/75/80）作为备选支撑。

    Returns:
        (stop_pct, details) — stop_pct 为负数百分比，已截断到 [MAX_STOP_LOSS, MIN_STOP_LOSS]
    """
    if avg_cost <= 0:
        return DEFAULT_STOP_LOSS, {
            "code": code, "source": "support", "low_20": low_20,
            "stop_loss_pct": DEFAULT_STOP_LOSS, "stop_loss_price": None,
            "reason": "avg_cost<=0, fallback default",
        }

    if low_20 is None or low_20 <= 0:
        # 无近 20 日低点 → 回退到动态止损默认档
        stop_pct = DEFAULT_STOP_LOSS
        stop_price = avg_cost * (1 + stop_pct / 100.0)
        return stop_pct, {
            "code": code, "source": "support", "low_20": low_20,
            "stop_loss_pct": stop_pct, "stop_loss_price": round(stop_price, 3),
            "reason": "low_20 missing, fallback to DEFAULT_STOP_LOSS",
        }

    # 整数关口备选支撑：取近 low_20 的整数关口（向下取整到 5 的倍数）
    support = low_20 - 0.01
    if round_number:
        round_floor = int(support // 5 * 5)
        if round_floor > 0 and round_floor < low_20:
            support = float(round_floor)

    stop_pct = (support / avg_cost - 1) * 100
    # 截断到合理范围
    stop_pct = max(stop_pct, MAX_STOP_LOSS)
    stop_pct = min(stop_pct, MIN_STOP_LOSS)
    # 截断后重算 stop_price 保持一致
    stop_price = avg_cost * (1 + stop_pct / 100.0)

    return stop_pct, {
        "code": code, "source": "support", "low_20": low_20,
        "support_price": round(support, 3), "round_number": round_number,
        "stop_loss_pct": stop_pct, "stop_loss_price": round(stop_price, 3),
        "avg_cost": avg_cost,
    }


def compute_vol_stop(code: str, avg_cost: float, *,
                     atr: Optional[float] = None,
                     daily_returns: Optional[list] = None) -> Tuple[float, dict]:
    """T1.10 二期：波动率止损。

    stop_price = avg_cost - K×ATR；stop_pct = (stop_price/avg_cost - 1)×100。
    atr 为 None 时用 daily_returns 的 std×sqrt(ATR_LOOKBACK) 简化估计。
    截断到 [MAX_STOP_LOSS, MIN_STOP_LOSS]。

    Returns:
        (stop_pct, details)
    """
    if avg_cost <= 0:
        return DEFAULT_STOP_LOSS, {
            "code": code, "source": "vol", "atr": atr,
            "stop_loss_pct": DEFAULT_STOP_LOSS, "stop_loss_price": None,
            "reason": "avg_cost<=0, fallback default",
        }

    # 估算 ATR
    est_atr = atr
    atr_source = "input"
    if est_atr is None or est_atr <= 0:
        if daily_returns and len(daily_returns) >= 3:
            # 简化估计：std(returns) × sqrt(14) × avg_cost（把收益率波动还原为价格波动）
            import statistics
            std_ret = statistics.stdev(daily_returns)
            est_atr = std_ret * (ATR_LOOKBACK ** 0.5) * avg_cost
            atr_source = "estimated_from_returns"
        else:
            # 无任何波动数据 → 回退默认
            stop_pct = DEFAULT_STOP_LOSS
            stop_price = avg_cost * (1 + stop_pct / 100.0)
            return stop_pct, {
                "code": code, "source": "vol", "atr": None,
                "stop_loss_pct": stop_pct, "stop_loss_price": round(stop_price, 3),
                "reason": "no atr/returns, fallback to DEFAULT_STOP_LOSS",
            }

    stop_price = avg_cost - VOL_STOP_K * est_atr
    stop_pct = (stop_price / avg_cost - 1) * 100
    # 截断
    stop_pct = max(stop_pct, MAX_STOP_LOSS)
    stop_pct = min(stop_pct, MIN_STOP_LOSS)
    stop_price = avg_cost * (1 + stop_pct / 100.0)

    return stop_pct, {
        "code": code, "source": "vol", "atr": round(est_atr, 4),
        "atr_source": atr_source, "k": VOL_STOP_K,
        "stop_loss_pct": stop_pct, "stop_loss_price": round(stop_price, 3),
        "avg_cost": avg_cost,
    }


def compute_tightest_stop(code: str, avg_cost: float,
                          current_price: Optional[float] = None,
                          daily_returns: Optional[list] = None,
                          low_20: Optional[float] = None,
                          atr: Optional[float] = None) -> Tuple[float, dict]:
    """T1.10 二期：三件套止损取紧。

    返回 max(动态止损_pct, 支撑位止损_pct, 波动率止损_pct)。
    注意三个百分比都是负数，max = 最紧 = 最高止损价（最早触发）。
    先例：signal_loop.py:463 的 max(rapid_drop_pct, stop_pct) 取紧逻辑。

    Returns:
        (stop_pct, details)
        details 含 dynamic/support/vol 三个子 dict + final_source 标明取紧来源
    """
    dyn_pct, dyn_details = compute_stop_loss_pct(
        code, avg_cost, current_price, daily_returns
    )
    sup_pct, sup_details = compute_support_stop(code, avg_cost, low_20=low_20)
    vol_pct, vol_details = compute_vol_stop(
        code, avg_cost, atr=atr, daily_returns=daily_returns
    )

    # 取紧：负数 max = 最紧
    candidates = [
        ("dynamic", dyn_pct, dyn_details),
        ("support", sup_pct, sup_details),
        ("vol", vol_pct, vol_details),
    ]
    final_source = "dynamic"
    final_pct = dyn_pct
    for name, pct, _ in candidates:
        if pct > final_pct:  # 负数比较：-4 > -5
            final_pct = pct
            final_source = name

    stop_price = avg_cost * (1 + final_pct / 100.0) if avg_cost > 0 else None

    details = {
        "code": code,
        "avg_cost": avg_cost,
        "current_price": current_price,
        "stop_loss_pct": final_pct,
        "stop_loss_price": round(stop_price, 3) if stop_price else None,
        "final_source": final_source,
        "components": {
            "dynamic": dyn_details,
            "support": sup_details,
            "vol": vol_details,
        },
    }
    if current_price and stop_price:
        details["margin_to_stop"] = round(current_price - stop_price, 3)
        details["margin_to_stop_pct"] = round(
            (current_price - stop_price) / current_price * 100, 2
        )
    return final_pct, details


def is_stop_loss_triggered(
    code: str,
    avg_cost: float,
    current_price: float,
    daily_returns: Optional[list] = None,
    *,
    low_20: Optional[float] = None,
    atr: Optional[float] = None,
) -> Tuple[bool, dict]:
    """判断当前价是否触发动态止损。

    T1.10 二期：传入 low_20 或 atr 时走三件套取紧（compute_tightest_stop），
    否则保持一期行为（compute_stop_loss_pct，向后兼容）。

    Returns:
        (triggered, details)
    """
    if low_20 is None and atr is None:
        # 一期行为：纯动态止损
        stop_pct, details = compute_stop_loss_pct(code, avg_cost, current_price, daily_returns)
    else:
        # 二期行为：三件套取紧
        stop_pct, details = compute_tightest_stop(
            code, avg_cost, current_price, daily_returns, low_20=low_20, atr=atr
        )
    stop_price = details.get("stop_loss_price")

    if stop_price is None or current_price <= 0:
        return False, details

    triggered = current_price <= stop_price
    details["triggered"] = triggered
    if "margin_to_stop" not in details:
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

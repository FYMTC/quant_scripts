"""direction_resolver.py — T1.10 信号方向解析器（2026-06-29）

核心思想：
    方向不应由信号类型决定，应由「持仓态 × 止损态 × 抄底分 × 大盘态」三元组决定。
    同一 rapid_drop 信号：
        持仓 + 未触发止损 → HOLD（忍，等反弹）
        持仓 + 触发止损   → SELL（截断亏损）
        空仓 + 抄底分达标 → BUY（均值回归抄底）
        空仓 + 抄底分不足 → WAIT（规避接飞刀）

替换 agent_desk.py:247-252 的写死 direction 判定（rapid_drop/price_below/rolling_decline
无条件 → SELL），与 agent_desk.py:666-669 的 forced_risk 同款逻辑。

设计参考：
    - 纯逻辑模块（仿 dynamic_stop_loss.py），无外部 IO 依赖，便于单测
    - 决策树速查表见 quant-wiki/strategies/single-stock-swing-strategy.md 附录 A
    - market_regime 枚举：bear/sideways/bull（market_regime.py:32）
    - risk_level 枚举：safe/warning/danger（position_sizer.py:68）

集成点：
    agent_desk._resolve_signal_direction() 组装三元组后调本模块 resolve_direction()
"""

from __future__ import annotations

from typing import Optional


# ========== 阈值常量 ==========

BF_THR = 0.6          # 抄底分阈值：≥ 则空仓急跌可 BUY
BO_THR = 0.6          # 突破分阈值：≥ 则空仓突破可 BUY
TP_THR = 0.6          # 止盈分阈值：≥ 则持仓急涨可 SELL
T_FLIP_GAP = 0.015    # 高开阈值：open > pre_close×(1+GAP) 且 price < open → 做T

# 信号类型枚举
SIGNAL_RAPID_DROP = "rapid_drop"
SIGNAL_PRICE_BELOW = "price_below"
SIGNAL_ROLLING_DECLINE = "rolling_decline"
SIGNAL_RAPID_SURGE = "rapid_surge"
SIGNAL_SURGE_PEAK = "surge_peak"
SIGNAL_PRICE_ABOVE = "price_above"
SIGNAL_UNKNOWN = "unknown"

DECLINE_SIGNALS = (SIGNAL_RAPID_DROP, SIGNAL_PRICE_BELOW, SIGNAL_ROLLING_DECLINE)
SURGE_SIGNALS = (SIGNAL_RAPID_SURGE, SIGNAL_SURGE_PEAK, SIGNAL_PRICE_ABOVE)

# 方向输出枚举
DIR_BUY = "BUY"
DIR_SELL = "SELL"
DIR_HOLD = "HOLD"
DIR_WAIT = "WAIT"
DIR_T_FLIP = "T_FLIP"


def classify_signal_type(signal_id: str) -> str:
    """从 signal_id 反解信号类型（contains 匹配）。

    signal_loop._build_signals_for_stock 生成的 id 形如：
        {code}_price_below_{target} / {code}_price_above_{target}
        {code}_rapid_drop / {code}_rapid_surge / {code}_surge_peak
    rolling_decline 由 smart_guard 触发，id 直接含该关键词。
    """
    sid = str(signal_id or "").lower()
    if "rolling_decline" in sid:
        return SIGNAL_ROLLING_DECLINE
    if "rapid_drop" in sid:
        return SIGNAL_RAPID_DROP
    if "price_below" in sid:
        return SIGNAL_PRICE_BELOW
    if "rapid_surge" in sid:
        return SIGNAL_RAPID_SURGE
    if "surge_peak" in sid:
        return SIGNAL_SURGE_PEAK
    if "price_above" in sid:
        return SIGNAL_PRICE_ABOVE
    return SIGNAL_UNKNOWN


def detect_t_flip(open_price: Optional[float],
                  pre_close: Optional[float],
                  current_price: Optional[float]) -> bool:
    """检测做T机会：高开低走（开盘卖出、尾盘买回，仓位不变成本下降）。

    触发条件：open > pre_close×(1+T_FLIP_GAP) 且 current < open
    参考：mean-reversion-backtest v3 做T增强策略。
    """
    try:
        if open_price is None or pre_close is None or current_price is None:
            return False
        if pre_close <= 0:
            return False
        gap_up = open_price > pre_close * (1 + T_FLIP_GAP)
        intra_decline = current_price < open_price
        return bool(gap_up and intra_decline)
    except Exception:
        return False


def _is_weak_market(regime: Optional[str], risk_level: Optional[str]) -> bool:
    """弱市判定：熊市或风险等级 danger。"""
    regime = str(regime or "").lower()
    risk_level = str(risk_level or "").lower()
    return regime == "bear" or risk_level == "danger"


def resolve_direction(
    *,
    signal_type: str,
    holding: bool,
    stop_triggered: bool,
    bottom_fish_score: Optional[float] = None,
    regime: Optional[str] = None,
    risk_level: Optional[str] = None,
    t_flip: bool = False,
    take_profit_score: Optional[float] = None,
    breakout_score: Optional[float] = None,
) -> str:
    """决策树：根据三元组解析信号方向。

    Args:
        signal_type: classify_signal_type() 的返回值
        holding: 是否持仓（True=持仓 Tier A，False=空仓）
        stop_triggered: 动态止损是否触发（仅 holding=True 时有意义）
        bottom_fish_score: 抄底分 0-1（仅 holding=False 时有意义）
        regime: market_regime (bear/sideways/bull)
        risk_level: risk_level (safe/warning/danger)
        t_flip: 是否检测到高开低走做T机会（仅 holding=True 时有意义）
        take_profit_score: 止盈分 0-1（持仓+急涨时用）
        breakout_score: 突破分 0-1（空仓+急涨/突破时用）

    Returns:
        BUY / SELL / HOLD / WAIT / T_FLIP
    """
    signal_type = signal_type or SIGNAL_UNKNOWN
    weak = _is_weak_market(regime, risk_level)

    # ── 持仓态 ──
    if holding:
        if signal_type in DECLINE_SIGNALS:
            if stop_triggered:
                return DIR_SELL       # 截断亏损
            if weak:
                return DIR_HOLD       # 弱市不忍痛割，但也不加仓
            if t_flip:
                return DIR_T_FLIP     # 高开低走做T降成本
            return DIR_HOLD           # 默认忍（T1.10 核心：跌≠该卖）
        if signal_type in SURGE_SIGNALS:
            tp = take_profit_score if take_profit_score is not None else 0.0
            return DIR_SELL if tp >= TP_THR else DIR_HOLD
        return DIR_HOLD               # 未知信号持仓默认忍

    # ── 空仓态 ──
    if signal_type in (SIGNAL_RAPID_DROP, SIGNAL_PRICE_BELOW):
        bf = bottom_fish_score if bottom_fish_score is not None else 0.0
        return DIR_BUY if bf >= BF_THR else DIR_WAIT
    if signal_type == SIGNAL_ROLLING_DECLINE:
        return DIR_WAIT               # 渐进阴跌不抄底（无急跌反弹语义）
    if signal_type in SURGE_SIGNALS:
        bo = breakout_score if breakout_score is not None else 0.0
        return DIR_BUY if bo >= BO_THR else DIR_WAIT
    return DIR_HOLD                   # 未知信号空仓默认观望


def resolve_from_event(
    *,
    signal_id: str,
    holding: bool,
    stop_triggered: bool = False,
    bottom_fish_score: Optional[float] = None,
    regime: Optional[str] = None,
    risk_level: Optional[str] = None,
    open_price: Optional[float] = None,
    pre_close: Optional[float] = None,
    current_price: Optional[float] = None,
) -> tuple:
    """便捷封装：从 signal_id 解析类型 + 检测做T + 决策。

    Returns:
        (direction, resolver_path) — resolver_path 为决策路径描述，供审计
    """
    sig_type = classify_signal_type(signal_id)
    t_flip = detect_t_flip(open_price, pre_close, current_price) if holding else False
    direction = resolve_direction(
        signal_type=sig_type,
        holding=holding,
        stop_triggered=stop_triggered,
        bottom_fish_score=bottom_fish_score,
        regime=regime,
        risk_level=risk_level,
        t_flip=t_flip,
    )
    weak = _is_weak_market(regime, risk_level)
    path = (
        f"sig={sig_type} holding={holding} stop={stop_triggered} "
        f"bf={bottom_fish_score} regime={regime} risk={risk_level} "
        f"weak={weak} t_flip={t_flip} → {direction}"
    )
    return direction, path


def cli():
    """CLI 入口：
    python3 direction_resolver.py --signal-id 002049_rapid_drop --holding --stop-triggered
    python3 direction_resolver.py --signal-id 002049_rolling_decline --no-holding
    """
    import argparse
    import json

    p = argparse.ArgumentParser(description="T1.10 信号方向解析器")
    p.add_argument("--signal-id", required=True, help="信号 ID（含类型关键词）")
    p.add_argument("--holding", dest="holding", action="store_true", default=None)
    p.add_argument("--no-holding", dest="holding", action="store_false")
    p.add_argument("--stop-triggered", action="store_true", help="持仓时止损是否触发")
    p.add_argument("--bottom-fish", type=float, default=None, help="抄底分 0-1")
    p.add_argument("--regime", default=None, help="market_regime: bear/sideways/bull")
    p.add_argument("--risk-level", default=None, help="risk_level: safe/warning/danger")
    p.add_argument("--open", type=float, default=None, help="当日开盘价（做T检测）")
    p.add_argument("--pre-close", type=float, default=None, help="昨收价（做T检测）")
    p.add_argument("--price", type=float, default=None, help="当前价（做T检测）")
    args = p.parse_args()

    if args.holding is None:
        p.error("必须指定 --holding 或 --no-holding")

    direction, path = resolve_from_event(
        signal_id=args.signal_id,
        holding=args.holding,
        stop_triggered=args.stop_triggered,
        bottom_fish_score=args.bottom_fish,
        regime=args.regime,
        risk_level=args.risk_level,
        open_price=args.open,
        pre_close=args.pre_close,
        current_price=args.price,
    )
    print(json.dumps({
        "signal_type": classify_signal_type(args.signal_id),
        "direction": direction,
        "resolver_path": path,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()

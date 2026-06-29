"""t_flip_applicability.py — T1.10 二期做T适用性自动判断（2026-06-30）

按近 3 月高开低走频率决定 gap 参数：
  高开低走天数 ≥ 3 且日内振幅 ≥ 4%  → 启用做T（gap=1.5%）
  高开低走天数 == 0                 → 关闭做T（gap=999，永不触发）
  其他                              → 观望（gap=2.0%）

参考：
  - backtest_drop_buy_rally_sell.py:58-68 的高开低走检测逻辑
  - mean-reversion-backtest v3 做T增强策略（净贡献 +6,597 元/6 个月）
  - single-stock-swing-strategy.md §5.2 做T降成本启用判据

集成点：
  agent_desk._resolve_signal_direction() 做T检测前调 is_applicable() gating，
  不适用则跳过 fetch_quote 调用省网络（方向解析器 detect_t_flip 仍用固定 gap）。
"""

from __future__ import annotations

import sys
import os
from typing import Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ========== 阈值常量 ==========

T_FLIP_GAP_ENABLED = 0.015     # 启用做T的 gap 阈值（1.5%）
T_FLIP_GAP_OBSERVE = 0.020     # 观望期 gap（2.0%）
T_FLIP_GAP_DISABLED = 9.99     # 关闭做T（永不触发）
MIN_FLIP_DAYS = 3              # 启用做T的最少高开低走天数
MIN_INTRADAY_RANGE = 0.04      # 启用做T的最小日内振幅（4%）
LOOKBACK_MONTHS = 3            # 回看月数


def detect_gap_up_decline(open_price: float, pre_close: float,
                          high: float, low: float, close: float,
                          gap_pct: float = T_FLIP_GAP_ENABLED) -> bool:
    """检测单日高开低走（复用 backtest_drop_buy_rally_sell.py:58-68 逻辑）。

    条件：
      1. gap_up = (open/pre_close - 1) >= gap_pct（高开 ≥ 1.5%）
      2. intraday_decline = close < open（盘中走低）
      3. close_position = (close - low) / (high - low) < 0.3（收盘在低位 30% 分位）

    Args:
        open_price: 当日开盘价
        pre_close: 昨收价
        high: 当日最高
        low: 当日最低
        close: 当日收盘
        gap_pct: 高开阈值（小数，0.015 = 1.5%）

    Returns:
        bool: 是否为高开低走日
    """
    try:
        if open_price is None or pre_close is None or high is None or low is None or close is None:
            return False
        if pre_close <= 0:
            return False
        gap_up = (open_price / pre_close - 1) >= gap_pct
        intraday_decline = close < open_price
        ir = high - low
        cp = (close - low) / ir if ir > 0 else 0.5
        return bool(gap_up and intraday_decline and cp < 0.3)
    except Exception:
        return False


def compute_t_flip_frequency(code: str, lookback_months: int = LOOKBACK_MONTHS) -> dict:
    """拉取近 N 月日线，统计高开低走天数 + 平均日内振幅。

    Returns:
        {
            "flip_days": int,              # 高开低走天数
            "total_days": int,             # 总交易日
            "avg_intraday_range": float,   # 平均日内振幅 (high-low)/pre_close
            "flip_ratio": float,           # flip_days / total_days
            "sample_dates": list[str],     # 高开低走日日期样本（最多 5 个）
        }
        失败返回 {"error": "..."}
    """
    try:
        from data_converter import fetch_kline_baostock
        from datetime import date, timedelta
        end = date.today().strftime("%Y%m%d")
        start = (date.today() - timedelta(days=lookback_months * 30 + 5)).strftime("%Y%m%d")
        recs = fetch_kline_baostock(code, start, end)
        if not recs or len(recs) < 2:
            return {"error": "no kline data", "flip_days": 0, "total_days": 0}

        flip_days = 0
        sample_dates = []
        intraday_ranges = []
        for i in range(1, len(recs)):
            prev = recs[i - 1]
            curr = recs[i]
            pre_close = float(prev.get("收盘") or 0)
            o = float(curr.get("开盘") or 0)
            h = float(curr.get("最高") or 0)
            low = float(curr.get("最低") or 0)
            c = float(curr.get("收盘") or 0)
            if pre_close <= 0 or h <= 0 or low <= 0:
                continue
            ir = (h - low) / pre_close
            intraday_ranges.append(ir)
            if detect_gap_up_decline(o, pre_close, h, low, c):
                flip_days += 1
                if len(sample_dates) < 5:
                    sample_dates.append(str(curr.get("日期") or ""))

        total = len(recs) - 1
        avg_ir = sum(intraday_ranges) / len(intraday_ranges) if intraday_ranges else 0.0
        return {
            "flip_days": flip_days,
            "total_days": total,
            "avg_intraday_range": round(avg_ir, 4),
            "flip_ratio": round(flip_days / total, 4) if total > 0 else 0.0,
            "sample_dates": sample_dates,
        }
    except Exception as e:
        return {"error": f"compute_t_flip_frequency failed: {str(e)[:200]}", "flip_days": 0, "total_days": 0}


def is_applicable(code: str) -> Tuple[bool, float, str]:
    """判断标的是否适用做T。

    Returns:
        (applicable, gap, reason)
          applicable=True,  gap=0.015  → 启用做T
          applicable=False, gap=9.99   → 关闭做T
          applicable=False, gap=0.020  → 观望（保留低 gap 备用但不算启用）

    判据（single-stock-swing-strategy.md §5.2）：
      flip_days >= MIN_FLIP_DAYS 且 avg_intraday_range >= MIN_INTRADAY_RANGE → 启用
      flip_days == 0                                                          → 关闭
      其他                                                                    → 观望
    """
    freq = compute_t_flip_frequency(code)
    if freq.get("error"):
        return False, T_FLIP_GAP_DISABLED, f"数据不可用: {str(freq.get('error'))[:80]}"

    flip_days = int(freq.get("flip_days") or 0)
    avg_ir = float(freq.get("avg_intraday_range") or 0)

    if flip_days >= MIN_FLIP_DAYS and avg_ir >= MIN_INTRADAY_RANGE:
        return True, T_FLIP_GAP_ENABLED, f"启用: flip_days={flip_days} avg_ir={avg_ir:.2%}"
    if flip_days == 0:
        return False, T_FLIP_GAP_DISABLED, f"关闭: flip_days=0 近3月无高开低走"
    return False, T_FLIP_GAP_OBSERVE, (
        f"观望: flip_days={flip_days}(<{MIN_FLIP_DAYS}) "
        f"或 avg_ir={avg_ir:.2%}(<{MIN_INTRADAY_RANGE:.2%})"
    )


def cli():
    """CLI 入口：python3 t_flip_applicability.py 002049"""
    import argparse
    import json

    p = argparse.ArgumentParser(description="T1.10 二期做T适用性判断")
    p.add_argument("code", help="股票代码")
    p.add_argument("--months", type=int, default=LOOKBACK_MONTHS, help="回看月数")
    args = p.parse_args()

    freq = compute_t_flip_frequency(args.code, args.months)
    applicable, gap, reason = is_applicable(args.code)
    print(json.dumps({
        "code": args.code,
        "frequency": freq,
        "applicable": applicable,
        "gap": gap,
        "reason": reason,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()

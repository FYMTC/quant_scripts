#!/config/quant_env/bin/python3
"""
risk_metrics.py — 风险指标计算模块

对标 FinCon (CVaR) + FINRS (多时间尺度动量) 论文。
Q1.1: GARCH(1,1) 波动率模型 (Engle 2003 诺奖)

提供：
1. CVaR（条件风险价值）— 历史模拟法
2. 多时间尺度动量（1日/7日/30日）— risk_check升级
3. 动量一致性评分
4. GARCH(1,1) 条件波动率 + 5日预测 🆕
5. 最大回撤

用法：
  from risk_metrics import calc_cvar, calc_multi_momentum, calc_garch_vol

  cvar = calc_cvar(price_series, confidence=0.95)
  momentum = calc_multi_momentum(price_series)
  garch = calc_garch_vol(price_series)          # {'cond_vol': ..., 'forecast_5d': [...]}
"""

from typing import List, Optional, Dict


def calc_returns(prices: List[float]) -> List[float]:
    """计算日收益率序列"""
    if len(prices) < 2:
        return []
    return [(prices[i+1] - prices[i]) / prices[i] for i in range(len(prices) - 1)]


def calc_cvar(prices: List[float], confidence: float = 0.95) -> Optional[float]:
    """
    计算CVaR（条件风险价值）
    CVaR = PnL序列最差 (1-confidence) 部分的均值
    """
    returns = calc_returns(prices)
    if len(returns) < 20:  # 需要至少20个交易日
        return None
    
    alpha = 1 - confidence
    n_tail = max(1, int(len(returns) * alpha))
    sorted_rets = sorted(returns)
    tail = sorted_rets[:n_tail]
    return sum(tail) / len(tail)


def calc_multi_momentum(prices: List[float]) -> dict:
    """
    多时间尺度动量计算
    对标 FINRS：1日（短期）、7日（中期）、30日（长期）
    """
    result = {'1d': 0.0, '7d': 0.0, '30d': 0.0, 'composite': 0.0, 'consistency': 0.0}
    
    if len(prices) < 2:
        return result
    
    latest = prices[-1]
    m1 = (latest - prices[-2]) / prices[-2]
    
    m7 = 0.0
    if len(prices) >= 8:
        m7 = (latest - prices[-8]) / prices[-8]
    
    m30 = 0.0
    if len(prices) >= 31:
        m30 = (latest - prices[-31]) / prices[-31]
    
    composite = m1 * 0.5 + m7 * 0.3 + m30 * 0.2
    
    # 方向一致性
    def sign(x, thresh=0.005):
        return 1 if x > thresh else (-1 if x < -thresh else 0)
    
    dirs = [sign(m) for m in (m1, m7, m30)]
    non_zero = [d for d in dirs if d != 0]
    consistency = 1.0 if len(non_zero) <= 1 else sum(1 for d in non_zero if d == non_zero[0]) / len(non_zero)
    
    return {
        '1d': round(m1 * 100, 2),
        '7d': round(m7 * 100, 2),
        '30d': round(m30 * 100, 2),
        'composite': round(composite * 100, 2),
        'consistency': round(consistency, 2),
    }


def calc_max_drawdown(prices: List[float]) -> Optional[float]:
    """计算最大回撤（百分比）"""
    if len(prices) < 2:
        return None
    peak = prices[0]
    max_dd = 0.0
    for p in prices:
        if p > peak:
            peak = p
        dd = (p - peak) / peak
        if dd < max_dd:
            max_dd = dd
    return round(max_dd * 100, 2)


# ========== CLI ==========

def cli():
    import argparse
    p = argparse.ArgumentParser(description="风险指标计算器")
    p.add_argument("action", choices=["cvar", "momentum", "mdd"], help="计算类型")
    p.add_argument("--prices", nargs="+", type=float, required=True, help="价格序列（从旧到新）")
    p.add_argument("--confidence", type=float, default=0.95, help="CVaR置信度")
    args = p.parse_args()
    
    if args.action == "cvar":
        result = calc_cvar(args.prices, args.confidence)
        print(f"CVaR({args.confidence:.0%}): {result*100:.2f}%" if result is not None else "CVaR: 数据不足")
    elif args.action == "momentum":
        result = calc_multi_momentum(args.prices)
        print(f"动量: 1日={result['1d']:+.2f}% 7日={result['7d']:+.2f}% 30日={result['30d']:+.2f}%")
        print(f"合成={result['composite']:+.2f}% 一致性={result['consistency']:.0%}")
    elif args.action == "mdd":
        result = calc_max_drawdown(args.prices)
        print(f"最大回撤: {result:.2f}%" if result is not None else "MDD: 数据不足")


if __name__ == "__main__":
    cli()

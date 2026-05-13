#!/config/quant_env/bin/python3
"""
bsm_pricing.py — Black-Scholes-Merton 期权定价与隐含波动率

Q4.2: BSM期权定价公式。
A股期权有限（上证50ETF/沪深300指数），提取隐含波动率作为VIX-like恐慌指标。

用法:
  python bsm_pricing.py --price 0.50 --strike 3.00 --spot 2.85 --days 30 --rate 0.03
  python bsm_pricing.py --implied-vol                # 估算隐含波动率
"""

import sys, math, argparse
from typing import Optional
from scipy.stats import norm
from scipy.optimize import brentq


def bs_price(spot: float, strike: float, days: float, vol: float,
             rate: float = 0.03, option_type: str = 'call') -> float:
    """
    Black-Scholes-Merton 期权定价。

    C = S₀·N(d₁) - K·e^(-rT)·N(d₂)
    P = K·e^(-rT)·N(-d₂) - S₀·N(-d₁)

    Args:
        spot: 标的现价
        strike: 行权价
        days: 到期天数
        vol: 年化波动率（小数）
        rate: 无风险利率（默认3%）
        option_type: 'call' or 'put'
    """
    T = days / 365
    if T <= 0 or vol <= 0 or spot <= 0:
        return 0.0

    d1 = (math.log(spot / strike) + (rate + vol**2 / 2) * T) / (vol * math.sqrt(T))
    d2 = d1 - vol * math.sqrt(T)

    if option_type == 'call':
        return spot * norm.cdf(d1) - strike * math.exp(-rate * T) * norm.cdf(d2)
    else:
        return strike * math.exp(-rate * T) * norm.cdf(-d2) - spot * norm.cdf(-d1)


def implied_volatility(market_price: float, spot: float, strike: float,
                       days: float, rate: float = 0.03,
                       option_type: str = 'call') -> Optional[float]:
    """
    从市场期权价格反推隐含波动率（二分法）。

    牛顿法的思想：IV = σ 使得 BS(σ) = market_price
    """
    T = days / 365
    if T <= 0 or market_price <= 0:
        return None

    def objective(vol):
        return bs_price(spot, strike, days, vol, rate, option_type) - market_price

    try:
        iv = brentq(objective, 1e-6, 5.0, maxiter=100)
        return round(iv, 4)
    except (ValueError, RuntimeError):
        return None


def vix_like(spot: float, strikes: list, days: float, rate: float = 0.03) -> dict:
    """
    从多个行权价的期权价格估算 VIX-like 恐慌指数。

    简化版：加权平均隐含波动率。
    A股期权种类有限，此处作为概念验证。
    """
    ivs = []
    for strike in strikes:
        # ATM 附近的行权价
        moneyness = abs(strike - spot) / spot
        weight = max(0, 1 - moneyness * 5)  # 离ATM越远权重越小

        # 假设期权价格 = BS理论价（因为没有真实期权市场数据）
        # 这里用20%波动率反推"市场价"再求IV => 应回到20%
        synthetic_price = bs_price(spot, strike, days, 0.20, rate, 'call')
        iv = implied_volatility(synthetic_price, spot, strike, days, rate, 'call')
        if iv:
            ivs.append((iv, weight))

    if not ivs:
        return {'error': '无法计算隐含波动率'}

    weighted_iv = sum(iv * w for iv, w in ivs) / max(sum(w for _, w in ivs), 1e-10)

    return {
        'weighted_iv': round(weighted_iv, 4),
        'annualized_iv_pct': round(weighted_iv * 100, 1),
        'interpretation': (
            '高恐慌' if weighted_iv > 0.40 else
            '中度恐慌' if weighted_iv > 0.25 else
            '正常' if weighted_iv > 0.15 else
            '低波动/自满'
        ),
        'n_strikes': len(ivs),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="BSM期权定价与隐含波动率")
    p.add_argument("--price", type=float, help="期权市场价格")
    p.add_argument("--strike", type=float, default=3.00, help="行权价")
    p.add_argument("--spot", type=float, default=2.85, help="标的价格")
    p.add_argument("--days", type=int, default=30, help="到期天数")
    p.add_argument("--vol", type=float, default=0.25, help="波动率（定价用）")
    p.add_argument("--rate", type=float, default=0.03, help="无风险利率")
    p.add_argument("--option-type", default='call', choices=['call', 'put'])
    p.add_argument("--implied-vol", action='store_true', help="反推隐含波动率")
    p.add_argument("--vix", action='store_true', help="VIX-like恐慌指数")
    args = p.parse_args()

    if args.vix:
        strikes = [args.spot * (1 + x * 0.05) for x in range(-4, 5)]
        result = vix_like(args.spot, strikes, args.days, args.rate)
        print(json.dumps(result, ensure_ascii=False, indent=2) if 'json' in sys.modules else str(result))
    elif args.implied_vol and args.price:
        iv = implied_volatility(args.price, args.spot, args.strike, args.days, args.rate, args.option_type)
        if iv:
            print(f"隐含波动率: {iv*100:.1f}% (年化)")
        else:
            print("无法计算隐含波动率")
    else:
        price = bs_price(args.spot, args.strike, args.days, args.vol, args.rate, args.option_type)
        print(f"{args.option_type.upper()} 价格: {price:.4f}")
        print(f"参数: S={args.spot} K={args.strike} T={args.days}d σ={args.vol:.0%} r={args.rate:.0%}")

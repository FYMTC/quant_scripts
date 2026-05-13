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


# ========== Q1.1: GARCH(1,1) 波动率模型 ==========

def calc_garch_vol(prices: List[float], horizon: int = 5) -> Dict:
    """
    GARCH(1,1) 条件波动率估计 + 前向预测。

    与简单历史 std 的区别：GARCH 捕捉波动率聚集效应——
    大波动后往往跟大波动，平静期后往往继续平静。

    Args:
        prices: 收盘价序列（从旧到新，≥60个）
        horizon: 前向预测天数（默认5天）

    Returns:
        {
            'cond_vol': float,           # 当前条件日波动率（小数）
            'ann_vol': float,            # 年化条件波动率
            'forecast_5d': [float*5],    # 未来5日波动率预测
            'simple_ann_vol': float,     # 简单历史波动率（对比用）
            'vol_regime': str,           # 'low'/'normal'/'high' vs 历史
            'converged': bool,
            'n_obs': int,                # 样本量
        }
        若数据不足或拟合失败返回 None
    """
    if len(prices) < 60:
        return None

    try:
        import numpy as np
        from arch import arch_model

        # 对数收益率（百分比）
        log_prices = np.log(np.array(prices, dtype=float))
        returns = np.diff(log_prices) * 100

        # 拟合 GARCH(1,1)
        model = arch_model(returns, vol='Garch', p=1, q=1)
        result = model.fit(disp='off')

        # 条件波动率
        cond_vol_arr = result.conditional_volatility
        cond_vol = float(cond_vol_arr[-1])  # 最新条件日波动率（%）

        # 年化
        ann_vol = cond_vol * np.sqrt(252) / 100

        # 前向预测
        forecast = result.forecast(horizon=horizon)
        fcast_var = forecast.variance.values[-1]
        fcast_vol = [float(v) / 100 for v in np.sqrt(fcast_var)]

        # 简单历史波动率（对比）
        simple_ann_vol = float(np.std(returns) * np.sqrt(252) / 100)

        # 波动率状态
        hist_vol_mean = float(np.mean(returns))
        if cond_vol > np.std(returns) * 1.5:
            vol_regime = 'high'
        elif cond_vol < np.std(returns) * 0.5:
            vol_regime = 'low'
        else:
            vol_regime = 'normal'

        return {
            'cond_vol': round(cond_vol / 100, 6),       # 日波动率
            'ann_vol': round(ann_vol, 4),                # 年化
            f'forecast_{horizon}d': [round(v, 6) for v in fcast_vol],
            'simple_ann_vol': round(simple_ann_vol, 4),
            'vol_regime': vol_regime,
            'converged': result.convergence_flag == 0,
            'n_obs': len(returns),
            'params': {
                'omega': round(float(result.params['omega']), 6),
                'alpha': round(float(result.params['alpha[1]']), 4),
                'beta': round(float(result.params['beta[1]']), 4),
                'persistence': round(float(result.params['alpha[1]']) + float(result.params['beta[1]']), 4),
            },
        }
    except Exception as e:
        return {'error': str(e), 'converged': False}


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
    elif args.action == "gbm":
        result = calc_gbm_cvar(args.prices, confidence=args.confidence)
        if result:
            print(f"GBM-CVaR({args.confidence:.0%}): {result['cvar']*100:.2f}% (VaR: {result['var']*100:.2f}%)")
            print(f"对比: 历史CVaR={result['historical_cvar']*100:.2f}%")
            print(f"参数: μ={result['mu']*100:.2f}% σ={result['sigma']:.1%} paths={result['n_paths']}")
        else:
            print("GBM-CVaR: 数据不足")


# ========== Q2.2: GBM 蒙特卡洛 VaR/CVaR ==========

def calc_gbm_cvar(prices: List[float], confidence: float = 0.95,
                  n_paths: int = 10000, horizon: int = 20,
                  annualize_input: bool = False) -> Optional[Dict]:
    """
    几何布朗运动(GBM)蒙特卡洛模拟计算 VaR/CVaR。

    与历史模拟法的区别：
    - 历史法：仅用过去20天数据，样本量极小
    - GBM法：基于参数化模型生成10000条路径，尾部估计更稳健

    dS/S = μ·dt + σ·dW    (dW ~ N(0,dt))

    Args:
        prices: 收盘价序列
        confidence: 置信水平（默认95%）
        n_paths: 模拟路径数（默认10000）
        horizon: 预测天数（默认20个交易日≈1个月）
        annualize_input: μ/σ是否已年化（默认False，自动从日数据估算）

    Returns:
        {
            'var': float,              # Value at Risk
            'cvar': float,             # Conditional VaR
            'historical_cvar': float,  # 历史CVaR（对比）
            'mu': float,              # 漂移率（年化）
            'sigma': float,           # 波动率（年化，优先用GARCH）
            'n_paths': int,
            'horizon_days': int,
            'confidence': float,
        }
    """
    if len(prices) < 20:
        return None

    try:
        import numpy as np

        # 对数收益率
        log_prices = np.log(np.array(prices, dtype=float))
        daily_rets = np.diff(log_prices)

        # 参数估计：μ(漂移率)、σ(波动率)
        # 优先使用 GARCH 条件波动率
        garch_result = calc_garch_vol(prices)
        if garch_result and garch_result.get('converged'):
            sigma_daily = garch_result['ann_vol'] / np.sqrt(252)
        else:
            sigma_daily = float(np.std(daily_rets))

        mu_daily = float(np.mean(daily_rets))

        # 年化
        mu = mu_daily * 252
        sigma = sigma_daily * np.sqrt(252) if not annualize_input else sigma_daily

        # 蒙特卡洛模拟
        dt = 1 / 252  # 日步长
        np.random.seed(42)
        Z = np.random.randn(n_paths, horizon)
        returns_paths = (mu_daily - 0.5 * sigma_daily**2) * dt + sigma_daily * np.sqrt(dt) * Z
        cumulative = np.sum(returns_paths, axis=1)

        # VaR & CVaR
        alpha = 1 - confidence
        var = -float(np.percentile(cumulative, alpha * 100))
        tail = cumulative[cumulative <= -var]
        cvar = -float(tail.mean()) if len(tail) > 0 else var

        # 历史CVaR（对比）
        hist_cvar = calc_cvar(prices, confidence)

        return {
            'var': round(var, 6),
            'cvar': round(cvar, 6),
            'historical_cvar': round(hist_cvar, 6) if hist_cvar is not None else None,
            'mu': round(mu, 6),
            'sigma': round(sigma, 4),
            'sigma_source': 'garch' if (garch_result and garch_result.get('converged')) else 'historical',
            'n_paths': n_paths,
            'horizon_days': horizon,
            'confidence': confidence,
        }
    except Exception as e:
        return {'error': str(e)}


# ========== Q3.2: Copula 尾部相关性 ==========

def calc_copula_tail(ret1: List[float], ret2: List[float]) -> Dict:
    """
    Copula 尾部依赖系数估计（Clayton下尾 + Gumbel上尾）。

    方法：
    1. 伪观测值转化：经验CDF → Uniform(0,1)
    2. Clayton Copula: C(u,v) = (u^(-θ) + v^(-θ) - 1)^(-1/θ)
       下尾依赖 λ_L = 2^(-1/θ)
    3. Gumbel Copula: C(u,v) = exp(-((-ln u)^θ + (-ln v)^θ)^(1/θ))
       上尾依赖 λ_U = 2 - 2^(1/θ)

    科学依据: Sklar定理，Basel III压力测试推荐工具。

    Args:
        ret1, ret2: 两个标的的对数收益率序列（等长）

    Returns:
        {
            'lambda_L': float,      # 下尾依赖系数（极端下跌的联动概率）
            'lambda_U': float,      # 上尾依赖系数（极端上涨的联动概率）
            'clayton_theta': float, # Clayton参数
            'gumbel_theta': float,  # Gumbel参数
            'linear_corr': float,   # 对比：线性相关系数
            'interpretation': str,  # 解释
        }
    """
    import numpy as np
    from scipy.optimize import minimize
    from scipy import stats

    ret1 = np.asarray(ret1, dtype=float)
    ret2 = np.asarray(ret2, dtype=float)
    n = min(len(ret1), len(ret2))
    ret1, ret2 = ret1[:n], ret2[:n]

    # 伪观测值（经验CDF）
    u = stats.rankdata(ret1) / (n + 1)
    v = stats.rankdata(ret2) / (n + 1)

    # Clip to avoid log(0)
    eps = 1e-10
    u = np.clip(u, eps, 1 - eps)
    v = np.clip(v, eps, 1 - eps)

    linear_corr = float(np.corrcoef(ret1, ret2)[0, 1])

    # ── Clayton Copula MLE ──
    def clayton_nll(theta):
        if theta <= 1e-6:
            return 1e10
        # log-likelihood
        c = (u ** (-theta) + v ** (-theta) - 1)
        if np.any(c <= 0):
            return 1e10
        ll = np.sum(np.log(1 + theta) - (theta + 1) * (np.log(u) + np.log(v))
                    - (2 + 1 / theta) * np.log(c))
        return -ll

    try:
        res_clayton = minimize(clayton_nll, x0=1.0, bounds=[(1e-6, 20)], method='L-BFGS-B')
        clayton_theta = float(res_clayton.x[0])
        lambda_L = 2 ** (-1 / clayton_theta) if clayton_theta > 0 else 0
    except Exception:
        clayton_theta = 0
        lambda_L = 0

    # ── Gumbel Copula MLE ──
    def gumbel_nll(theta):
        if theta < 1:
            return 1e10
        a = (-np.log(u)) ** theta
        b = (-np.log(v)) ** theta
        c = (a + b) ** (1 / theta)

        log_density = (
            np.log(c) * (1 - 2 * theta)
            + (theta - 1) * (np.log(np.log(u)) + np.log(np.log(v)))
            - np.log(u * v)
            - c
        )
        ll = np.sum(np.log(theta - 1 + c) + log_density)
        return -ll

    try:
        res_gumbel = minimize(gumbel_nll, x0=2.0, bounds=[(1.01, 20)], method='L-BFGS-B')
        gumbel_theta = float(res_gumbel.x[0])
        lambda_U = 2 - 2 ** (1 / gumbel_theta) if gumbel_theta > 1 else 0
    except Exception:
        gumbel_theta = 0
        lambda_U = 0

    # 解释
    if lambda_L > 0.3 and lambda_U > 0.3:
        interp = "强双向尾部依赖 — 极端行情下同涨同跌概率高，需严格分散"
    elif lambda_L > 0.3:
        interp = "强下尾依赖 — 崩盘时高度联动，建议不同行业分散"
    elif lambda_U > 0.3:
        interp = "强上尾依赖 — 牛市中齐涨，可适度集中但注意反转风险"
    elif lambda_L < 0.1 and lambda_U < 0.1:
        interp = "尾部独立 — 极端行情下各自独立，分散效果良好"
    else:
        interp = "中等尾部依赖 — 极端行情有一定联动"

    return {
        'lambda_L': round(lambda_L, 4),
        'lambda_U': round(lambda_U, 4),
        'clayton_theta': round(clayton_theta, 4),
        'gumbel_theta': round(gumbel_theta, 4),
        'linear_corr': round(linear_corr, 4),
        'n_obs': n,
        'interpretation': interp,
    }


if __name__ == "__main__":
    cli()

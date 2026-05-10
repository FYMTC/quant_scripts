#!/usr/bin/env python3
"""
风险分析与组合优化模块

功能:
1. 个股风险指标：VaR、最大回撤、波动率、Beta
2. 组合优化：均值方差、风险平价、最小方差
3. 持仓诊断：集中度、相关性、风险贡献
"""
import json
import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
from scipy.optimize import minimize

sys.path.insert(0, str(Path(__file__).parent))
from data_converter import fetch_kline, \
    STOCK_MAP


def load_stock_returns(stock_code, start_date="20230101", end_date="20250426"):
    """加载个股日收益率序列"""
    kline = fetch_kline(stock_code, start_date, end_date)
    if not kline:
        return None

    df = convert_to_qlib_csv(kline, f"/tmp/{stock_code}_risk_temp.csv")
    if df is None:
        return None

    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    df = df.set_index("date").sort_index()
    df["daily_return"] = df["close"].pct_change()
    return df["daily_return"].dropna()


def individual_risk_analysis(stock_code):
    """个股风险分析"""
    daily_returns = load_stock_returns(stock_code)
    if daily_returns is None or len(daily_returns) < 20:
        return None

    close = None
    # 获取当前价
    quote = run_omnidata_spider("eastmoney_stock_quote", {"stock_code": stock_code})
    if quote and quote.get("success") and quote.get("data"):
        close = float(quote["data"].get("最新价", 0))

    returns = daily_returns.values

    # 年化波动率
    annual_vol = returns.std() * np.sqrt(252) * 100

    # VaR (95%, 1日)
    var_95 = np.percentile(returns, 5) * 100

    # CVaR (95%)
    cvar_95 = returns[returns <= np.percentile(returns, 5)].mean() * 100

    # 最大回撤
    cum_returns = (1 + daily_returns).cumprod()
    peak = cum_returns.cummax()
    drawdown = cum_returns / peak - 1
    max_dd = drawdown.min() * 100

    # 收益
    latest_60d_ret = (1 + daily_returns.tail(60)).prod() - 1 if len(daily_returns) >= 60 else \
                     (1 + daily_returns.tail(min(30, len(daily_returns)))).prod() - 1
    latest_20d_ret = (1 + daily_returns.tail(20)).prod() - 1 if len(daily_returns) >= 20 else \
                     (1 + daily_returns).prod() - 1

    # 夏普比
    excess = returns.mean() - 0.02 / 252
    sharpe = np.sqrt(252) * excess / (returns.std() + 1e-9)

    # 偏度、峰度
    skewness = pd.Series(returns).skew()
    kurtosis = pd.Series(returns).kurtosis()

    # 最大连续下跌
    is_negative = returns < 0
    max_consecutive_loss = 0
    current_consecutive = 0
    for neg in is_negative:
        if neg:
            current_consecutive += 1
            max_consecutive_loss = max(max_consecutive_loss, current_consecutive)
        else:
            current_consecutive = 0

    return {
        "close": close,
        "年化波动率(%)": round(annual_vol, 2),
        "VaR_95(%)": round(var_95, 2),
        "CVaR_95(%)": round(cvar_95, 2),
        "最大回撤(%)": round(max_dd, 2),
        "近20日收益(%)": round(latest_20d_ret * 100, 2),
        "近60日收益(%)": round(latest_60d_ret * 100, 2),
        "夏普比": round(sharpe, 3),
        "偏度": round(skewness, 3),
        "峰度": round(kurtosis, 3),
        "最大连续下跌天数": max_consecutive_loss,
        "样本数": len(returns),
    }


def portfolio_optimization(stock_codes, lookback_days=252, risk_free_rate=0.02):
    """
    组合优化
    支持方法:
    1. 均值方差 (max sharpe)
    2. 最小方差
    3. 风险平价
    """
    # 加载所有股票的收益率
    all_returns = {}
    for code in stock_codes:
        daily_returns = load_stock_returns(code)
        if daily_returns is not None and len(daily_returns) >= lookback_days:
            all_returns[code] = daily_returns.tail(lookback_days)

    if len(all_returns) < 2:
        print("[FAIL] 至少需要2只股票且有足够数据", file=sys.stderr)
        return None

    returns_df = pd.DataFrame(all_returns).dropna()
    n_assets = len(returns_df.columns)
    stock_names = [STOCK_MAP.get(c, c) for c in returns_df.columns]

    # 年化收益率和协方差矩阵
    annual_returns = returns_df.mean() * 252
    cov_matrix = returns_df.cov() * 252

    print(f"[INFO] {n_assets} 只股票，{len(returns_df)} 天数据", file=sys.stderr)

    # ===== 1. 均值方差（最大夏普比）=====
    def neg_sharpe(weights):
        port_return = np.sum(annual_returns.values * weights)
        port_vol = np.sqrt(weights.T @ cov_matrix.values @ weights)
        return -(port_return - risk_free_rate) / (port_vol + 1e-9)

    constraints = {"type": "eq", "fun": lambda x: np.sum(x) - 1}
    bounds = tuple((0.02, 0.5) for _ in range(n_assets))  # 单只不超过50%，不低于2%

    init_weights = np.array([1 / n_assets] * n_assets)
    result_ms = minimize(neg_sharpe, init_weights, method="SLSQP",
                         bounds=bounds, constraints=constraints,
                         options={"maxiter": 1000, "ftol": 1e-12})

    if not result_ms.success:
        print(f"[WARN] 均值方差优化未收敛: {result_ms.message}", file=sys.stderr)
        weights_ms = init_weights
    else:
        weights_ms = result_ms.x

    port_return_ms = np.sum(annual_returns.values * weights_ms)
    port_vol_ms = np.sqrt(weights_ms.T @ cov_matrix.values @ weights_ms)
    sharpe_ms = (port_return_ms - risk_free_rate) / (port_vol_ms + 1e-9)

    # ===== 2. 最小方差 =====
    def port_vol(weights):
        return np.sqrt(weights.T @ cov_matrix.values @ weights)

    result_mv = minimize(port_vol, init_weights, method="SLSQP",
                         bounds=bounds, constraints=constraints,
                         options={"maxiter": 1000, "ftol": 1e-12})

    weights_mv = result_mv.x
    port_return_mv = np.sum(annual_returns.values * weights_mv)
    port_vol_mv = np.sqrt(weights_mv.T @ cov_matrix.values @ weights_mv)
    sharpe_mv = (port_return_mv - risk_free_rate) / (port_vol_mv + 1e-9)

    # ===== 3. 风险平价 =====
    def risk_parity_objective(weights):
        port_vol = np.sqrt(weights.T @ cov_matrix.values @ weights)
        # 每个资产的边际风险贡献
        marginal_contrib = (cov_matrix.values @ weights) / (port_vol + 1e-9)
        risk_contrib = weights * marginal_contrib
        # 目标：每个资产风险贡献相等
        target_risk = port_vol / n_assets
        return np.sum((risk_contrib - target_risk) ** 2)

    result_rp = minimize(risk_parity_objective, init_weights, method="SLSQP",
                          bounds=bounds, constraints=constraints,
                          options={"maxiter": 1000, "ftol": 1e-12})

    weights_rp = result_rp.x
    port_return_rp = np.sum(annual_returns.values * weights_rp)
    port_vol_rp = np.sqrt(weights_rp.T @ cov_matrix.values @ weights_rp)
    sharpe_rp = (port_return_rp - risk_free_rate) / (port_vol_rp + 1e-9)

    # 汇总
    portfolio_result = {
        "stock_codes": list(returns_df.columns),
        "stock_names": stock_names,
        "methods": {
            "均值方差(最大夏普)": {
                "weights": [round(w * 100, 1) for w in weights_ms],
                "年化收益(%)": round(port_return_ms * 100, 2),
                "年化波动(%)": round(port_vol_ms * 100, 2),
                "夏普比": round(sharpe_ms, 3),
            },
            "最小方差": {
                "weights": [round(w * 100, 1) for w in weights_mv],
                "年化收益(%)": round(port_return_mv * 100, 2),
                "年化波动(%)": round(port_vol_mv * 100, 2),
                "夏普比": round(sharpe_mv, 3),
            },
            "风险平价": {
                "weights": [round(w * 100, 1) for w in weights_rp],
                "年化收益(%)": round(port_return_rp * 100, 2),
                "年化波动(%)": round(port_vol_rp * 100, 2),
                "夏普比": round(sharpe_rp, 3),
            },
        },
    }

    return portfolio_result


def format_risk_report(stock_code, risk_data):
    """格式化个股风险报告"""
    if risk_data is None:
        return f"⚠️ {STOCK_MAP.get(stock_code, stock_code)}: 数据不足，无法分析"

    lines = [
        f"📊 **{STOCK_MAP.get(stock_code, stock_code)} 风险分析**",
        f"收盘价: {risk_data.get('close', 'N/A')}",
        f"",
        f"**波动风险**",
        f"├ 年化波动率: {risk_data['年化波动率(%)']}%",
        f"├ VaR(95%): {risk_data['VaR_95(%)']}% (1日最大可能亏损)",
        f"└ CVaR(95%): {risk_data['CVaR_95(%)']}% (极端情况下平均亏损)",
        f"",
        f"**回撤与收益**",
        f"├ 最大回撤: {risk_data['最大回撤(%)']}%",
        f"├ 近20日收益: {risk_data['近20日收益(%)']:+.2f}%",
        f"└ 近60日收益: {risk_data['近60日收益(%)']:+.2f}%",
        f"",
        f"**综合指标**",
        f"├ 夏普比: {risk_data['夏普比']}",
        f"├ 偏度: {risk_data['偏度']} ({'右偏' if risk_data['偏度'] > 0 else '左偏'} 分布)",
        f"├ 峰度: {risk_data['峰度']} ({'厚尾' if risk_data['峰度'] > 0.5 else '正常' if abs(risk_data['峰度']) <= 0.5 else '薄尾'})",
        f"└ 最大连续下跌: {risk_data['最大连续下跌天数']}天",
        f"",
        f"样本: {risk_data['样本数']}天",
    ]
    return "\n".join(lines)


def format_portfolio_report(portfolio_result):
    """格式化组合优化报告"""
    if portfolio_result is None:
        return "⚠️ 数据不足，无法进行组合优化"

    lines = [
        "📊 **组合优化报告**",
        f"股票池: {', '.join(portfolio_result['stock_names'])}",
        f"",
    ]

    for method_name, method_data in portfolio_result["methods"].items():
        lines.append(f"**{method_name}**")
        lines.append(f"├ 年化收益: **{method_data['年化收益(%)']}%**")
        lines.append(f"├ 年化波动: {method_data['年化波动(%)']}%")
        lines.append(f"├ 夏普比: {method_data['夏普比']}")
        lines.append(f"└ 仓位分配:")
        for i, (code, name) in enumerate(zip(portfolio_result["stock_codes"],
                                              portfolio_result["stock_names"])):
            w = method_data["weights"][i]
            if w > 1:
                lines.append(f"    {name}({code}): {w}%")
        lines.append(f"")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="风险分析与组合优化")
    parser.add_argument("--code", help="个股风险分析")
    parser.add_argument("--codes", nargs="+", help="组合优化（多只股票）")
    parser.add_argument("--action", default="risk", choices=["risk", "portfolio", "all"])
    args = parser.parse_args()

    if args.action in ("risk", "all") and args.code:
        risk = individual_risk_analysis(args.code)
        if risk:
            print(format_risk_report(args.code, risk))
            print()
            print(json.dumps(risk, ensure_ascii=False, indent=2))

    if args.action in ("portfolio", "all") and args.codes:
        result = portfolio_optimization(args.codes)
        if result:
            print(format_portfolio_report(result))
            print()
            print(json.dumps(result, ensure_ascii=False, indent=2))

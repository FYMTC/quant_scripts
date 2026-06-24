#!/usr/local/bin/python3
"""
stat_arb.py — 协整性检验与配对统计套利

Q3.1: Johansen/Engle-Granger 协整检验 → 寻找协整对 → 价差回归策略。
A股限制：仅做多方向（不可做空），价差>2σ时做多弱势标的。

用法:
  python stat_arb.py                      # 全量扫描，stdout报告
  python stat_arb.py --json               # JSON输出
  python stat_arb.py --pairs 000063,512480 # 指定配对检验

科学依据: Engle-Granger 1987 诺贝尔奖，配对交易行业标准。
"""

import sys, json, os, argparse
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from itertools import combinations

sys.path.insert(0, os.path.dirname(__file__))

# 行业分组（简化版，用于优先配对同行业标的）
INDUSTRY_GROUPS = {
    '半导体/科技': ['512480', '000063', '000938', '002049', '603986'],
    '新能源/光伏': ['515790', '002594', '300750', '601012'],
    '消费/白酒': ['600519', '000858', '002304', '600809'],
    '金融/银行': ['000001', '600036', '601398', '510050'],
}


def fetch_pair_data(codes: List[str], days: int = 120) -> Dict[str, np.ndarray]:
    """从 Baostock 获取多标的价格序列并对齐"""
    try:
        from data_converter import fetch_kline_baostock
    except ImportError:
        return {}

    end = datetime.now().strftime('%Y%m%d')
    start = (datetime.now() - timedelta(days=days * 2)).strftime('%Y%m%d')

    price_data = {}
    for code in codes:
        records = fetch_kline_baostock(code, start, end)
        if records and len(records) >= 60:
            closes = np.array([float(r['收盘']) for r in records])
            price_data[code] = closes

    # 对齐长度（取所有标的共有的最短长度）
    if len(price_data) < 2:
        return price_data

    min_len = min(len(v) for v in price_data.values())
    for code in price_data:
        price_data[code] = price_data[code][-min_len:]

    return price_data


def adf_test(series: np.ndarray, maxlag: int = None) -> Dict:
    """
    Augmented Dickey-Fuller 单位根检验。

    H₀: 存在单位根（非平稳）
    H₁: 平稳

    Returns: {'statistic': ..., 'pvalue': ..., 'is_stationary': bool}
    """
    try:
        from statsmodels.tsa.stattools import adfuller
        result = adfuller(series, maxlag=maxlag, autolag='AIC' if maxlag is None else None)
        return {
            'statistic': round(float(result[0]), 4),
            'pvalue': round(float(result[1]), 4),
            'is_stationary': result[1] < 0.05,
            'usedlag': int(result[2]),
            'nobs': int(result[3]),
        }
    except ImportError:
        return {'error': 'statsmodels not available'}


def engle_granger_test(y: np.ndarray, x: np.ndarray) -> Dict:
    """
    Engle-Granger 两步法协整检验。

    1. OLS: y = α + β·x + ε
    2. ADF on ε → 若ε平稳则协整

    Returns:
        {
            'cointegrated': bool,
            'pvalue': float,
            'hedge_ratio': float,
            'intercept': float,
            'spread': np.ndarray,
            'spread_mean': float,
            'spread_std': float,
            'half_life': float,
            'current_zscore': float,
        }
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)

    # 对数价格
    log_x = np.log(x)
    log_y = np.log(y)

    # OLS
    X = np.column_stack([np.ones(len(log_x)), log_x])
    beta = np.linalg.lstsq(X, log_y, rcond=None)[0]
    intercept, hedge_ratio = beta[0], beta[1]

    # 价差
    spread = log_y - (intercept + hedge_ratio * log_x)

    # ADF on spread
    adf = adf_test(spread)
    cointegrated = adf.get('is_stationary', False)

    # 价差统计
    spread_mean = float(np.mean(spread))
    spread_std = float(np.std(spread))
    current_zscore = float((spread[-1] - spread_mean) / spread_std) if spread_std > 0 else 0

    # 半衰期（均值回归速度）
    half_life = _estimate_half_life(spread)

    return {
        'cointegrated': cointegrated,
        'pvalue': adf.get('pvalue', 1.0),
        'adf_statistic': adf.get('statistic', 0),
        'hedge_ratio': round(float(hedge_ratio), 4),
        'intercept': round(float(intercept), 4),
        'spread_mean': round(spread_mean, 6),
        'spread_std': round(spread_std, 6),
        'half_life': round(half_life, 1),
        'current_zscore': round(current_zscore, 2),
        'n_obs': len(x),
    }


def _estimate_half_life(spread: np.ndarray) -> float:
    """估计价差均值回归半衰期（天数）"""
    spread = np.asarray(spread, dtype=float)
    delta_spread = np.diff(spread)
    lag_spread = spread[:-1]

    # OLS: Δs = α + β·s + ε
    X = np.column_stack([np.ones(len(lag_spread)), lag_spread])
    beta = np.linalg.lstsq(X, delta_spread, rcond=None)[0]
    theta = -beta[1]

    if theta <= 0 or theta > 1:
        return float('inf')

    return float(-np.log(2) / theta)


def scan_pairs(price_data: Dict[str, np.ndarray],
               significance: float = 0.10) -> List[Dict]:
    """
    扫描所有配对，返回协整对。

    Args:
        price_data: {code: prices_array}
        significance: ADF检验显著性水平（默认0.10，宽松）

    Returns:
        [{code1, code2, cointegrated, hedge_ratio, half_life, zscore, ...}]
    """
    results = []
    codes = list(price_data.keys())

    for c1, c2 in combinations(codes, 2):
        result = engle_granger_test(price_data[c1], price_data[c2])
        if result['cointegrated'] and result['pvalue'] < significance:
            results.append({
                'code1': c1,
                'code2': c2,
                **result,
            })

    # 按显著性排序
    results.sort(key=lambda r: r['pvalue'])
    return results


def find_industry(codes: List[str]) -> Dict[str, str]:
    """查找标的所属行业"""
    mapping = {}
    for code in codes:
        for industry, members in INDUSTRY_GROUPS.items():
            if code in members:
                mapping[code] = industry
                break
        if code not in mapping:
            mapping[code] = '其他'
    return mapping


def render_report(pairs: List[Dict], price_data: Dict) -> str:
    """渲染人类可读报告"""
    industries = find_industry(list(price_data.keys()))
    now = datetime.now().strftime('%Y-%m-%d %H:%M')

    lines = [
        "=" * 55,
        "  协整性检验与配对统计套利",
        f"  时间: {now}",
        f"  扫描: {len(price_data)}只标的 → {len(list(combinations(list(price_data.keys()), 2)))}个配对",
        "=" * 55,
    ]

    if not pairs:
        lines.append("\n⚠️ 未发现显著协整对（p < 0.10）")
        lines.append("   可能原因：样本期过短、行业差异过大、市场效率较高")
        return "\n".join(lines)

    lines.append(f"\n📊 发现 {len(pairs)} 个协整对:\n")

    for i, p in enumerate(pairs[:10]):  # top 10
        ind1 = industries.get(p['code1'], '?')
        ind2 = industries.get(p['code2'], '?')
        same_ind = "同行业" if ind1 == ind2 else "跨行业"

        signal = ""
        z = p['current_zscore']
        if abs(z) > 2.0:
            if z > 2:
                signal = f" 🔥 {p['code1']}相对高估 → 关注{p['code2']}（价差+{z:.1f}σ）"
            else:
                signal = f" 🔥 {p['code2']}相对高估 → 关注{p['code1']}（价差{z:.1f}σ）"
        elif abs(z) > 1.5:
            signal = " ⚠️ 接近阈值"

        lines.append(
            f"  [{i+1}] {p['code1']}({ind1}) ↔ {p['code2']}({ind2}) {same_ind}"
        )
        lines.append(
            f"      p={p['pvalue']:.4f} β={p['hedge_ratio']:.3f} "
            f"半衰期={p['half_life']:.0f}天 z-score={z:+.2f}{signal}"
        )

    # 信号汇总
    signals = [p for p in pairs if abs(p['current_zscore']) > 1.5]
    if signals:
        lines.append(f"\n🎯 交易信号 ({len(signals)}个):")
        for s in signals:
            z = s['current_zscore']
            if z > 2:
                target = s['code2']
                lines.append(f"  → {target} 被低估 {z:.1f}σ，可考虑做多（A股仅多方向）")
            elif z < -2:
                target = s['code1']
                lines.append(f"  → {target} 被低估 {-z:.1f}σ，可考虑做多")

    return "\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="协整检验与配对统计套利")
    p.add_argument("--codes", default="", help="逗号分隔的标的代码（默认持仓+自选）")
    p.add_argument("--pairs", default="", help="指定配对检验，如 000063,512480")
    p.add_argument("--significance", type=float, default=0.10, help="显著性阈值")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    # 确定标的列表
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",")]
    else:
        # 默认：持仓标的
        try:
            from stock_kb import StockKB
            kb = StockKB()
            pf = kb.read_portfolio_truth()
            codes = list(pf['positions'].keys())
        except Exception:
            codes = ['000063', '512480', '515790']

    if len(codes) < 2:
        print("ERROR: 至少需要2个标的进行协整检验", file=sys.stderr)
        sys.exit(1)

    price_data = fetch_pair_data(codes)

    if len(price_data) < 2:
        print("ERROR: 数据不足（需≥2个标的且≥60天数据）", file=sys.stderr)
        sys.exit(1)

    if args.pairs:
        c1, c2 = [c.strip() for c in args.pairs.split(",")]
        if c1 in price_data and c2 in price_data:
            result = engle_granger_test(price_data[c1], price_data[c2])
            pairs = [{'code1': c1, 'code2': c2, **result}] if result['cointegrated'] else []
        else:
            pairs = []
    else:
        pairs = scan_pairs(price_data, args.significance)

    if args.json:
        output = {'pairs': pairs, 'n_stocks': len(price_data), 'n_pairs_scanned': len(list(combinations(list(price_data.keys()), 2)))}
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(render_report(pairs, price_data))

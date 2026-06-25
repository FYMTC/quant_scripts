#!python3
"""
market_regime.py — 隐马尔可夫模型(HMM)市场状态识别

Q1.2: 3状态高斯HMM（牛市/震荡/熊市），输出状态概率 + 转移矩阵。
科学依据：Rabiner 1989, HMM在金融状态识别的经典应用。

用法:
  python market_regime.py                          # 全量分析，stdout
  python market_regime.py --json                   # JSON输出（供cron注入）
  python market_regime.py --code 000300            # 指定标的（默认沪深300）

落地：
  night_preflight.py 每日调用 → cron 注入市场状态上下文
  策略联动：熊市→收紧止损、BUY阈值上移
"""

import sys, json, os, argparse
import numpy as np
from datetime import datetime
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(__file__))

# 默认用平安银行(000001)作为市场代理（Baostock不支持指数，取大盘股近似）
DEFAULT_CODE = "000001"
# HMM 参数
N_STATES = 3
MIN_OBS = 60   # 最少60个交易日（Baostock仅提供约80天数据）

# 状态标签映射
STATE_LABELS = {
    0: 'bear',      # 熊市：低收益+高波动
    1: 'sideways',  # 震荡：中等收益+中等波动
    2: 'bull',      # 牛市：高收益+低波动
}

STATE_CN = {
    'bear': '熊市',
    'sideways': '震荡',
    'bull': '牛市',
}


def fetch_index_data(code: str = DEFAULT_CODE, days: int = 500) -> Optional[np.ndarray]:
    """从 Baostock 获取指数日线数据"""
    try:
        from data_converter import fetch_kline_baostock
        from datetime import date, timedelta
        end = date.today().strftime('%Y%m%d')
        start = (date.today() - timedelta(days=days * 2)).strftime('%Y%m%d')
        records = fetch_kline_baostock(code, start, end)
        if not records or len(records) < MIN_OBS:
            return None
        closes = np.array([float(r['收盘']) for r in records])
        return closes
    except Exception as e:
        print(f"[ERROR] fetch_index_data: {e}", file=sys.stderr)
        return None


def fit_hmm(returns: np.ndarray) -> Optional[Dict]:
    """
    拟合 3 状态高斯 HMM。

    Args:
        returns: 日对数收益率序列（≥250个）

    Returns:
        {
            'states': {0: 'bear', 1: 'sideways', 2: 'bull'},
            'state_means': [μ₀, μ₁, μ₂],        # 各状态日均收益
            'state_vols': [σ₀, σ₁, σ₂],          # 各状态日波动率
            'transmat': [[p₀₀,p₀₁,p₀₂], ...],    # 转移概率矩阵
            'current_state': int,                 # 当前最可能状态
            'current_probs': [p₀,p₁,p₂],          # 当前状态概率分布
            'state_sequence': [...],              # 完整状态序列
            'n_obs': int,
            'aic': float,
        }
    """
    if len(returns) < MIN_OBS:
        return None

    try:
        from hmmlearn import hmm

        X = returns.reshape(-1, 1)

        # 高斯HMM，3状态，全协方差
        model = hmm.GaussianHMM(
            n_components=N_STATES,
            covariance_type='full',
            n_iter=1000,
            random_state=42,
        )
        model.fit(X)

        # 状态序列
        states = model.predict(X)

        # 各状态统计
        state_means = []
        state_vols = []
        for s in range(N_STATES):
            mask = states == s
            if mask.sum() > 0:
                state_means.append(float(returns[mask].mean()))
                state_vols.append(float(returns[mask].std()))
            else:
                state_means.append(0.0)
                state_vols.append(0.0)

        # 按收益排序映射标签
        sorted_idx = np.argsort(state_means)
        label_map = {sorted_idx[0]: 'bear', sorted_idx[1]: 'sideways', sorted_idx[2]: 'bull'}

        # 当前状态
        current_state = int(states[-1])
        current_probs = model.predict_proba(X)[-1].tolist()

        # 转移矩阵
        transmat = model.transmat_.tolist()

        # AIC（拟合优度）
        n_params = N_STATES * (N_STATES - 1) + N_STATES * 2  # 转移概率 + 均值+方差
        log_likelihood = model.score(X)
        aic = 2 * n_params - 2 * log_likelihood

        # 状态分布统计
        state_counts = {s: int((states == s).sum()) for s in range(N_STATES)}
        total = len(states)
        state_pcts = {label_map[s]: round(state_counts[s] / total * 100, 1) for s in range(N_STATES)}

        return {
            'states': {str(k): label_map[k] for k in range(N_STATES)},
            'state_means': [round(m * 100, 3) for m in state_means],   # 转为百分比
            'state_vols': [round(v * 100, 3) for v in state_vols],     # 转为百分比
            'transmat': [[round(p, 4) for p in row] for row in transmat],
            'current_state': label_map[current_state],
            'current_state_id': current_state,
            'current_probs': [round(p, 4) for p in current_probs],
            'state_distribution': state_pcts,
            'n_obs': len(returns),
            'aic': round(aic, 1),
            'sequence_length': len(states),
        }
    except Exception as e:
        return {'error': str(e)}


def render_report(result: Dict) -> str:
    """渲染人类可读报告"""
    if not result or 'error' in result:
        return f"HMM拟合失败: {result.get('error', 'unknown')}"

    current = result['current_state']
    probs = result['current_probs']
    current_idx = result['current_state_id']

    lines = [
        "=" * 55,
        "  HMM 市场状态识别 (3-state Gaussian HMM)",
        f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"  样本: {result['n_obs']}个交易日",
        "=" * 55,
        "",
        "📊 各状态特征:",
    ]

    for s_id, label in result['states'].items():
        idx = int(s_id)
        mean = result['state_means'][idx]
        vol = result['state_vols'][idx]
        pct = result['state_distribution'].get(label, 0)
        marker = " ← 当前" if label == current else ""
        lines.append(
            f"  {STATE_CN.get(label, label):4s} | "
            f"日均收益 {mean:+.3f}% | 波动率 {vol:.2f}% | "
            f"占比 {pct:.0f}%{marker}"
        )

    lines.append(f"\n🎯 当前状态: {STATE_CN.get(current, current)}")
    lines.append(f"   概率分布: 熊市={probs[0]:.1%} 震荡={probs[1]:.1%} 牛市={probs[2]:.1%}")

    lines.append(f"\n🔄 状态转移矩阵 (row→col):")
    lines.append(f"   {'':>8} {'熊市':>8} {'震荡':>8} {'牛市':>8}")
    for i, row in enumerate(result['transmat']):
        label = STATE_CN.get(result['states'][str(i)], f'State{i}')
        lines.append(f"   {label:>4s}  " + "  ".join(f"{p:.3f}" for p in row))

    # 策略建议
    lines.append(f"\n💡 策略建议:")
    if current == 'bear':
        lines.append(f"   ⚠️ 熊市模式 → 收紧止损(-5%→-3%)、BUY阈值上移(0.8→1.2)")
        lines.append(f"   📉 建议降低仓位，优先现金或防御性标的")
    elif current == 'bull':
        lines.append(f"   ✅ 牛市模式 → 正常止损、可适度追涨")
        lines.append(f"   📈 当前牛市概率 {probs[2]:.0%}，震荡概率 {probs[1]:.0%}")
    else:
        lines.append(f"   ⏸️ 震荡模式 → 维持正常参数，关注突破方向")
        bear_to_sideways = result['transmat'][0][1] if len(result['transmat']) > 1 else 0
        if bear_to_sideways > 0.2:
            lines.append(f"   🔄 熊市→震荡概率 {bear_to_sideways:.0%}，可能正在筑底")

    return "\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="HMM市场状态识别")
    p.add_argument("--code", default=DEFAULT_CODE, help="标的代码（默认000300沪深300）")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    closes = fetch_index_data(args.code)
    if closes is None:
        print(f"ERROR: 无法获取 {args.code} 数据（需≥{MIN_OBS}个交易日）", file=sys.stderr)
        sys.exit(1)

    log_returns = np.diff(np.log(closes))
    result = fit_hmm(log_returns)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_report(result))

    sys.exit(0 if result and 'error' not in result else 1)

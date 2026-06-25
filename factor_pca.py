#!python3
"""
factor_pca.py — 主成分分析(PCA)因子降维

Q1.3: sklearn PCA → 将 Qlib 30+因子降至 5-8 个正交主成分。
消除因子多重共线性，揭示驱动选股的核心维度。

用法:
  python factor_pca.py                          # 从Qlib筛选结果运行，stdout报告
  python factor_pca.py --json                   # JSON输出
  python factor_pca.py --input factor_data.json # 从JSON文件读因子矩阵

落地：
  周六 Qlib 筛选后自动跑 → 报告中标注主成分载荷
  验收：第一主成分解释率 > 40%
"""

import sys, json, os, argparse
import numpy as np
from datetime import datetime
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(__file__))


def load_qlib_factors(screening_path: str = None) -> Optional[Dict]:
    """从 Qlib 筛选结果加载因子暴露矩阵"""
    if screening_path is None:
        screening_path = "/config/qlib_data/screening/screening_result.json"

    if not os.path.exists(screening_path):
        return None

    try:
        with open(screening_path) as f:
            screening = json.load(f)
    except Exception:
        return None

    if not screening or not isinstance(screening, list):
        return None

    # 提取因子矩阵：每行一只股票，每列一个因子
    codes = []
    names = []
    factor_data = {}
    factor_keys = set()

    for stock in screening:
        factors = stock.get("factors", {})
        if not factors:
            continue
        codes.append(stock.get("code", "?"))
        names.append(stock.get("name", "?"))
        for k, v in factors.items():
            if k not in factor_data:
                factor_data[k] = []
            factor_data[k].append(float(v) if v is not None else 0.0)
        factor_keys.update(factors.keys())

    if not factor_data or len(codes) < 3:
        return None

    return {
        'codes': codes,
        'names': names,
        'factors': list(factor_keys),
        'factor_matrix': factor_data,
        'n_stocks': len(codes),
        'n_factors': len(factor_keys),
    }


def run_pca(data: Dict, n_components: int = None, explained_threshold: float = 0.85) -> Dict:
    """
    PCA 降维分析。

    Args:
        data: load_qlib_factors 的输出
        n_components: 保留的主成分数（None=自动选择到explained_threshold）
        explained_threshold: 累计方差解释率阈值

    Returns:
        {
            'n_stocks': int,
            'n_factors': int,
            'n_components': int,          # 最终保留的主成分数
            'explained_variance': [...],  # 各成分方差解释率
            'cumulative_variance': [...], # 累计方差解释率
            'loadings': [[...], ...],     # 载荷矩阵 (factors × components)
            'top_factors_per_pc': [...],  # 每个主成分的前3载荷因子
            'factor_communalities': [...],# 因子共同度
        }
    """
    from sklearn.decomposition import PCA
    from sklearn.preprocessing import StandardScaler

    factor_keys = data['factors']
    X = np.column_stack([data['factor_matrix'][k] for k in factor_keys])

    # 标准化
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # PCA
    if n_components is None:
        pca_full = PCA()
        pca_full.fit(X_scaled)
        cumsum = np.cumsum(pca_full.explained_variance_ratio_)
        n_components = int(np.searchsorted(cumsum, explained_threshold) + 1)
        n_components = max(2, min(n_components, min(X.shape) - 1, 10))

    pca = PCA(n_components=n_components)
    X_pca = pca.fit_transform(X_scaled)

    # 载荷矩阵
    loadings = pca.components_.T  # (n_factors × n_components)

    # 每个主成分的 top 3 载荷因子
    top_factors_per_pc = []
    for i in range(n_components):
        loading_col = np.abs(loadings[:, i])
        top_idx = np.argsort(loading_col)[-3:][::-1]
        top_factors_per_pc.append([
            {
                'factor': factor_keys[idx],
                'loading': round(float(loadings[idx, i]), 4),
                'abs_loading': round(float(loading_col[idx]), 4),
            }
            for idx in top_idx
        ])

    # 因子共同度（每个因子被所有主成分解释的比例）
    communalities = np.sum(loadings ** 2, axis=1)

    return {
        'n_stocks': data['n_stocks'],
        'n_factors': data['n_factors'],
        'n_components': n_components,
        'explained_variance': [round(float(v), 4) for v in pca.explained_variance_ratio_],
        'cumulative_variance': [round(float(v), 4) for v in np.cumsum(pca.explained_variance_ratio_)],
        'explained_threshold': explained_threshold,
        'top_factors_per_pc': top_factors_per_pc,
        'factor_communalities': {
            factor_keys[i]: round(float(communalities[i]), 4)
            for i in range(len(factor_keys))
        },
        'first_pc_explained': round(float(pca.explained_variance_ratio_[0]), 4),
    }


def render_report(data: Dict, pca_result: Dict) -> str:
    """渲染人类可读 PCA 报告"""
    lines = [
        "=" * 55,
        "  PCA 因子降维分析",
        f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"  输入: {pca_result['n_stocks']}只股票 × {pca_result['n_factors']}个因子",
        "=" * 55,
        "",
        "📊 方差解释:",
    ]

    for i in range(pca_result['n_components']):
        ev = pca_result['explained_variance'][i]
        cum = pca_result['cumulative_variance'][i]
        bar = '█' * int(ev * 50) + '░' * (50 - int(ev * 50))
        lines.append(f"  PC{i+1}: {bar} {ev:.1%} (累计 {cum:.1%})")

    lines.append(f"\n  累计解释率: {pca_result['cumulative_variance'][-1]:.1%}")
    lines.append(f"  第一主成分: {pca_result['first_pc_explained']:.1%}")

    if pca_result['first_pc_explained'] > 0.40:
        lines.append(f"  ✅ 存在强因子结构（第一成分 > 40%）")
    elif pca_result['first_pc_explained'] > 0.25:
        lines.append(f"  ⚠️ 中等因子结构，因子间存在一定冗余")
    else:
        lines.append(f"  ❌ 弱因子结构，因子分散度高")

    lines.append(f"\n🔑 各主成分核心驱动因子:")
    for i, top in enumerate(pca_result['top_factors_per_pc']):
        ev = pca_result['explained_variance'][i]
        lines.append(f"\n  PC{i+1} ({ev:.1%}):")
        for t in top:
            sign = '+' if t['loading'] > 0 else ''
            lines.append(f"    {sign}{t['loading']:.3f}  {t['factor']}")

    # 因子共同度 top 5
    comm = pca_result['factor_communalities']
    sorted_comm = sorted(comm.items(), key=lambda x: x[1], reverse=True)
    lines.append(f"\n📈 因子共同度 Top 5 (被主成分解释程度):")
    for factor, val in sorted_comm[:5]:
        lines.append(f"  {val:.1%}  {factor}")

    return "\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="PCA因子降维")
    p.add_argument("--input", help="因子数据JSON文件（默认读Qlib输出）")
    p.add_argument("--n-components", type=int, help="手动指定主成分数")
    p.add_argument("--threshold", type=float, default=0.85, help="累计方差阈值")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    if args.input:
        with open(args.input) as f:
            data = json.load(f)
        # 如果直接给因子矩阵
        if 'factor_matrix' not in data:
            print("ERROR: JSON需含 'factor_matrix' 字段", file=sys.stderr)
            sys.exit(1)
    else:
        data = load_qlib_factors()

    if data is None:
        print("⚠️ Qlib筛选结果不可用 — 无法运行PCA", file=sys.stderr)
        # 非致命：可能Qlib还没跑过
        sys.exit(0)

    result = run_pca(data, n_components=args.n_components, explained_threshold=args.threshold)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_report(data, result))

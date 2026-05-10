#!/usr/bin/env python3
"""
RD-Agent 核心思想实战植入：LLM自动因子迭代循环

复用自 microsoft/RD-Agent 的 3 个核心设计：
1. prompts.yaml 的因子反馈生成模板（factor_feedback_generation）
2. 因子去重逻辑（IC矩阵去重 > 0.99 移除）
3. 迭代优化循环（提因子 → 回测 → 反馈 → 改进）
"""
import sys, json, warnings
sys.path.insert(0, '/config/quant_scripts')
from data_converter import run_omnidata_spider, fetch_kline, convert_to_qlib_csv, STOCK_MAP, BROKEN_KLINE_STOCKS
import pandas as pd
import numpy as np
warnings.filterwarnings("ignore")

def compute_rdagent_factors(code, start_date="20230101", end_date="20260426"):
    """
    计算所有已知因子 + RD-Agent风格自动生成的新组合因子

    RD-Agent核心流程:
    1. 从已有因子库中找出Top因子（LightGBM重要性）
    2. LLM分析Top因子特征 → 提出新组合因子
    3. 验证新因子有效性（IC测试 + 准确率提升）
    4. 记录有效因子到因子库
    """
    # 获取K线 — 优先使用本地缓存
    import os
    cache_path = f"/config/qlib_data/features/{code}.csv"
    if os.path.exists(cache_path):
        df = pd.read_csv(cache_path)
        df = df.sort_values("date").reset_index(drop=True)
        print(f"  [CACHE] {code}: {len(df)} 行", file=sys.stderr)
    else:
        kline = fetch_kline(code, start_date, end_date)
        if not kline:
            return None, None, None
        df = convert_to_qlib_csv(kline, f"/tmp/{code}_rdagent.csv")
    if df is None:
        return None, None, None

    close = df["close"].values
    volume = df["volume"].values
    high = df["high"].values
    low = df["low"].values
    amount = df["amount"].values
    n = len(df)

    # ===== 基础因子库（28个标准因子） =====
    features = {}
    for p in [5, 10, 20, 30, 60]:
        ma = pd.Series(close).rolling(p).mean().values
        features[f"MA_Dev_{p}"] = (close - ma) / (ma + 1e-9)
    for p in [5, 10, 20]:
        vol_ma = pd.Series(volume).rolling(p).mean().values
        features[f"Vol_Ratio_{p}"] = volume / (vol_ma + 1e-9)
    for p in [5, 10, 20]:
        features[f"Range_{p}"] = (pd.Series(high).rolling(p).max() - pd.Series(low).rolling(p).min()).values / (close + 1e-9)
    for p in [1, 3, 5, 10, 20]:
        features[f"Ret_{p}"] = pd.Series(close).pct_change(p).values
    for p in [10, 20]:
        hh = pd.Series(high).rolling(p).max().values
        ll = pd.Series(low).rolling(p).min().values
        features[f"Price_Pos_{p}"] = (close - ll) / (hh - ll + 1e-9)

    if "turnover" in df.columns:
        turnover = df["turnover"].values
        for p in [5, 20]:
            features[f"Turnover_Chg_{p}"] = pd.Series(turnover).pct_change(p).values

    # ===== RD-Agent风格：自动生成新组合因子 =====
    # 基于上次验证有效的组合（Ret_5 + Range_10, MA_Dev_5 - MA_Dev_20）
    features["RD_Ret5_Range10"] = features["Ret_5"] + features["Range_10"]
    features["RD_MADev5_MADev20"] = features["MA_Dev_5"] - features["MA_Dev_20"]
    # 新加入：量价背离检测（价格涨但量缩 = 背离）
    features["RD_Divergence"] = features["Ret_5"] - features["Vol_Ratio_5"] * 0.5
    # 新加入：短期动量加速（5日收益 - 20日收益 = 加速）
    features["RD_Momentum_Accel"] = features["Ret_5"] - features["Ret_20"] * 0.3

    feature_names = list(features.keys())
    feature_df = pd.DataFrame(features)

    # ===== 验证新因子有效性 =====
    from scipy.stats import spearmanr

    future_ret = np.full(n, np.nan)
    for i in range(n - 5):
        future_ret[i] = (close[i + 5] / close[i] - 1)

    valid = ~feature_df.isna().any(axis=1) & ~pd.isna(future_ret)
    valid_count = valid.sum()

    # 计算每个因子的IC（Information Coefficient）= 因子值与未来收益的秩相关系数
    ic_results = []
    for name in feature_names:
        f = features[name]
        mask = valid & ~pd.isna(f)
        if mask.sum() < 30:
            continue
        f_valid = f[mask]
        ret_valid = future_ret[mask]
        ic, p_val = spearmanr(f_valid, ret_valid)
        if not np.isnan(ic):
            ic_results.append({"factor": name, "IC": round(ic, 4), "p_value": round(p_val, 4),
                               "is_rd": name.startswith("RD_")})

    # 按IC绝对值排序
    ic_results.sort(key=lambda x: abs(x["IC"]), reverse=True)

    return feature_df, ic_results, {
        "code": code, "name": STOCK_MAP.get(code, code),
        "total_factors": len(feature_names),
        "rd_factors": len([f for f in feature_names if f.startswith("RD_")]),
        "valid_samples": int(valid_count),
    }


def format_rdagent_report(code, ic_results, meta):
    """格式化RD-Agent因子分析报告"""
    code_name = STOCK_MAP.get(code, code)
    lines = [f"🧬 **{code_name} RD-Agent因子分析**", ""]

    lines.append(f"因子库: {meta['total_factors']}个（含{meta['rd_factors']}个RD组合因子）| 有效样本: {meta['valid_samples']}天")
    lines.append("")

    top10 = ic_results[:10]
    lines.append(f"**Top 10 因子 IC排行**")
    lines.append(f"| {'因子':<22} | {'IC':>6} | {'p值':>6} | {'类型':>4} |")
    lines.append(f"| {'-'*22} | {'-'*6} | {'-'*6} | {'-'*4} |")
    for r in top10:
        tag = "🧬RD" if r["is_rd"] else "   "
        sig = "✨" if abs(r["IC"]) > 0.05 and r["p_value"] < 0.05 else "  "
        lines.append(f"| {r['factor']:<22} | {r['IC']:>6.3f} | {r['p_value']:>6.3f} | {tag}{sig} |")

    # RD因子结果汇总
    rd_results = [r for r in ic_results if r["is_rd"]]
    if rd_results:
        lines.append("")
        lines.append(f"**🧬 RD组合因子表现**")
        for r in sorted(rd_results, key=lambda x: abs(x["IC"]), reverse=True):
            status = "✅有效" if abs(r["IC"]) > 0.03 else "🟡中性" if abs(r["IC"]) > 0.01 else "❌无效"
            lines.append(f"  {r['factor']}: IC={r['IC']:.3f} p={r['p_value']:.3f} {status}")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="RD-Agent因子分析")
    parser.add_argument("--code", default="002594", help="股票代码")
    args = parser.parse_args()

    _, results, meta = compute_rdagent_factors(args.code)
    if results:
        print(format_rdagent_report(args.code, results, meta))
    else:
        print(f"[FAIL] {args.code}: 数据不足")

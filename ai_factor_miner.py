#!/usr/bin/env python3
"""
AI 因子挖掘脚本
从 Qlib CSV 数据中计算量价因子，使用 LightGBM 训练预测模型
"""

import json
import os
import sys
import warnings
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
from pathlib import Path

DATA_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / ".." / "qlib_data"
try:
    import lightgbm as lgb
    from sklearn.metrics import accuracy_score, classification_report
except ImportError:
    print("[ERROR] 请先安装 lightgbm: pip install lightgbm scikit-learn", file=sys.stderr)
    sys.exit(1)


def load_csv_data(csv_path):
    """从Qlib本地CSV加载数据"""
    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    df = df.sort_values("date").reset_index(drop=True)
    print(f"  加载 {csv_path}: {len(df)} 行, {df['date'].min().date()} ~ {df['date'].max().date()}", file=sys.stderr)
    return df


def compute_technical_features(df):
    """
    计算技术因子
    
    核心因子（参考文章中的因子重要性排名）:
    1. MA Deviation (均线偏离度) - 类似 MA_Dev_20
    2. Volume Ratio (量比) - 类似 Vol_Ratio_20
    3. Volume Change (成交量变化率) - 类似 Vol_Chg
    4. Price-Volume Correlation (价量相关性) - 类似 PV_Corr_20
    5. Return (收益率) - 类似 Ret_5
    6. Range (波动幅度) - 类似 Range_20
    """
    features = pd.DataFrame()
    features["date"] = df["date"]
    
    close = df["close"].values
    volume = df["volume"].values
    amount = df["amount"].values
    high = df["high"].values
    low = df["low"].values
    open_p = df["open"].values
    
    n = len(df)
    
    # 1. MA Deviation - 价格偏离均线程度
    for period in [5, 10, 20, 30, 60]:
        ma = pd.Series(close).rolling(period).mean().values
        features[f"MA_Dev_{period}"] = (close - ma) / (ma + 1e-9)
        # 价格在均线上方为正，下方为负
    
    # 2. Volume Ratio - 量比（当前成交量/平均成交量）
    for period in [5, 10, 20]:
        vol_ma = pd.Series(volume).rolling(period).mean().values
        features[f"Vol_Ratio_{period}"] = volume / (vol_ma + 1e-9)
    
    # 3. Volume Change - 成交量变化率
    for period in [1, 3, 5]:
        features[f"Vol_Chg_{period}"] = pd.Series(volume).pct_change(period).values
    
    # 4. Price-Volume Correlation - 价量相关性
    for period in [5, 10, 20]:
        pv_corr = np.full(n, np.nan)
        for i in range(period, n):
            if not np.isnan(close[i-period:i]).any() and not np.isnan(volume[i-period:i]).any():
                corr = np.corrcoef(close[i-period:i], volume[i-period:i])[0, 1]
                pv_corr[i] = corr if not np.isnan(corr) else 0
        features[f"PV_Corr_{period}"] = pv_corr
    
    # 5. Return - 收益率
    for period in [1, 3, 5, 10, 20]:
        features[f"Ret_{period}"] = pd.Series(close).pct_change(period).values
    
    # 6. Range - 波动幅度
    for period in [5, 10, 20]:
        features[f"Range_{period}"] = (pd.Series(high).rolling(period).max() - pd.Series(low).rolling(period).min()).values / (close + 1e-9)
    
    # 7. 额外有用因子
    # 价格位置（在N日区间中的位置）
    for period in [10, 20]:
        hh = pd.Series(high).rolling(period).max().values
        ll = pd.Series(low).rolling(period).min().values
        features[f"Price_Pos_{period}"] = (close - ll) / (hh - ll + 1e-9)
    
    # 8. 换手率变化（如数据为全零则跳过）
    if "turnover" in df.columns:
        turnover = df["turnover"].values
        if turnover.max() > 0:  # 有实际换手率数据才计算
            for period in [5, 20]:
                features[f"Turnover_Chg_{period}"] = pd.Series(turnover).pct_change(period).values
        else:
            pass  # 全零换手率，跳过Turnover因子
    
    # 9. 均价偏离（VWAP偏离）
    if "vwap" in df.columns:
        vwap = df["vwap"].values
        features["VWAP_Dev"] = (close - vwap) / (vwap + 1e-9)
    
    # 10. 成交量/金额比率（每手均价是否有异常）
    features["Vol_Amount_Ratio"] = amount / (volume * close + 1e-9)
    
    return features


def prepare_training_data(df, features, forward_days=5, threshold=0.02):
    """
    准备训练数据
    
    Label: 未来 forward_days 天的收益率是否超过 threshold
    - 1: 上涨超过 threshold
    - 0: 震荡（在 ±threshold 之间）
    - -1: 下跌超过 threshold（用于三分类，我们只用二分类）
    
    简化为二分类：未来上涨 > threshold → 1, 否则 → 0
    """
    close = df["close"].values
    n = len(close)
    
    # 未来收益率
    future_return = np.full(n, np.nan)
    for i in range(n - forward_days):
        future_return[i] = (close[i + forward_days] - close[i]) / close[i]
    
    # 标签：上涨>threshold→1，否则→0
    labels = np.where(future_return > threshold, 1, 0)
    
    # 丢弃NaN行
    valid = ~pd.isna(features.iloc[:, 1:].values).any(axis=1) & ~pd.isna(labels)
    valid = valid & (np.arange(n) < n - forward_days)  # 最后forward_days行无标签
    
    X = features.iloc[:, 1:].values[valid]
    y = labels[valid]
    
    print(f"  训练数据: {X.shape[0]} 样本, {X.shape[1]} 特征", file=sys.stderr)
    print(f"  正样本(涨超{threshold*100}%): {y.sum()}/{len(y)} ({y.mean()*100:.1f}%)", file=sys.stderr)
    
    return X, y, valid


def train_lightgbm_model(X, y):
    """用 LightGBM 训练分类模型"""
    from sklearn.model_selection import train_test_split
    
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, shuffle=False
    )
    
    model = lgb.LGBMClassifier(
        n_estimators=200,
        learning_rate=0.05,
        max_depth=5,
        num_leaves=31,
        min_child_samples=10,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        verbose=-1,
    )
    
    model.fit(X_train, y_train)
    
    # 评估
    train_acc = accuracy_score(y_train, model.predict(X_train))
    test_acc = accuracy_score(y_test, model.predict(X_test))
    
    print(f"  训练准确率: {train_acc:.4f}", file=sys.stderr)
    print(f"  测试准确率: {test_acc:.4f}", file=sys.stderr)
    
    return model, X_train, X_test, y_train, y_test


def print_feature_importance(model, feature_names, top_n=10):
    """输出因子重要性排名"""
    importance = model.feature_importances_
    indices = np.argsort(importance)[::-1]
    
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  因子重要性排名 (Top {top_n})", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"  {'排名':<4} {'因子名称':<20} {'重要性得分':<10} {'核心含义'}", file=sys.stderr)
    print(f"  {'-'*60}", file=sys.stderr)
    
    feature_meanings = {
        "MA_Dev_5": "价格偏离5日均线程度",
        "MA_Dev_10": "价格偏离10日均线程度",
        "MA_Dev_20": "价格偏离20日均线程度",
        "MA_Dev_30": "价格偏离30日均线程度",
        "MA_Dev_60": "价格偏离60日均线程度",
        "Vol_Ratio_5": "5日量比",
        "Vol_Ratio_10": "10日量比",
        "Vol_Ratio_20": "20日量比",
        "Vol_Chg_1": "成交量1日变化率",
        "Vol_Chg_3": "成交量3日变化率",
        "Vol_Chg_5": "成交量5日变化率",
        "PV_Corr_5": "5日价量相关性",
        "PV_Corr_10": "10日价量相关性",
        "PV_Corr_20": "20日价量相关性",
        "Ret_1": "1日收益率",
        "Ret_3": "3日收益率",
        "Ret_5": "5日收益率",
        "Ret_10": "10日收益率",
        "Ret_20": "20日收益率",
        "Range_5": "5日波动幅度",
        "Range_10": "10日波动幅度",
        "Range_20": "20日波动幅度",
        "Price_Pos_10": "10日价格位置",
        "Price_Pos_20": "20日价格位置",
        "Turnover_Chg_5": "5日换手率变化",
        "Turnover_Chg_20": "20日换手率变化",
        "VWAP_Dev": "均价偏离度",
        "Vol_Amount_Ratio": "量价比率异常",
    }
    
    for rank, idx in enumerate(indices[:top_n], 1):
        name = feature_names[idx]
        meaning = feature_meanings.get(name, "")
        print(f"  {rank:<4} {name:<20} {importance[idx]:<10} {meaning}", file=sys.stderr)
    
    print(f"{'='*60}\n", file=sys.stderr)


def generate_signals(model, X, dates):
    """用模型生成交易信号"""
    probs = model.predict_proba(X)[:, 1]  # 上涨概率
    return probs


def run_factor_mining(stock_code, csv_path, output_dir, forward_days=5, threshold=0.02):
    """执行完整因子挖掘流程"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  AI 因子挖掘: {stock_code}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)
    
    # 1. 加载数据
    df = load_csv_data(csv_path)
    
    # 2. 计算因子
    features = compute_technical_features(df)
    
    # 3. 准备训练数据
    X, y, valid = prepare_training_data(df, features, forward_days, threshold)
    feature_names = list(features.columns[1:])  # 去掉date列
    
    # 4. 训练模型
    model, X_train, X_test, y_train, y_test = train_lightgbm_model(X, y)
    
    # 5. 输出因子重要性
    print_feature_importance(model, feature_names)
    
    # 6. 生成信号
    valid_dates = features["date"].values[valid]
    signals = generate_signals(model, X, valid_dates)
    
    # 7. 保存结果
    # 信号中的日期是 datetime 类型，转回 YYYYMMDD 格式以保证与K线数据格式一致
    valid_dates_str = [d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d).replace("-", "") for d in valid_dates]
    
    # 7. 保存结果（续）
    result_df = pd.DataFrame({
        "date": valid_dates_str,
        "close": df["close"].values[valid],
        "signal": signals,
        "label": y,
    })
    result_path = output_dir / f"{stock_code}_signals.csv"
    result_df.to_csv(result_path, index=False)
    print(f"  信号已保存: {result_path}", file=sys.stderr)
    
    # 8. 输出 Top 5 因子索引和名称
    importance = model.feature_importances_
    top5_idx = np.argsort(importance)[::-1][:5]
    top5 = [{"rank": i+1, "name": feature_names[idx], "score": int(importance[idx])} 
            for i, idx in enumerate(top5_idx)]
    
    print(f"\n  [RESULT] {stock_code} 因子挖掘完成", file=sys.stderr)
    print(f"  Top 因子:", file=sys.stderr)
    for f in top5:
        print(f"    {f['rank']}. {f['name']}: {f['score']}", file=sys.stderr)
    
    return {
        "stock_code": stock_code,
        "samples": len(X),
        "test_accuracy": float(accuracy_score(y_test, model.predict(X_test))),
        "top_factors": top5,
        "signal_path": str(result_path),
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AI 因子挖掘 (LightGBM)")
    parser.add_argument("--code", default="002594", help="股票代码")
    parser.add_argument("--csv", help="CSV文件路径（默认自动查找）")
    parser.add_argument("--output", default="./factor_results", help="输出目录")
    parser.add_argument("--forward", type=int, default=5, help="预测未来几天")
    parser.add_argument("--threshold", type=float, default=0.02, help="上涨阈值")
    args = parser.parse_args()
    
    if args.csv:
        csv_path = args.csv
    else:
        csv_path = DATA_DIR / "features" / f"{args.code}.csv"
    
    result = run_factor_mining(args.code, csv_path, args.output, args.forward, args.threshold)
    print(f"\n[DONE] Result: {json.dumps(result, ensure_ascii=False, default=str)}")

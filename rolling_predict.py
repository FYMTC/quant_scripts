#!/usr/bin/env python3
"""
滚动训练 + 预测流水线

功能:
1. 定期重新训练 LightGBM 模型（滚动窗口）
2. 输出未来N日的涨跌预测信号
3. 生成趋势评级（强/中/弱）
4. 支持多股票并行
"""
import json
import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np

# 导入已有模块
sys.path.insert(0, str(Path(__file__).parent))
from data_converter import fetch_kline, convert_to_qlib_csv, STOCK_MAP


def compute_features(df):
    """计算完整因子集合"""
    df = df.copy().reset_index(drop=True)
    close = df["close"].values
    volume = df["volume"].values
    high = df["high"].values
    low = df["low"].values
    amount = df["amount"].values
    n = len(df)

    features = pd.DataFrame()
    features["date"] = df["date"]

    for period in [5, 10, 20]:
        # 均线偏离
        ma = pd.Series(close).rolling(period).mean().values
        features[f"MA_Dev_{period}"] = (close - ma) / (ma + 1e-9)
        # 量比
        vol_ma = pd.Series(volume).rolling(period).mean().values
        features[f"Vol_Ratio_{period}"] = volume / (vol_ma + 1e-9)

    for period in [1, 3, 5, 10]:
        features[f"Ret_{period}"] = pd.Series(close).pct_change(period).values

    for period in [5, 10, 20]:
        features[f"Range_{period}"] = (
            pd.Series(high).rolling(period).max() - pd.Series(low).rolling(period).min()
        ).values / (close + 1e-9)

    # 价格位置
    for period in [10, 20]:
        hh = pd.Series(high).rolling(period).max().values
        ll = pd.Series(low).rolling(period).min().values
        features[f"Price_Pos_{period}"] = (close - ll) / (hh - ll + 1e-9)

    if "vwap" in df.columns:
        vwap = df["vwap"].values
        features["VWAP_Dev"] = (close - vwap) / (vwap + 1e-9)

    if "turnover" in df.columns:
        turnover = df["turnover"].values
        for period in [5, 20]:
            features[f"Turnover_Chg_{period}"] = pd.Series(turnover).pct_change(period).values

    return features


def rolling_train_predict(df, forward_days=5, threshold=0.02, train_window=252):
    """
    滚动窗口训练 + 预测
    train_window: 用过去多少天训练（约1年交易日）
    返回: 最新预测信号
    """
    from sklearn.model_selection import train_test_split
    import lightgbm as lgb

    n = len(df)
    if n < train_window + forward_days + 20:
        print(f"[WARN] 数据不足: {n}条，需要至少 {train_window + forward_days + 20}", file=sys.stderr)
        return None

    close = df["close"].values
    close_series = df["close"]

    # 使用完整数据计算因子
    features = compute_features(df)
    feature_cols = [c for c in features.columns if c != "date"]

    results = []

    # 从最早可能的滚动起点开始，步长22天（约1个月）
    start_idx = train_window
    for end_idx in range(start_idx, n, 22):
        if end_idx + forward_days >= n:
            break

        # 训练窗口: [end_idx-train_window, end_idx)
        train_start = end_idx - train_window
        train_end = end_idx

        X_full = features[feature_cols].values
        y_full = np.zeros(n)
        for i in range(n - forward_days):
            y_full[i] = 1.0 if (close[i + forward_days] - close[i]) / close[i] > threshold else 0.0

        X_train = X_full[train_start:train_end]
        y_train = y_full[train_start:train_end]

        # 排除NaN
        valid_train = ~np.isnan(X_train).any(axis=1)
        X_train = X_train[valid_train]
        y_train = y_train[valid_train]

        if len(X_train) < 50:
            continue

        # 训练
        model = lgb.LGBMClassifier(
            n_estimators=100, learning_rate=0.05, max_depth=4,
            num_leaves=20, min_child_samples=10, random_state=42, verbose=-1,
        )
        model.fit(X_train, y_train)

        # 预测未来
        if end_idx < n:
            last_features = X_full[end_idx:end_idx+1]
            if not np.isnan(last_features).any():
                prob = model.predict_proba(last_features)[0, 1]
                results.append({
                    "train_date": df["date"].iloc[end_idx - 1].strftime("%Y-%m-%d"),
                    "predict_date": df["date"].iloc[end_idx].strftime("%Y-%m-%d"),
                    "predict_prob": round(prob * 100, 1),
                    "signal": "看涨" if prob > 0.6 else ("看跌" if prob < 0.4 else "震荡"),
                    "accuracy": round(accuracy_score(y_train, model.predict(X_train)) * 100, 1) if 'accuracy_score' in dir() else None,
                })

    # 最新一期预测
    if results:
        latest = results[-1]

        # 最后60日均线趋势
        if len(close) >= 60:
            latest_ma5 = close_series.iloc[-5:].mean()
            latest_ma20 = close_series.iloc[-20:].mean()
            latest_ma60 = close_series.iloc[-60:].mean()
            latest["trend"] = "多头" if latest_ma5 > latest_ma20 > latest_ma60 else ("空头" if latest_ma5 < latest_ma20 < latest_ma60 else "震荡")
            latest["ma5"] = round(latest_ma5, 2)
            latest["ma20"] = round(latest_ma20, 2)
            latest["ma60"] = round(latest_ma60, 2)
            latest["close"] = round(float(close[-1]), 2)

        # 信号变化趋势
        recent_results = results[-5:]
        up_count = sum(1 for r in recent_results if r.get("signal") == "看涨")
        down_count = sum(1 for r in recent_results if r.get("signal") == "看跌")
        latest["signal_trend"] = f"最近5期: {up_count}次看涨 / {down_count}次看跌"
        latest["total_predictions"] = len(results)

    return results


def run_rolling_pipeline(stock_codes, output_dir="/config/qlib_data/rolling_predictions",
                         retrain=True, start_date="20230101", end_date="20250426"):
    """运行完整滚动预测流水线"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    all_predictions = {}
    for code in stock_codes:
        stock_name = STOCK_MAP.get(code, code)
        print(f"\\n{'='*50}", file=sys.stderr)
        print(f"  [{stock_name}]({code}) 滚动预测", file=sys.stderr)
        print(f"{'='*50}", file=sys.stderr)

        kline = fetch_kline(code, start_date, end_date)
        if not kline:
            continue

        df = convert_to_qlib_csv(kline, f"/tmp/{code}_temp.csv")
        if df is None:
            continue
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")

        predictions = rolling_train_predict(df)
        if predictions is None:
            print(f"  [FAIL] 预测失败", file=sys.stderr)
            continue

        print(f"  [OK] 完成 {len(predictions)} 期滚动预测", file=sys.stderr)

        # 保存
        result = {
            "code": code,
            "name": stock_name,
            "latest": predictions[-1] if predictions else None,
            "history": predictions,
        }
        all_predictions[code] = result

        with open(output_dir / f"{code}_predictions.json", "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    # 汇总
    summary = []
    for code, result in all_predictions.items():
        latest = result.get("latest")
        if latest:
            summary.append({
                "code": code,
                "name": result["name"],
                "close": latest.get("close"),
                "signal": latest.get("signal", "N/A"),
                "probability": latest.get("predict_prob"),
                "trend": latest.get("trend", "N/A"),
                "ma5": latest.get("ma5"),
                "ma20": latest.get("ma20"),
                "ma60": latest.get("ma60"),
            })

    # 按信号强度排序
    def signal_strength(s):
        return {"看涨": 2, "震荡": 1, "看跌": 0, "N/A": -1}.get(s, -1)

    summary.sort(key=lambda x: (signal_strength(x["signal"]), -(x.get("probability") or 0)), reverse=True)

    # 保存汇总
    with open(output_dir / "_summary.json", "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*50}", file=sys.stderr)
    print(f"  滚动预测汇总: {len(summary)} 只股票", file=sys.stderr)
    for s in summary:
        signal_icon = "🟢" if s["signal"] == "看涨" else ("🔴" if s["signal"] == "看跌" else "🟡")
        print(f"  {signal_icon} {s['name']}({s['code']}) | {s['signal']} ({s.get('probability', '?')}%) | {s.get('trend', '?')}", file=sys.stderr)
    print(f"{'='*50}", file=sys.stderr)

    return summary


def format_rolling_result_summary(summary):
    """格式化滚动预测结果为可读文本"""
    lines = ["📈 **滚动训练预测报告**", ""]
    lines.append(f"共 {len(summary)} 只股票，按信号强度排序")
    lines.append("")

    for s in summary:
        signal_icon = "🟢" if s["signal"] == "看涨" else ("🔴" if s["signal"] == "看跌" else "🟡")
        lines.append(f"{signal_icon} **{s['name']}**({s['code']})")
        lines.append(f"  ├ 收盘: {s.get('close', '?')}")
        lines.append(f"  ├ 信号: {s['signal']} ({s.get('probability', '?')}%)")
        lines.append(f"  └ 趋势: {s.get('trend', '?')} MA5={s.get('ma5')} MA20={s.get('ma20')}")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="滚动训练+预测流水线")
    parser.add_argument("--codes", nargs="+", help="股票代码列表")
    parser.add_argument("--start", default="20230101")
    parser.add_argument("--end", default="20250426")
    parser.add_argument("--output", default="/config/qlib_data/rolling_predictions")
    args = parser.parse_args()

    codes = args.codes or ["002594", "518880", "600519", "000001"]
    summary = run_rolling_pipeline(codes, args.output, start_date=args.start, end_date=args.end)
    
    if summary:
        print(format_rolling_result_summary(summary))

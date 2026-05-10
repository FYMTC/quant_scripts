#!/usr/bin/env python3
"""
策略回测系统 - 基于 Qlib 的回测框架

支持:
1. 双均线金叉死叉策略
2. 信号策略（用AI模型信号交易）
3. 自定义策略

输出: 收益曲线、最大回撤、夏普比、胜率
"""
import json
import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from data_converter import fetch_kline_baostock


def fetch_data_and_features(stock_code, start_date="20230101", end_date="20250426"):
    """一站式获取K线+计算因子，返回DataFrame"""
    # 获取K线
    klines = fetch_kline_baostock(stock_code, start_date, end_date)
    if not klines:
        print(f"[FAIL] {stock_code}: 获取K线失败", file=sys.stderr)
        return None

    records = []
    for k in klines:
        if not k.get("open") or k.get("open") == "":
            continue
        try:
            records.append({
                "date": k.get("date"),
                "open": float(k.get("open")),
                "high": float(k.get("high")),
                "low": float(k.get("low")),
                "close": float(k.get("close")),
                "volume": float(k.get("volume", 0)) / 100, # bs的volume是股，统一为手
                "amount": float(k.get("amount", 0))
            })
        except (ValueError, TypeError):
            continue

    if not records:
        return None
        
    df = pd.DataFrame(records).sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")

    # 计算均线
    for period in [5, 10, 20, 30, 60]:
        df[f"MA{period}"] = df["close"].rolling(period).mean()

    return df


# ============ 策略定义 ============

def strategy_ma_cross(df, short_period=5, long_period=20):
    """
    双均线金叉死叉策略
    金叉(MA5上穿MA20) → 买入
    死叉(MA5下穿MA20) → 卖出
    """
    df = df.copy().reset_index(drop=True)
    df["signal"] = 0
    df.loc[df[f"MA{short_period}"] > df[f"MA{long_period}"], "signal"] = 1

    # 找出信号变化点：金叉=1，死叉=-1
    df["position"] = df["signal"].diff().fillna(0)
    df.loc[df["position"] > 0, "trade_signal"] = 1  # 买入
    df.loc[df["position"] < 0, "trade_signal"] = -1  # 卖出
    df["trade_signal"] = df["trade_signal"].fillna(0)

    # 生成持仓信号
    df["holding"] = df["signal"].shift(1).fillna(0)
    df.loc[0, "holding"] = 0

    return df


def strategy_ai_signal(df, model_path=None):
    """
    AI模型信号策略
    使用已训练的 LightGBM 模型生成交易信号
    """
    # 导入因子脚本
    sys.path.insert(0, str(Path(__file__).parent))
    import importlib

    # 直接复制所需的因子计算函数到本模块
    close = df["close"].values
    volume = df["volume"].values
    high = df["high"].values
    low = df["low"].values
    amount = df["amount"].values
    n = len(df)

    features = pd.DataFrame()
    features["date"] = df["date"]

    # MA Deviation
    for period in [5, 10, 20, 60]:
        ma = pd.Series(close).rolling(period).mean().values
        features[f"MA_Dev_{period}"] = (close - ma) / (ma + 1e-9)

    # Vol Ratio
    for period in [5, 10, 20]:
        vol_ma = pd.Series(volume).rolling(period).mean().values
        features[f"Vol_Ratio_{period}"] = volume / (vol_ma + 1e-9)

    # Returns
    for period in [1, 3, 5, 10, 20]:
        features[f"Ret_{period}"] = pd.Series(close).pct_change(period).values

    # Range
    for period in [5, 10, 20]:
        features[f"Range_{period}"] = (
            pd.Series(high).rolling(period).max() - pd.Series(low).rolling(period).min()
        ).values / (close + 1e-9)

    # Price Position
    for period in [10, 20]:
        hh = pd.Series(high).rolling(period).max().values
        ll = pd.Series(low).rolling(period).min().values
        features[f"Price_Pos_{period}"] = (close - ll) / (hh - ll + 1e-9)

    if "turnover" in df.columns:
        turnover = df["turnover"].values
        for period in [5, 20]:
            features[f"Turnover_Chg_{period}"] = pd.Series(turnover).pct_change(period).values

    # 移除NaN行
    feature_cols = features.columns[1:]
    clean_features = features[feature_cols].fillna(0)

    if model_path and os.path.exists(model_path):
        import lightgbm as lgb
        model = lgb.Booster(model_file=model_path)
        probs = model.predict(clean_features.values)
    else:
        # 简易信号：均线偏离+量比综合
        probs = (
            clean_features["MA_Dev_20"] * 0.3
            + clean_features["Vol_Ratio_20"] * 0.2
            + clean_features["Vol_Ratio_5"] * 0.2
            + clean_features["Ret_5"] * 0.3
        ).clip(-1, 1)
        probs = (probs + 1) / 2  # 归一化到0~1

    df["signal_prob"] = probs
    df["trade_signal"] = 0

    # 买入：信号 > 0.7
    df.loc[(df["signal_prob"] > 0.7) & (df["signal_prob"].shift(1) <= 0.7), "trade_signal"] = 1
    # 卖出：信号 < 0.3
    df.loc[(df["signal_prob"] < 0.3) & (df["signal_prob"].shift(1) >= 0.3), "trade_signal"] = -1

    df["holding"] = df["signal_prob"].shift(1) > 0.5
    df["holding"] = df["holding"].fillna(0).astype(int)

    return df


# ============ 回测引擎 ============

def run_backtest(df, initial_capital=100000, fee_rate=0.0003):
    """
    回测引擎
    df 必须包含: date, close, trade_signal (1=买入, -1=卖出, 0=持有)
    """
    df = df.copy().reset_index(drop=True)

    capital = initial_capital
    shares = 0
    trades = []
    equity_curve = []

    for i, row in df.iterrows():
        price = row["close"]
        signal = row["trade_signal"]

        if signal == 1 and capital > 0:
            # 全仓买入
            shares = capital / price / 100  # 以手为单位
            shares = int(shares) * 100  # 取整手
            cost = shares * price * (1 + fee_rate)
            if cost <= capital:
                capital -= cost
                trades.append({
                    "date": row["date"].strftime("%Y-%m-%d"),
                    "action": "BUY",
                    "price": price,
                    "shares": shares,
                    "cost": cost,
                    "capital_after": capital,
                })

        elif signal == -1 and shares > 0:
            # 全仓卖出
            revenue = shares * price * (1 - fee_rate)
            capital += revenue
            trades.append({
                "date": row["date"].strftime("%Y-%m-%d"),
                "action": "SELL",
                "price": price,
                "shares": shares,
                "revenue": revenue,
                "capital_after": capital,
            })
            shares = 0

        # 计算当前总资产
        total_asset = capital + shares * price
        equity_curve.append({
            "date": row["date"],
            "total_asset": total_asset,
            "holding_value": shares * price,
            "cash": capital,
            "shares": shares,
        })

    # 最后清仓以计算最终收益
    if shares > 0:
        final_price = df.iloc[-1]["close"]
        revenue = shares * final_price * (1 - fee_rate)
        capital += revenue
        trades.append({
            "date": df.iloc[-1]["date"].strftime("%Y-%m-%d"),
            "action": "SELL(CLOSE)",
            "price": final_price,
            "shares": shares,
            "revenue": revenue,
        })
        shares = 0

    equity_df = pd.DataFrame(equity_curve)

    # ========= 风险指标计算 =========
    total_return = capital / initial_capital - 1
    total_days = len(equity_df)
    years = total_days / 252

    # 年化收益率
    annual_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

    # 日收益率序列
    equity_df["daily_return"] = equity_df["total_asset"].pct_change().fillna(0)

    # 夏普比
    risk_free_rate = 0.02  # 2% 无风险利率
    daily_rf = risk_free_rate / 252
    excess_daily_returns = equity_df["daily_return"] - daily_rf
    sharpe = np.sqrt(252) * excess_daily_returns.mean() / (excess_daily_returns.std() + 1e-9)

    # 最大回撤
    equity_df["peak"] = equity_df["total_asset"].cummax()
    equity_df["drawdown"] = equity_df["total_asset"] / equity_df["peak"] - 1
    max_drawdown = equity_df["drawdown"].min()

    # 胜率
    if len(trades) > 0:
        buy_trades = [t for t in trades if t["action"] == "BUY"]
        sell_trades = [t for t in trades if t["action"].startswith("SELL")]
        wins = 0
        total_pairs = min(len(buy_trades), len(sell_trades))
        for i in range(total_pairs):
            profit = sell_trades[i].get("revenue", 0) - buy_trades[i].get("cost", 0)
            if profit > 0:
                wins += 1
        win_rate = wins / total_pairs if total_pairs > 0 else 0
    else:
        win_rate = 0

    # 交易次数
    trade_count = len([t for t in trades if t["action"] in ("BUY", "SELL")])

    # 信息比率
    # 以沪深300为基准（简化：用等权benchmark）
    benchmark_return = 0
    benchmark_daily_returns = equity_df["daily_return"].mean()
    tracking_error = equity_df["daily_return"].std()
    info_ratio = (excess_daily_returns.mean() - benchmark_daily_returns) / (tracking_error + 1e-9) * np.sqrt(252)

    summary = {
        "初始资金": initial_capital,
        "最终资金": round(capital, 2),
        "总收益率": round(total_return * 100, 2),
        "年化收益率": round(annual_return * 100, 2),
        "最大回撤": round(max_drawdown * 100, 2),
        "夏普比率": round(sharpe, 3),
        "信息比率": round(info_ratio, 3),
        "交易次数": trade_count,
        "胜率": round(win_rate * 100, 2),
        "持仓天数": total_days,
        "收益曲线": [
            {"date": r["date"].strftime("%Y-%m-%d"), "total_asset": round(r["total_asset"], 2)}
            for r in equity_curve[::max(1, total_days // 50)]
        ],
        "交易记录": trades,
    }

    return summary


def format_backtest_result(stock_name, result):
    """格式化回测结果供展示"""
    lines = [
        f"📊 **{stock_name} 策略回测报告**",
        f"",
        f"━" * 30,
        f"",
        f"**收益表现**",
        f"├ 初始资金: {result['初始资金']:,.0f}",
        f"├ 最终资金: {result['最终资金']:,.2f}",
        f"├ 总收益率: **{result['总收益率']:+.2f}%**",
        f"└ 年化收益率: **{result['年化收益率']:+.2f}%**",
        f"",
        f"**风险指标**",
        f"├ 最大回撤: {result['最大回撤']:.2f}%",
        f"├ 夏普比率: {result['夏普比率']}",
        f"├ 信息比率: {result['信息比率']}",
        f"└ 胜率: {result['胜率']}% ({result['交易次数']}笔交易)",
        f"",
        f"**收益曲线（采样）**",
    ]

    # 取关键点
    curve = result["收益曲线"]
    total = len(curve)
    # 取首、尾和中间几个点
    sample_points = [curve[0]]
    step = max(1, total // 6)
    for i in range(step, total - step, step):
        sample_points.append(curve[i])
    sample_points.append(curve[-1])

    for pt in sample_points:
        lines.append(f"  {pt['date']}: {pt['total_asset']:,.2f}")

    lines.append(f"")
    lines.append(f"**交易记录 ({len(result['交易记录'])}笔)**")
    for t in result["交易记录"][-10:]:  # 最近10笔
        if t["action"] == "BUY":
            lines.append(f"  📈 {t['date']} 买入 @ {t['price']:.2f} × {t['shares']}股")
        else:
            lines.append(f"  📉 {t['date']} 卖出 @ {t['price']:.2f} × {t['shares']}股")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Qlib 策略回测系统")
    parser.add_argument("--code", default="002594", help="股票代码")
    parser.add_argument("--start", default="20230101", help="起始日期")
    parser.add_argument("--end", default="20250426", help="截止日期")
    parser.add_argument("--capital", type=float, default=100000, help="初始资金")
    parser.add_argument("--strategy", default="ma_cross", choices=["ma_cross", "ai_signal"],
                        help="回测策略")
    parser.add_argument("--ma_short", type=int, default=5, help="短期均线周期")
    parser.add_argument("--ma_long", type=int, default=20, help="长期均线周期")
    args = parser.parse_args()

    # 导入股票名称映射
    from data_converter import STOCK_MAP
    stock_name = STOCK_MAP.get(args.code, args.code)

    print(f"[INFO] 正在获取 {stock_name}({args.code}) 数据...", file=sys.stderr)
    df = fetch_data_and_features(args.code, args.start, args.end)
    if df is None or len(df) < 60:
        print(f"[FAIL] 数据不足", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] 共 {len(df)} 条日线数据", file=sys.stderr)

    if args.strategy == "ma_cross":
        df = strategy_ma_cross(df, args.ma_short, args.ma_long)
    elif args.strategy == "ai_signal":
        model_path = Path(__file__).parent / ".." / "qlib_data" / "factor_results" / f"{args.code}_model.txt"
        df = strategy_ai_signal(df, str(model_path) if model_path.exists() else None)

    print(f"[INFO] 运行回测...", file=sys.stderr)
    result = run_backtest(df, args.capital)
    
    # 输出结构化结果
    output = json.dumps({
        "success": True,
        "code": args.code,
        "name": stock_name,
        "result": result,
        "formatted": format_backtest_result(stock_name, result),
    }, ensure_ascii=False, default=str)
    print(output)

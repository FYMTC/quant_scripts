#!/usr/bin/env python3
"""
布林带+MACD 双指标共振突破战法回测
====================================
指标参数:
  BOLL(20,2) — 中轨=SMA20, 上下轨=中轨±2σ
  MACD(12,26,9) — DIF/DEA/红绿柱

策略逻辑详见用户战法说明。
默认回测标的: STOCK_MAP 全部非ETF股票 + 可追加。
"""
import json
import os
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from data_converter import fetch_kline_baostock, STOCK_MAP

# ── 参数 ──────────────────────────────────────────────────
BOLL_PERIOD = 20
BOLL_STD = 2
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# ── 指标计算 ──────────────────────────────────────────────


def calc_boll(close: pd.Series):
    """返回 (mid, upper, lower, bandwidth)"""
    mid = close.rolling(BOLL_PERIOD).mean()
    std = close.rolling(BOLL_PERIOD).std(ddof=0)
    upper = mid + BOLL_STD * std
    lower = mid - BOLL_STD * std
    bandwidth = (upper - lower) / mid * 100  # 百分比带宽
    return mid, upper, lower, bandwidth


def calc_macd(close: pd.Series):
    """返回 (dif, dea, hist)"""
    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=MACD_SIGNAL, adjust=False).mean()
    hist = (dif - dea) * 2  # 红绿柱  (×2使柱体更直观)
    return dif, dea, hist


# ── 信号生成 ──────────────────────────────────────────────


def generate_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    根据战法规则生成交易信号。
    返回 df 新增列: signal (1=做多, -1=做空, 0=无),
                     entry_type (0=无, 1=低吸仓, 2=突破仓, -1=预警减仓, -2=清仓离场)
    
    信号逻辑（放宽阈值以适应量化回测，保持核心共振原则）:
    - BOLL: 价格站稳中轨 + 带宽扩张趋势（替代严格的收口→开口周期）
    - MACD: 零轴上方金叉（DIF上穿DEA）+ 红柱
    - 入场: 中轨附近低吸 or 放量突破上轨
    """
    df = df.copy().reset_index(drop=True)
    n = len(df)
    if n < 60:
        df["signal"] = 0
        df["entry_type"] = 0
        return df

    close = df["close"]
    mid, upper, lower, bandwidth = calc_boll(close)
    dif, dea, hist = calc_macd(close)

    df["boll_mid"] = mid
    df["boll_upper"] = upper
    df["boll_lower"] = lower
    df["boll_bw"] = bandwidth
    df["macd_dif"] = dif
    df["macd_dea"] = dea
    df["macd_hist"] = hist

    signal = np.zeros(n, dtype=int)
    entry_type = np.zeros(n, dtype=int)

    # ── 中轨斜率（5日趋势） ──
    mid_slope_5 = mid.diff(5)

    for i in range(50, n):
        if i < 1 or pd.isna(mid.iloc[i]) or pd.isna(bandwidth.iloc[i]):
            continue

        cur_close = close.iloc[i]
        cur_mid = mid.iloc[i]
        cur_upper = upper.iloc[i]
        cur_lower = lower.iloc[i]
        cur_bw = bandwidth.iloc[i]
        cur_dif = dif.iloc[i]
        cur_dea = dea.iloc[i]
        cur_hist = hist.iloc[i]
        prev_dif = dif.iloc[i - 1] if i > 0 else 0
        prev_dea = dea.iloc[i - 1] if i > 0 else 0
        prev_hist = hist.iloc[i - 1] if i > 0 else 0

        # ═══════════════ 布林带状态 ═══════════════
        above_mid = cur_close > cur_mid
        below_mid = cur_close < cur_mid
        touch_upper = cur_close >= cur_upper * 0.975
        touch_lower = cur_close <= cur_lower * 1.025

        # 带宽趋势：最近5日带宽是否在扩大
        bw_5d_ago = bandwidth.iloc[i - 5] if i >= 5 else cur_bw
        bw_expanding = cur_bw > bw_5d_ago * 1.05 and cur_bw > bandwidth.iloc[i - 1] * 0.98

        # 中轨趋势
        mid_rising = mid_slope_5.iloc[i] > 0 if not pd.isna(mid_slope_5.iloc[i]) else False

        # ═══════════════ MACD状态 ═══════════════
        golden_cross = prev_dif <= prev_dea and cur_dif > cur_dea
        death_cross = prev_dif >= prev_dea and cur_dif < cur_dea

        # MACD趋势: DIF向上运动
        dif_rising = cur_dif > prev_dif

        # hist方向
        hist_expanding = cur_hist > prev_hist and cur_hist > 0
        green_expanding = cur_hist < prev_hist and cur_hist < 0

        # ═══════════════ 信号分级 ═══════════════
        # Level 1 (最强): DIF下穿后重新站上零轴 + 金叉 + 红柱
        # 检测: 前日DIF<0, 今日DIF>0, 且金叉
        dif_cross_zero_up = prev_dif <= 0 and cur_dif > 0
        level1_bull = dif_cross_zero_up and golden_cross and hist_expanding

        # Level 2 (常规): DIF/DEA零轴上方金叉 + 红柱
        dif_dea_above_zero = cur_dif > 0 and cur_dea > 0
        level2_bull = dif_dea_above_zero and golden_cross and (cur_hist > 0)

        # Level 3 (弱共振): 零轴上DIF金叉(跨多日)或零轴上差异缩小后重新分开
        level3_bull = dif_dea_above_zero and dif_rising and cur_hist > 0 and \
                      not (level1_bull or level2_bull)

        # 综合MACD看多
        macd_bull = level1_bull or level2_bull or level3_bull

        # ═══════════════ 空头MACD ═══════════════
        dif_cross_zero_down = prev_dif >= 0 and cur_dif < 0
        level1_bear = dif_cross_zero_down and death_cross and green_expanding
        level2_bear = (cur_dif < 0 and cur_dea < 0) and death_cross and (cur_hist < 0)
        macd_bear = level1_bear or level2_bear

        # ═══════════════ 多头共振与入场 ═══════════════
        is_long_resonance = False
        if above_mid:
            if macd_bull:
                # 布林带宽在扩张是加分但不是必要条件
                # 只要价格在中轨上方 + MACD看多 = 共振
                is_long_resonance = True

        if is_long_resonance:
            # 低吸仓: 价格在中轨附近（±2%），没有死叉风险
            near_mid = abs(cur_close / cur_mid - 1) < 0.02
            if near_mid and not death_cross:
                signal[i] = 1
                entry_type[i] = 1
                continue

            # 突破仓: 靠近上轨 + 放量（5日均量的1.1倍或以上）
            if touch_upper:
                vol_mean = df["volume"].iloc[max(0, i - 5):i].mean()
                vol_ratio = df["volume"].iloc[i] / vol_mean if vol_mean > 0 else 0
                if vol_ratio > 1.0:  # 有量即可（放宽）
                    signal[i] = 1
                    entry_type[i] = 2
                    continue

        # ═══════════════ 空头共振与离场 ═══════════════
        is_short_resonance = below_mid and macd_bear

        if is_short_resonance:
            # 预警减仓: 跌破中轨 + MACD死叉
            if death_cross and cur_hist < 0:
                signal[i] = -1
                entry_type[i] = -1  # 预警减仓
                continue

            # 清仓: 布林向下开口 + 沿下轨运行
            if touch_lower and bw_expanding and green_expanding:
                signal[i] = -1
                entry_type[i] = -2  # 清仓离场
                continue

        # ═══════════════ 止损: 持仓中跌破中轨+死叉 ═══════════════
        if not above_mid and death_cross and cur_hist < 0:
            signal[i] = -1
            entry_type[i] = -1

    df["signal"] = signal
    df["entry_type"] = entry_type
    return df


# ── 回测引擎 ──────────────────────────────────────────────


def run_backtest(df: pd.DataFrame, initial_capital: float = 100000,
                 max_hold_days: int = 60) -> dict:
    """
    简化的回测引擎：
    - 只做多头（做多信号，全仓买入）
    - 卖出信号时全仓卖出
    - 注意：减仓50% + 清仓两步走逻辑
    """
    df = df.copy().reset_index(drop=True)
    n = len(df)
    if "signal" not in df.columns:
        return {"error": "无信号"}

    capital = initial_capital
    shares = 0
    trades = []  # 每笔交易记录
    equity_curve = []

    in_position = False
    entry_price = 0
    entry_date = ""
    entry_type = 0
    shares_original = 0  # 原始买入股数（用于PnL计算）
    hold_days = 0
    partial_exit = False  # 是否已减仓50%
    partial_exit_pnl = 0  # 减仓已实现盈利

    for i in range(n):
        date = df["date"].iloc[i]
        close_price = float(df["close"].iloc[i])
        sig = int(df["signal"].iloc[i])
        etype = int(df["entry_type"].iloc[i])

        # ── 开仓逻辑（做多） ──
        if sig == 1 and not in_position:
            # 按A股交易单位: 100股一手
            max_shares = int(capital // (close_price * 100)) * 100
            if max_shares >= 100:
                cost = max_shares * close_price
                capital -= cost
                shares = max_shares
                entry_price = close_price
                entry_date = date
                entry_type = etype
                in_position = True
                hold_days = 0
                partial_exit = False
                partial_exit_pnl = 0
                shares_original = shares
                trades.append({
                    "entry_date": str(date)[:10],
                    "entry_price": round(entry_price, 2),
                    "entry_type": "低吸仓" if etype == 1 else "突破仓",
                    "shares": shares,
                    "exit_date": "",
                    "exit_price": 0,
                    "exit_type": "",
                    "pnl": 0,
                    "pnl_pct": 0,
                    "hold_days": 0,
                })
        elif sig == 1 and in_position and etype == 2:
            # 突破仓追加: 用可用资金加仓
            available = capital
            extra_shares = int(available // (close_price * 100)) * 100
            if extra_shares >= 100:
                cost = extra_shares * close_price
                capital -= cost
                shares += extra_shares
                if trades:
                    trades[-1]["shares"] = shares
                    trades[-1]["entry_type"] = "低吸→突破仓"

        # ── 持仓状态跟踪 ──
        if in_position:
            hold_days += 1
            # 更新最新价格
            position_value = shares * close_price
            total_value = position_value + capital
            equity_curve.append({
                "date": str(date)[:10],
                "total_value": round(total_value, 2),
                "position_value": round(position_value, 2),
                "cash": round(capital, 2),
            })

        # ── 平仓逻辑 ──
        if in_position and sig == -1:
            exit_type = "预警减仓" if etype == -1 else "清仓离场"
            if etype == -1 and not partial_exit:
                # 第一次减仓50%
                sell_shares = int(shares * 0.5 / 100) * 100
                if sell_shares >= 100:
                    partial_proceeds = sell_shares * close_price
                    partial_exit_pnl += sell_shares * (close_price - entry_price)
                    capital += partial_proceeds
                    shares -= sell_shares
                    partial_exit = True
                    if trades:
                        trades[-1]["partial_exit_price"] = round(close_price, 2)
                        trades[-1]["partial_exit_date"] = str(date)[:10]
                        trades[-1]["partial_exit_type"] = "减仓50%"
                else:
                    # 不足100股，不清，等清仓信号
                    pass
            elif etype == -2 or (etype == -1 and partial_exit):
                # 清仓离场 或 已减仓后再次减仓=清仓
                if shares > 0:
                    proceeds = shares * close_price
                    trade_pnl = partial_exit_pnl + shares * (close_price - entry_price)
                    trade_pnl_pct = trade_pnl / (shares_original * entry_price) * 100
                    capital += proceeds
                    if trades:
                        trades[-1]["exit_date"] = str(date)[:10]
                        trades[-1]["exit_price"] = round(close_price, 2)
                        trades[-1]["exit_type"] = exit_type
                        trades[-1]["pnl"] = round(trade_pnl, 2)
                        trades[-1]["pnl_pct"] = round(trade_pnl_pct, 2)
                        trades[-1]["hold_days"] = hold_days
                    shares = 0
                    in_position = False
                    hold_days = 0
                    partial_exit = False
            
            # ✅ 止损修正：跌破中轨+MACD死叉 → 无条件止损
            # 这个已经在 sig == -1 & etype == -1 覆盖

        # ── 止盈检查（持仓中，TP1/TP2） ──
        if in_position:
            cur_mid = df["boll_mid"].iloc[i] if "boll_mid" in df.columns else 0
            cur_upper = df["boll_upper"].iloc[i] if "boll_upper" in df.columns else 0
            cur_hist = df["macd_hist"].iloc[i] if "macd_hist" in df.columns else 0
            prev_hist = df["macd_hist"].iloc[i - 1] if i > 0 and "macd_hist" in df.columns else 0
            cur_bw = df["boll_bw"].iloc[i] if "boll_bw" in df.columns else 0
            prev_bw = df["boll_bw"].iloc[i - 1] if i > 0 and "boll_bw" in df.columns else 0
            cur_dif = df["macd_dif"].iloc[i] if "macd_dif" in df.columns else 0
            prev_dif = df["macd_dif"].iloc[i - 1] if i > 0 and "macd_dif" in df.columns else 0
            cur_dea = df["macd_dea"].iloc[i] if "macd_dea" in df.columns else 0
            prev_dea = df["macd_dea"].iloc[i - 1] if i > 0 and "macd_dea" in df.columns else 0

            # TP1: 触碰上轨 + 红柱缩量 → 减仓一半
            if not partial_exit and close_price >= cur_upper * 0.98:
                if cur_hist > 0 and cur_hist < prev_hist:
                    sell_shares = int(shares * 0.5 / 100) * 100
                    if sell_shares >= 100:
                        capital += sell_shares * close_price
                        shares -= sell_shares
                        partial_exit = True
                        if trades:
                            trades[-1]["tp1_date"] = str(date)[:10]
                            trades[-1]["tp1_price"] = round(close_price, 2)
                            trades[-1]["tp1_type"] = "上轨缩量减半"
                            trades[-1]["partial_exit_price"] = round(close_price, 2)

            # TP2: 布林收口 + 滞涨 + 高位死叉 → 全部清仓
            if cur_bw < prev_bw and cur_dif < cur_dea and prev_dif >= prev_dea:
                if shares > 0:
                    proceeds = shares * close_price
                    trade_pnl = proceeds - shares * entry_price
                    trade_pnl_pct = trade_pnl / (shares * entry_price) * 100
                    capital += proceeds
                    if trades:
                        trades[-1]["exit_date"] = str(date)[:10]
                        trades[-1]["exit_price"] = round(close_price, 2)
                        trades[-1]["exit_type"] = "TP2清仓(布林收口+死叉)"
                        trades[-1]["pnl"] = round(trade_pnl, 2)
                        trades[-1]["pnl_pct"] = round(trade_pnl_pct, 2)
                        trades[-1]["hold_days"] = hold_days
                    shares = 0
                    in_position = False
                    hold_days = 0
                    partial_exit = False

        # ── 强制平仓：最大持仓日 ──
        if in_position and hold_days >= max_hold_days:
            proceeds = shares * close_price
            trade_pnl = proceeds - shares * entry_price
            trade_pnl_pct = trade_pnl / (shares * entry_price) * 100
            capital += proceeds
            if trades:
                trades[-1]["exit_date"] = str(date)[:10]
                trades[-1]["exit_price"] = round(close_price, 2)
                trades[-1]["exit_type"] = "强制平仓(超期)"
                trades[-1]["pnl"] = round(trade_pnl, 2)
                trades[-1]["pnl_pct"] = round(trade_pnl_pct, 2)
                trades[-1]["hold_days"] = hold_days
            shares = 0
            in_position = False
            hold_days = 0
            partial_exit = False

    # ── 最终持仓结算 ──
    if in_position and shares > 0:
        final_price = float(df["close"].iloc[-1])
        proceeds = shares * final_price
        trade_pnl = proceeds - shares * entry_price
        trade_pnl_pct = trade_pnl / (shares * entry_price) * 100
        capital += proceeds
        if trades:
            trades[-1]["exit_date"] = str(df["date"].iloc[-1])[:10]
            trades[-1]["exit_price"] = round(final_price, 2)
            trades[-1]["exit_type"] = "期末强制平仓"
            trades[-1]["pnl"] = round(trade_pnl, 2)
            trades[-1]["pnl_pct"] = round(trade_pnl_pct, 2)
            trades[-1]["hold_days"] = hold_days
        shares = 0

    # ── 统计指标 ──
    total_return = capital - initial_capital
    total_return_pct = total_return / initial_capital * 100

    # 持仓盈亏
    winning_trades = [t for t in trades if t["pnl"] > 0]
    losing_trades = [t for t in trades if t["pnl"] < 0]
    win_rate = len(winning_trades) / len(trades) * 100 if trades else 0
    avg_win = np.mean([t["pnl"] for t in winning_trades]) if winning_trades else 0
    avg_loss = np.mean([t["pnl"] for t in losing_trades]) if losing_trades else 0
    max_loss = min([t["pnl"] for t in trades]) if trades else 0
    max_profit = max([t["pnl"] for t in trades]) if trades else 0

    # 盈亏比
    profit_factor = abs(sum(t["pnl"] for t in winning_trades) / 
                        sum(t["pnl"] for t in losing_trades)) if losing_trades and sum(t["pnl"] for t in losing_trades) != 0 else float('inf')

    # 夏普比（基于日收益）
    if len(equity_curve) > 20:
        eq_df = pd.DataFrame(equity_curve)
        eq_df["return"] = eq_df["total_value"].pct_change().fillna(0)
        sharpe = np.sqrt(252) * eq_df["return"].mean() / eq_df["return"].std() if eq_df["return"].std() > 0 else 0
    else:
        sharpe = 0

    # 最大回撤
    if len(equity_curve) > 0:
        eq_df = pd.DataFrame(equity_curve)
        eq_df["peak"] = eq_df["total_value"].cummax()
        eq_df["drawdown"] = (eq_df["total_value"] - eq_df["peak"]) / eq_df["peak"] * 100
        max_drawdown = eq_df["drawdown"].min()
    else:
        max_drawdown = 0

    return {
        "trades": trades,
        "total_trades": len(trades),
        "winning_trades": len(winning_trades),
        "losing_trades": len(losing_trades),
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_return, 2),
        "total_pnl_pct": round(total_return_pct, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_loss": round(max_loss, 2),
        "max_profit": round(max_profit, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "sharpe_ratio": round(sharpe, 2),
        "initial_capital": initial_capital,
        "final_capital": round(capital, 2),
    }


# ── 格式化输出 ──────────────────────────────────────────


def format_trade_result(name: str, code: str, result: dict) -> str:
    if "error" in result:
        return f"  ✗ {name}({code}): {result['error']}"

    lines = [
        f"  {result['total_trades']:3d}笔 | "
        f"胜率{result['win_rate']:5.1f}% | "
        f"总盈亏{result['total_pnl']:>+8.0f} | "
        f"{result['total_pnl_pct']:>+6.2f}% | "
        f"夏普{result['sharpe_ratio']:>5.2f} | "
        f"回撤{result['max_drawdown_pct']:>6.2f}% | "
        f"盈亏比{result['profit_factor']:>5.2f} | "
        f"均盈{result['avg_win']:>+8.0f} | "
        f"均亏{result['avg_loss']:>+8.0f} | "
        f"最亏{result['max_loss']:>+8.0f}"
    ]
    return f"  {name}({code}): " + "".join(lines)


def make_rating(result: dict) -> tuple:
    """
    综合评级:
    (标签, 排序分)
    """
    wr = result["win_rate"]
    pnl = result["total_pnl"]
    pnl_pct = result["total_pnl_pct"]
    sharpe = result["sharpe_ratio"]
    dd = abs(result["max_drawdown_pct"])
    trades = result["total_trades"]

    if trades < 2:
        return ("样本不足", 0)

    score = 0
    score += wr * 1.0  # 胜率权重
    score += min(pnl_pct, 50) * 0.8  # 收益率权重(cap 50%)
    score += max(min(sharpe, 3), -3) * 5.0  # 夏普比权重
    score -= dd * 0.5  # 回撤惩罚

    if trades >= 3 and wr >= 60 and pnl_pct > 5 and sharpe > 0.5:
        return ("✨推荐", score)
    elif trades >= 2 and wr >= 50 and pnl > 0:
        return ("✓可用", score)
    elif pnl > 0:
        return ("○勉强", score)
    else:
        return ("✗不适用", score)


# ── 主流程 ──────────────────────────────────────────────


def backtest_single(code: str, name: str, start_date: str = "20230101",
                    end_date: str = "", capital: float = 100000) -> dict:
    """单只股票回测"""
    if not end_date:
        end_date = datetime.now().strftime("%Y%m%d")

    raw = fetch_kline_baostock(code, start_date, end_date)
    if not raw or len(raw) < 60:
        return {"error": f"K线不足({len(raw) if raw else 0}条)"}

    records = []
    for k in raw:
        try:
            # baostock 返回中文键名: 日期/开盘/最高/最低/收盘/成交量(手)/成交额(万元)
            records.append({
                "date": k.get("日期"),
                "open": float(k.get("开盘", 0) or 0),
                "high": float(k.get("最高", 0) or 0),
                "low": float(k.get("最低", 0) or 0),
                "close": float(k.get("收盘", 0) or 0),
                "volume": float(k.get("成交量(手)", 0) or 0),
                "amount": float(k.get("成交额(万元)", 0) or 0),
            })
        except (ValueError, TypeError):
            continue

    if len(records) < 60:
        return {"error": f"有效K线不足({len(records)}条)"}

    df = pd.DataFrame(records)
    df = df.sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")

    # 生成信号
    df = generate_signals(df)

    # 回测
    result = run_backtest(df, capital)

    # 附加标的元信息
    result["code"] = code
    result["name"] = name

    return result


def batch_backtest(codes: list, names: list = None,
                   start_date: str = "20230101",
                   capital: float = 100000) -> list:
    """批量回测"""
    results = []
    total = len(codes)
    for idx, code in enumerate(codes):
        name = names[idx] if names else STOCK_MAP.get(code, code)
        print(f"  [{idx+1}/{total}] {name}({code})...", file=sys.stderr)
        result = backtest_single(code, name, start_date, capital=capital)
        results.append(result)
        line = format_trade_result(name, code, result)
        print(line, file=sys.stderr)
    return results


def format_summary(results: list, top_n: int = 10) -> str:
    """输出汇总报告"""
    # 筛选有交易记录的
    valid = [r for r in results if "error" not in r and r["total_trades"] >= 2]
    others = [r for r in results if "error" not in r and r["total_trades"] < 2]
    failed = [r for r in results if "error" in r]

    # 排序
    ranked = []
    for r in valid:
        label, score = make_rating(r)
        r["rating_label"] = label
        r["rating_score"] = score
        ranked.append(r)

    ranked.sort(key=lambda x: x["rating_score"], reverse=True)

    lines = []
    lines.append(f"{'='*100}")
    lines.append(f"📊 布林带+MACD双指标共振突破战法回测报告")
    lines.append(f"{'='*100}")
    lines.append(f"")

    # 回测参数
    lines.append(f"📋 参数: BOLL({BOLL_PERIOD},{BOLL_STD}) + MACD({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL})")
    lines.append(f"   起始资金: ¥100,000/只 | 标的数: {len(valid)+len(others)+len(failed)}")
    lines.append(f"")

    # 总体统计
    total_pnl = sum(r["total_pnl"] for r in valid)
    total_trades = sum(r["total_trades"] for r in valid)
    avg_win_rate = np.mean([r["win_rate"] for r in valid]) if valid else 0
    avg_sharpe = np.mean([r["sharpe_ratio"] for r in valid]) if valid else 0
    avg_dd = np.mean([abs(r["max_drawdown_pct"]) for r in valid]) if valid else 0

    lines.append(f"📈 总体 ({len(valid)}只有效)")
    lines.append(f"   总盈亏: ¥{total_pnl:>+,.0f} | 总交易: {total_trades}笔")
    lines.append(f"   平均胜率: {avg_win_rate:.1f}% | 平均夏普: {avg_sharpe:.2f} | 平均回撤: {avg_dd:.2f}%")
    lines.append(f"")

    # 各评级统计
    for label in ["✨推荐", "✓可用", "○勉强", "✗不适用"]:
        subset = [r for r in ranked if r["rating_label"] == label]
        if subset:
            avg_wr = np.mean([r["win_rate"] for r in subset])
            avg_pnl = np.mean([r["total_pnl"] for r in subset])
            avg_shp = np.mean([r["sharpe_ratio"] for r in subset])
            avg_dd_sub = np.mean([abs(r["max_drawdown_pct"]) for r in subset])
            lines.append(f"  {label}: {len(subset)}只 | 均胜率{avg_wr:.1f}% | 均收益¥{avg_pnl:>+,.0f} | 均夏普{avg_shp:.2f} | 均回撤{avg_dd_sub:.2f}%")

    lines.append(f"")
    lines.append(f"{'-'*100}")
    lines.append(f"")

    # Top N 详细排名
    lines.append(f"🏆 Top {min(top_n, len(ranked))} 排名")
    lines.append(f"{'排名':>4} {'评级':<8} {'标的':<16} {'代码':<8} {'笔数':<6} {'胜率':<8} {'总盈亏':<10} {'收益率':<8} {'夏普':<8} {'回撤':<8} {'盈亏比':<8}")
    lines.append(f"{'-'*100}")
    for i, r in enumerate(ranked[:top_n]):
        lines.append(
            f"{i+1:>4} {r['rating_label']:<8} {r['name']:<16} {r['code']:<8} "
            f"{r['total_trades']:<6} {r['win_rate']:<7.1f}% "
            f"{r['total_pnl']:<+9.0f} {r['total_pnl_pct']:<+7.2f}% "
            f"{r['sharpe_ratio']:<7.2f} {r['max_drawdown_pct']:<7.2f}% "
            f"{r['profit_factor']:<7.2f}"
        )

    if others:
        lines.append(f"")
        lines.append(f"⚠️ 样本不足(<2笔): {len(others)}只")
        for r in others:
            lines.append(f"  {r['name']}({r['code']}): {r['total_trades']}笔")

    if failed:
        lines.append(f"")
        lines.append(f"⛔ 失败: {len(failed)}只")
        for r in failed:
            lines.append(f"  {r['name']}({r['code']}): {r['error']}")

    # 各入场类型分析
    entry_stats = {"低吸仓": 0, "突破仓": 0, "低吸→突破仓": 0}
    for r in valid:
        for t in r.get("trades", []):
            et = t.get("entry_type", "")
            if et in entry_stats:
                entry_stats[et] += 1
    lines.append(f"")
    lines.append(f"📋 入场类型分布:")
    for k, v in entry_stats.items():
        lines.append(f"  {k}: {v}次")

    return "\n".join(lines)


def export_json(results: list, path: str = "/config/quant_scripts/data/boll_macd_backtest.json"):
    """导出JSON"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    output = []
    for r in results:
        item = {k: v for k, v in r.items() if k != "trades"}
        item["trade_count"] = len(r.get("trades", []))
        output.append(item)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 结果已导出: {path}")
    return path


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="布林带+MACD双指标共振突破战法回测")
    parser.add_argument("--codes", nargs="+", help="股票代码列表，默认=STOCK_MAP全部非ETF")
    parser.add_argument("--start", default="20230101", help="起始日期 YYYYMMDD")
    parser.add_argument("--end", default="", help="结束日期 YYYYMMDD（默认今天）")
    parser.add_argument("--capital", type=float, default=100000, help="每只起始资金（默认10万）")
    parser.add_argument("--top", type=int, default=10, help="显示前N名（默认10）")
    parser.add_argument("--export", action="store_true", help="导出JSON结果")
    args = parser.parse_args()

    if args.codes:
        codes = args.codes
        names = [STOCK_MAP.get(c, c) for c in codes]
    else:
        # 默认：STOCK_MAP 中的非ETF股票
        codes = []
        names = []
        for code, name in STOCK_MAP.items():
            if not name.endswith("ETF") and not name.endswith("ETF联接"):
                codes.append(code)
                names.append(name)
        print(f"📋 默认标的池: STOCK_MAP 非ETF股票, 共{len(codes)}只", file=sys.stderr)

    print(f"\n🚀 开始回测 {len(codes)} 只标的...\n", file=sys.stderr)
    start_time = datetime.now()
    results = batch_backtest(codes, names, args.start, args.capital)
    elapsed = (datetime.now() - start_time).total_seconds()
    print(f"\n⏱ 耗时: {elapsed:.1f}s\n", file=sys.stderr)

    summary = format_summary(results, args.top)
    print(summary)

    if args.export:
        export_json(results)

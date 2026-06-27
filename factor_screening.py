#!/usr/bin/env python3
"""
多因子选股排序系统

功能:
1. 对一批股票批量计算因子
2. 用训练好的模型或因子权重排序
3. 输出 Top/Bottom 榜单
4. 结合板块资金流做行业推荐

注意：历史 `rolling_predict.py` 已归档至 `archive/`；因子逻辑以本文件为准。
实际使用前已确认 `from sklearn.metrics import accuracy_score` 在 ai_factor_miner.py
顶部已有导入，本脚本自行导入以保持完整独立运行能力。
"""
import json
import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import pandas as pd
import numpy as np
from sklearn.metrics import accuracy_score

sys.path.insert(0, str(Path(__file__).parent))
from data_converter import fetch_kline, STOCK_MAP


def compute_all_factors(df):
    """计算全部因子，返回DataFrame"""
    df = df.copy().reset_index(drop=True)
    close = df["close"].values
    volume = df["volume"].values
    high = df["high"].values
    low = df["low"].values
    amount = df["amount"].values
    n = len(df)

    factors = {}

    # 基础统计
    factors["close"] = float(close[-1]) if n > 0 else 0

    # 收益率
    for period in [1, 5, 10, 20, 60]:
        if n > period:
            factors[f"ret_{period}d"] = (close[-1] / close[-(period+1)] - 1) * 100
        else:
            factors[f"ret_{period}d"] = 0

    # 波动率
    if n > 20:
        daily_rets = pd.Series(close).pct_change().dropna()
        factors["volatility_20d"] = float(daily_rets.tail(20).std() * np.sqrt(252) * 100)
    else:
        factors["volatility_20d"] = 0

    # MA偏离
    for period in [5, 10, 20, 60]:
        if n > period:
            ma = pd.Series(close).rolling(period).mean().values[-1]
            factors[f"ma_dev_{period}"] = (close[-1] / ma - 1) * 100
        else:
            factors[f"ma_dev_{period}"] = 0

    # 量比
    if n > 20:
        avg_vol = pd.Series(volume).rolling(20).mean().values[-1]
        factors["vol_ratio_20"] = volume[-1] / (avg_vol + 1e-9)
    else:
        factors["vol_ratio_20"] = 0

    # 价格位置 (20日)
    if n > 20:
        hh = pd.Series(high).rolling(20).max().values[-1]
        ll = pd.Series(low).rolling(20).min().values[-1]
        factors["price_pos_20"] = (close[-1] - ll) / (hh - ll + 1e-9) * 100
    else:
        factors["price_pos_20"] = 50  # 中性

    # 趋势强度
    if n > 60:
        ma5 = pd.Series(close).rolling(5).mean().values[-1]
        ma20 = pd.Series(close).rolling(20).mean().values[-1]
        ma60 = pd.Series(close).rolling(60).mean().values[-1]
        trend_score = 0
        if ma5 > ma20 > ma60:
            trend_score = 2  # 多头排列
        elif ma5 < ma20 < ma60:
            trend_score = -2  # 空头排列
        elif (ma5 > ma20) != (ma20 > ma60):
            trend_score = 0 if ma5 < ma20 else 1
        factors["trend_score"] = trend_score
    else:
        factors["trend_score"] = 0

    return factors


def compute_composite_score(factors):
    """综合评分（0~100）"""
    score = 50  # 基准分

    # 均线偏离（正的越好）
    ma_dev_5 = factors.get("ma_dev_5", 0)
    ma_dev_20 = factors.get("ma_dev_20", 0)
    score += ma_dev_5 * 1.5  # 短线偏离
    score += ma_dev_20 * 1.0  # 中线偏离

    # 短期收益（涨太好了扣分，防止追高）
    ret_5d = factors.get("ret_5d", 0)
    if ret_5d > 10:
        score -= (ret_5d - 10) * 1.5  # 涨太多扣分
    elif ret_5d < -5:
        score += abs(ret_5d + 5) * 0.5  # 跌多了适当加分但不激进

    # 趋势得分
    trend_score = factors.get("trend_score", 0)
    score += trend_score * 10

    # 量比（温和放量加分）
    vol_ratio = factors.get("vol_ratio_20", 1)
    if 1.0 < vol_ratio < 3.0:
        score += 5
    elif vol_ratio > 5:
        score -= 5  # 放量太大可能有风险

    # 波动率（低波动加分）
    vol = factors.get("volatility_20d", 20)
    if vol < 30:
        score += 3
    elif vol > 60:
        score -= 3

    # 价格位置（中低位加分）
    pos = factors.get("price_pos_20", 50)
    if 20 < pos < 80:
        score += 3
    elif pos < 10:
        score += 5  # 超跌
    elif pos > 90:
        score -= 5  # 高位

    return max(0, min(100, round(score, 1)))


def get_stock_quote(stock_code):
    """获取个股最新行情（经 market_data，不再依赖 OmniData spider）。"""
    try:
        from market_data import fetch_quote_old_format

        q = fetch_quote_old_format(stock_code)
        if not q:
            return None
        return {
            "最新价": q["最新价"],
            "涨跌幅": q["涨跌幅"],
            "市盈率(动态)": q.get("市盈率(动态)"),
            "换手(%)": q.get("换手(%)"),
            # market_data 使用「万」；下游仍读 成交额(万元)
            "成交额(万元)": q.get("成交额(万)", 0),
        }
    except Exception:
        return None


def get_sector_flow_data(sector_code="BK0737"):
    """板块资金流（原 OmniData 接口已废弃；当前返回 None，占位供后续 Eastmoney HTTP 接入）。"""
    _ = sector_code
    return None


def factor_screening(stock_codes, start_date="20230101", end_date="20250426",
                     output_dir="/root/ai_trading_package/qlib_data/screening"):
    """多因子选股排序主函数"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for code in stock_codes:
        stock_name = STOCK_MAP.get(code, code)
        print(f"  [INFO] 分析 {stock_name}({code})...", file=sys.stderr)

        # 获取行情
        quote = get_stock_quote(code)
        close = float(quote.get("最新价", 0)) if quote else 0
        pct = float(quote.get("涨跌幅", 0)) if quote else 0
        pe = quote.get("市盈率(动态)", None) if quote else None
        turnover = quote.get("换手(%)", None) if quote else None
        volume = float(quote.get("成交额(万元)", 0)) / 10000 if quote else 0  # 亿元

        # 获取K线算因子
        kline = fetch_kline(code, start_date, end_date)
        if not kline:
            results.append({
                "code": code, "name": stock_name,
                "close": close, "pct_change": pct,
                "pe": pe, "turnover": turnover,
                "volume_亿": volume,
                "composite_score": 0,
                "grade": "N/A",
                "reason": "K线数据获取失败",
            })
            continue

        df = convert_to_qlib_csv(kline, f"/tmp/{code}_temp.csv")
        if df is None:
            continue

        # 计算因子
        factors = compute_all_factors(df)
        factors.update({
            "close": close, "pct_change": pct, "pe": pe,
            "turnover": turnover, "volume_亿": volume,
        })

        score = compute_composite_score(factors)

        # 评级
        if score >= 75:
            grade = "值得操作"
        elif score >= 55:
            grade = "可留观察"
        else:
            grade = "建议剔除"

        results.append({
            "code": code, "name": stock_name,
            "close": close, "pct_change": pct,
            "pe": pe, "turnover": turnover,
            "volume_亿": volume,
            "composite_score": score,
            "grade": grade,
            "factors": factors,
        })

    # 排序
    results.sort(key=lambda x: x["composite_score"], reverse=True)

    # 保存
    with open(output_dir / "screening_result.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)

    return results


def format_screening_report(results):
    """格式化选股报告"""
    lines = ["📊 **多因子选股排序报告**", ""]

    for grade, icon in [("值得操作", "✅"), ("可留观察", "🟡"), ("建议剔除", "🔴")]:
        group = [r for r in results if r["grade"] == grade]
        if not group:
            continue

        lines.append(f"{icon} **{grade}** ({len(group)}只)")
        for r in group:
            pct_str = f"{r['pct_change']:+.2f}%" if r['pct_change'] else "N/A"
            score_str = f"评分 {r['composite_score']}" if r['composite_score'] else "N/A"
            volume_str = f"成交{r['volume_亿']:.1f}亿" if r.get('volume_亿') and r['volume_亿'] > 0 else "低量"

            f_name = r.get("factors", {})
            trend = "多头" if f_name.get("trend_score", 0) >= 1 else ("空头" if f_name.get("trend_score", 0) <= -1 else "震荡")

            lines.append(f"  **{r['name']}**({r['code']}) | 收盘{r['close']} | {pct_str} | {score_str} | {trend} | {volume_str}")

        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="多因子选股排序")
    parser.add_argument("--codes", nargs="+", help="股票代码列表")
    parser.add_argument("--start", default="20230101")
    parser.add_argument("--end", default="20250426")
    args = parser.parse_args()

    codes = args.codes or list(STOCK_MAP.keys())
    results = factor_screening(codes, args.start, args.end)
    print(format_screening_report(results))

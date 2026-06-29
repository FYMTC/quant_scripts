"""bottom_fish_score.py — T1.10 抄底分计算器（2026-06-29）

为 direction_resolver 的空仓急跌 BUY 判定提供量化依据。
抄底分 0-1，≥ BF_THR(0.6) 才允许 BUY，否则 WAIT。

四维打分（缺维度跳过 + 重归一化；cp 为强制维度）：
    cp        (w=0.35, 强制)  当日 K 线收盘分位 < 0.4 → 1.0（贴近下影线）
    MA20      (w=0.30, 硬否决) close > MA20 才算趋势未破；below → 直接 0.0
    analyst   (w=0.20)        TradingAgents 缓存 verdict HOLD/BUY → 1.0
    guba      (w=0.15)        股吧情绪偏空（逆向抄底信号）→ 1.0

数据源（已验证可得性）：
    cp      : market_data.fetch_quote(code) — 唯一盘中实时 OHLC 源
    MA20    : risk_monitor.get_price_history(code, 90) — baostock 收盘价
    analyst : StockKB().check_cache(code, price) — 4h/3% 缓存（不调 180s subprocess）
    guba    : tradingagents_runner._fetch_eastmoney_guba(code, 20) — 纯文本，自写关键词启发式

兜底：缺维度跳过 + 重归一化（final = Σ(score×w)/Σ(可用w)）；
      cp 拿不到 → 直接返回 {score: None}（抄底分核心，缺它无抄底语义）。
"""

from __future__ import annotations

from typing import Optional, Dict, List, Tuple


# ========== 权重与阈值 ==========

W_CP = 0.35
W_MA20 = 0.30
W_ANALYST = 0.20
W_GUBA = 0.15

MA20_PERIOD = 20
MA20_MIN_LOOKBACK = 90  # 取近 90 个交易日收盘算 MA20


# 股吧情绪关键词（用于自写启发式打分，因 _fetch_eastmoney_guba 只返回纯文本）
GUBA_BEARISH_KW = (
    "割肉", "止损", "暴雷", "跑路", "跌停", "大跌", "套牢", "清仓", "减仓",
    "看空", "悲观", "完蛋", "亏死", "血亏", "销户",
)
GUBA_BULLISH_KW = (
    "抄底", "加仓", "看多", "涨停", "大涨", "满仓", "乐观", "牛市",
    "起飞", "拉升", "上车", "干它",
)


def _cp_score(cp: float) -> float:
    """收盘分位 → 抄底分。cp<0.4 贴近下影线 → 高分。"""
    if cp < 0.4:
        return 1.0
    if cp < 0.6:
        return 0.5
    return 0.0


def _calc_cp(high: float, low: float, close: float) -> float:
    """cp = (close - low) / (high - low)，high==low 时取 0.5。

    参考 backtest_drop_buy_rally_sell.py:61 的口径。
    """
    if high == low:
        return 0.5
    return (close - low) / (high - low)


def _calc_above_ma20(closes: List[float]) -> Optional[bool]:
    """计算 close 是否在 MA20 之上。数据不足返回 None（不否决）。"""
    if len(closes) < MA20_PERIOD:
        return None
    ma20 = sum(closes[-MA20_PERIOD:]) / MA20_PERIOD
    if ma20 <= 0:
        return None
    return closes[-1] > ma20


def _score_guba(text: str) -> Optional[float]:
    """股吧纯文本 → 情绪分。偏空（逆向抄底信号）→ 1.0，偏多 → 0.3，中性 → 0.5。

    不可用文本（如"暂不可用"）返回 None（跳过该维度）。
    """
    if not text:
        return None
    if "暂不可用" in text or "不可用" in text:
        return None
    bearish = sum(text.count(k) for k in GUBA_BEARISH_KW)
    bullish = sum(text.count(k) for k in GUBA_BULLISH_KW)
    if bearish > bullish:
        return 1.0   # 偏空 = 散户恐慌 = 逆向抄底机会
    if bullish > bearish:
        return 0.3   # 偏多 = 已上车，非底部
    return 0.5       # 中性


def _score_analyst(report: Optional[dict]) -> Optional[float]:
    """analyst_reports 缓存 → 抄底分。HOLD/BUY → 1.0，SELL → 0.0。"""
    if not report:
        return None
    verdict = str(report.get("verdict") or report.get("signal") or "").upper()
    if verdict in ("HOLD", "BUY"):
        return 1.0
    if verdict == "SELL":
        return 0.0
    return None


def compute(
    code: str,
    price: Optional[float] = None,
    regime: Optional[str] = None,
    risk_level: Optional[str] = None,
) -> Dict:
    """计算抄底分。

    Args:
        code: 股票代码
        price: 当前价（用于 analyst 缓存价位校验；None 时用 fetch_quote.price）
        regime: market_regime (bear/sideways/bull) — 弱市下调不在此处，由 resolver 处理
        risk_level: risk_level (safe/warning/danger)

    Returns:
        {
            "score": float|None,         # 0-1，None 表示无法计算（cp 缺失）
            "dimensions_used": [str],    # 参与计算的维度名
            "dimensions_missing": [str], # 缺失跳过的维度名
            "reason": str,               # 简短说明
            "details": {...},            # 各维度原始值
        }
    """
    dims_used: List[str] = []
    dims_missing: List[str] = []
    details: Dict = {"code": code, "price": price}
    weighted_sum = 0.0
    weight_sum = 0.0

    # ── cp 维度（强制）──
    cp = None
    try:
        from market_data import fetch_quote
        q = fetch_quote(code)
        if q and q.get("high") is not None and q.get("low") is not None:
            close = float(q.get("price") or 0)
            high = float(q.get("high") or 0)
            low = float(q.get("low") or 0)
            if close > 0 and high > 0:
                cp = _calc_cp(high, low, close)
                details["cp"] = round(cp, 3)
                details["ohlc"] = {"open": q.get("open"), "high": high, "low": low, "close": close, "pre_close": q.get("pre_close")}
                if price is None:
                    price = close
    except Exception as e:
        details["cp_error"] = str(e)[:120]

    if cp is None:
        # cp 是强制维度，拿不到就不算抄底分
        return {
            "score": None,
            "dimensions_used": [],
            "dimensions_missing": ["cp", "ma20", "analyst", "guba"],
            "reason": "intraday_ohlc_unavailable",
            "details": details,
        }

    s_cp = _cp_score(cp)
    weighted_sum += s_cp * W_CP
    weight_sum += W_CP
    dims_used.append("cp")
    details["cp_score"] = s_cp

    # ── MA20 维度（硬否决）──
    above_ma20: Optional[bool] = None
    try:
        from risk_monitor import get_price_history
        closes = get_price_history(code, MA20_MIN_LOOKBACK)
        above_ma20 = _calc_above_ma20(closes)
        if above_ma20 is not None:
            ma20 = sum(closes[-MA20_PERIOD:]) / MA20_PERIOD
            details["ma20"] = round(m20, 3) if (m20 := ma20) else None
            details["above_ma20"] = above_ma20
    except Exception as e:
        details["ma20_error"] = str(e)[:120]

    if above_ma20 is False:
        # 趋势已破，硬否决（均值回归要求 close > MA20）
        return {
            "score": 0.0,
            "dimensions_used": ["cp", "ma20"],
            "dimensions_missing": ["analyst", "guba"],
            "reason": "below_ma20_veto",
            "details": details,
        }
    if above_ma20 is True:
        weighted_sum += 1.0 * W_MA20
        weight_sum += W_MA20
        dims_used.append("ma20")
        details["ma20_score"] = 1.0
    else:
        dims_missing.append("ma20")  # 数据不足，跳过不否决

    # ── analyst 维度 ──
    try:
        from stock_kb import StockKB
        check_price = float(price) if price else 0
        if check_price > 0:
            cache = StockKB().check_cache(code, check_price)
            if cache and cache.get("hit") and cache.get("report"):
                s_analyst = _score_analyst(cache["report"])
                details["analyst_verdict"] = cache["report"].get("verdict")
                if s_analyst is not None:
                    weighted_sum += s_analyst * W_ANALYST
                    weight_sum += W_ANALYST
                    dims_used.append("analyst")
                    details["analyst_score"] = s_analyst
                else:
                    dims_missing.append("analyst")
            else:
                dims_missing.append("analyst")
                details["analyst_cache"] = cache.get("reason") if cache else "no_cache"
        else:
            dims_missing.append("analyst")
    except Exception as e:
        dims_missing.append("analyst")
        details["analyst_error"] = str(e)[:120]

    # ── guba 维度 ──
    try:
        from tradingagents_runner import _fetch_eastmoney_guba
        text = _fetch_eastmoney_guba(code, 20)
        s_guba = _score_guba(text)
        if s_guba is not None:
            weighted_sum += s_guba * W_GUBA
            weight_sum += W_GUBA
            dims_used.append("guba")
            details["guba_score"] = s_guba
            details["guba_len"] = len(text or "")
        else:
            dims_missing.append("guba")
    except Exception as e:
        dims_missing.append("guba")
        details["guba_error"] = str(e)[:120]

    if weight_sum <= 0:
        return {
            "score": None,
            "dimensions_used": dims_used,
            "dimensions_missing": dims_missing,
            "reason": "no_available_dimensions",
            "details": details,
        }

    final = weighted_sum / weight_sum
    return {
        "score": round(final, 3),
        "dimensions_used": dims_used,
        "dimensions_missing": dims_missing,
        "reason": f"final={final:.3f} from {dims_used}",
        "details": details,
    }


def cli():
    """CLI 入口：
    python3 bottom_fish_score.py 002049 --price 78.20
    """
    import argparse
    import json
    import sys

    sys.path.insert(0, "/root/ai_trading_package/quant/quant_scripts")

    p = argparse.ArgumentParser(description="T1.10 抄底分计算器")
    p.add_argument("code", help="股票代码")
    p.add_argument("--price", type=float, default=None, help="当前价（可选，缺省用 fetch_quote）")
    p.add_argument("--regime", default=None, help="market_regime")
    p.add_argument("--risk-level", default=None, help="risk_level")
    args = p.parse_args()

    result = compute(args.code, args.price, args.regime, args.risk_level)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()

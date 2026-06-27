#!python3
"""
risk_monitor.py — 风险监控桥接层
===============================
将 risk_metrics.py / position_sizer.py 的计算能力注入 cron 上下文。

流程：
  持仓 guard_config.json → Baostock 价格数据 → risk_metrics 计算
  → position_sizer 仓位评估 → JSON 快照 + 告警标记

用法：
  python risk_monitor.py                # 全量扫描，输出到stdout + 保存快照
  python risk_monitor.py --json         # 纯JSON输出（供cron注入）
  python risk_monitor.py --code 000938  # 单标扫描

输出：
  stdout: JSON 风险快照（cron agent 可解析）
  ${system.root}/data/risk_snapshot.json: 落盘快照（运行时由 `cfg.path.risk_snapshot` 解析）

Phase 2: FinCon CVaR + FINRS 多时间尺度动量 + PositionSizer
"""

import json, os, sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

from data_converter import fetch_kline_baostock
from risk_metrics import calc_cvar, calc_multi_momentum, calc_max_drawdown, calc_garch_vol
from position_sizer import PositionSizer, SizerInput
from trade_account_context import load_portfolio_truth
from system_config import cfg

GUARD_CONFIG = cfg.path.guard_config
SNAPSHOT_DIR = cfg.data_dir
SNAPSHOT_FILE = f"{SNAPSHOT_DIR}/risk_snapshot.json"
FEATURE_SNAPSHOT_FILE = f"{SNAPSHOT_DIR}/feature_snapshot.json"

# ── CVaR 告警阈值 ──
CVAR_WARN_THRESHOLD = -0.05   # CVaR 日收益 < -5%  → WARN
CVAR_DANGER_THRESHOLD = -0.08  # CVaR 日收益 < -8%  → DANGER
CVAR_DELTA_WARN = 0.10         # CVaR 恶化 > 10% vs 7日前 → 标记 deterioration

# ── 动量一致性 ──
MOMENTUM_CONSISTENCY_STRONG = 0.8  # 一致性 > 0.8 → 强趋势
MOMENTUM_CONSISTENCY_WEAK = 0.4    # 一致性 < 0.4 → 方向混乱


def load_guard_config() -> dict:
    """读取 guard_config.json（仅用于 watchlist/signals/thresholds 等配置）"""
    with open(GUARD_CONFIG) as f:
        return json.load(f)


def load_portfolio_from_db() -> dict:
    """从 EasyTHS 账户快照读取持仓+现金。"""
    return load_portfolio_truth()


def get_price_history(code: str, lookback_days: int = 90) -> List[float]:
    """从 Baostock 获取收盘价序列（最近N个交易日）
    
    fetch_kline_baostock 返回 list[dict]，每行 dict 键为中文:
      {"日期": "20260509", "开盘": ..., "收盘": ..., ...}
    """
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=lookback_days * 2)).strftime("%Y%m%d")

    try:
        records = fetch_kline_baostock(code, start_date, end_date)
        if records is None or len(records) == 0:
            return []
        # records 是 list[dict]，中文键
        closes = []
        for r in records:
            close_val = r.get("收盘", 0)
            if close_val and float(close_val) > 0:
                closes.append(float(close_val))
        # 取最近 lookback_days 条
        return closes[-lookback_days:]
    except Exception as e:
        print(f"[WARN] {code} 价格获取失败: {e}", file=sys.stderr)
        return []


def load_previous_snapshot() -> Optional[dict]:
    """加载上次快照用于CVaR趋势对比"""
    if not os.path.exists(SNAPSHOT_FILE):
        return None
    try:
        with open(SNAPSHOT_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def analyze_position(code: str, name: str, shares: int, cost: float,
                     total_assets: float, available_cash: float,
                     prev_snapshot: Optional[dict] = None) -> dict:
    """对单个持仓做完整风险分析"""
    prices = get_price_history(code)

    # ── 风险指标 ──
    cvar = calc_cvar(prices, confidence=0.95) if len(prices) >= 20 else None
    momentum = calc_multi_momentum(prices) if len(prices) >= 2 else None
    mdd = calc_max_drawdown(prices) if len(prices) >= 2 else None
    # Q1.1: GARCH(1,1) 条件波动率
    garch = calc_garch_vol(prices) if len(prices) >= 60 else None

    current_price = prices[-1] if prices else 0
    position_value = shares * current_price
    position_ratio = position_value / total_assets if total_assets > 0 else 0

    # ── 仓位评估 ──
    sizer = PositionSizer(total_assets=total_assets, available_cash=available_cash)
    vol = _estimate_volatility(prices)
    sizer_input = SizerInput(
        code=code, name=name,
        direction="HOLD",
        confidence=0.5,  # 中性
        current_shares=shares,
        current_price=current_price,
        avg_cost=cost,
        total_assets=total_assets,
        annual_volatility=vol
    )
    size_result = sizer.calculate(sizer_input)

    # ── CVaR 趋势对比 ──
    cvar_trend = _compare_cvar_trend(code, cvar, prev_snapshot)

    # ── 风险评级 ──
    risk_level, risk_reasons = _assess_risk(
        cvar, momentum, mdd, position_ratio, cvar_trend
    )

    # ── 动量解读 ──
    mom_analysis = {}
    if momentum:
        comp = momentum["composite"]
        cons = momentum["consistency"]
        if comp > 2 and cons > MOMENTUM_CONSISTENCY_STRONG:
            mom_analysis["trend"] = "strong_uptrend"
            mom_analysis["hint"] = "多时间尺度一致上行，趋势健康"
        elif comp < -2 and cons > MOMENTUM_CONSISTENCY_STRONG:
            mom_analysis["trend"] = "strong_downtrend"
            mom_analysis["hint"] = "多时间尺度一致下行，注意风控"
        elif comp > 1 and cons < MOMENTUM_CONSISTENCY_WEAK:
            mom_analysis["trend"] = "divergent_uptrend"
            mom_analysis["hint"] = "短期上行但长期乏力，短暂反弹"
        elif comp < -1 and cons < MOMENTUM_CONSISTENCY_WEAK:
            mom_analysis["trend"] = "divergent_downtrend"
            mom_analysis["hint"] = "短期下跌但长期仍有支撑"
        else:
            mom_analysis["trend"] = "neutral"
            mom_analysis["hint"] = "方向不明，观望"

    return {
        "code": code,
        "name": name,
        "shares": shares,
        "cost": cost,
        "current_price": round(current_price, 2),
        "position_value": round(position_value, 2),
        "position_ratio": round(position_ratio * 100, 1),
        "risk_level": risk_level,
        "risk_reasons": risk_reasons,
        "cvar": {
            "value": round(cvar * 100, 2) if cvar is not None else None,
            "confidence": 0.95,
            "sample_days": len(prices),
            "trend": cvar_trend,
        },
        "momentum": momentum,
        "momentum_analysis": mom_analysis,
        "max_drawdown": mdd,
        "garch": garch,  # Q1.1: GARCH(1,1) 条件波动率
        "position_assessment": {
            "risk_label": size_result.risk_label,
            "single_position_ratio": round(position_ratio * 100, 1),
            "max_allowed_ratio": 30.0,
        },
        "data_quality": "ok" if len(prices) >= 20 else f"insufficient({len(prices)}d)",
    }


def _estimate_volatility(prices: List[float]) -> float:
    """从价格序列估算年化波动率 — Q1.1: 优先使用 GARCH"""
    if len(prices) < 5:
        return 0.30
    # 优先使用 GARCH 条件波动率
    try:
        garch = calc_garch_vol(prices)
        if garch and garch.get('converged'):
            return round(min(garch['ann_vol'], 1.5), 2)
    except Exception:
        pass
    # 退路：简单历史波动率
    from risk_metrics import calc_returns
    import math
    rets = calc_returns(prices)
    if not rets:
        return 0.30
    daily_vol = (sum((r - sum(rets)/len(rets))**2 for r in rets) / len(rets)) ** 0.5
    annual_vol = daily_vol * math.sqrt(252)
    return round(min(annual_vol, 1.5), 2)


def _compare_cvar_trend(code: str, current_cvar: Optional[float],
                        prev_snapshot: Optional[dict]) -> str:
    """对比上次快照的CVaR趋势"""
    if current_cvar is None or prev_snapshot is None:
        return "insufficient_data"
    prev_positions = prev_snapshot.get("positions", {})
    prev = prev_positions.get(code, {})
    prev_cvar_raw = prev.get("cvar", {}).get("value")
    if prev_cvar_raw is None:
        return "new"
    prev_cvar = prev_cvar_raw / 100  # 快照存的是百分比
    delta = abs(current_cvar - prev_cvar)
    if current_cvar < prev_cvar and delta > CVAR_DELTA_WARN:
        return "deteriorating"
    elif current_cvar > prev_cvar and delta > CVAR_DELTA_WARN:
        return "improving"
    else:
        return "stable"


def _assess_risk(cvar: Optional[float], momentum: Optional[dict],
                 mdd: Optional[float], position_ratio: float,
                 cvar_trend: str) -> tuple:
    """综合风险评级"""
    reasons = []
    score = 0  # 越高越危险

    # CVaR
    if cvar is not None:
        if cvar < CVAR_DANGER_THRESHOLD:
            score += 3
            reasons.append(f"CVaR={cvar*100:.1f}% 处于危险区")
        elif cvar < CVAR_WARN_THRESHOLD:
            score += 1
            reasons.append(f"CVaR={cvar*100:.1f}% 低于警戒线")

    if cvar_trend == "deteriorating":
        score += 2
        reasons.append("CVaR 恶化中（前次→本次）")

    # 动量一致性
    if momentum:
        if momentum["consistency"] < MOMENTUM_CONSISTENCY_WEAK:
            score += 1
            reasons.append(f"动量方向混乱 (一致性={momentum['consistency']:.0%})")

    # 最大回撤
    if mdd is not None and mdd < -20:
        score += 2
        reasons.append(f"最大回撤{mdd:.0f}% 已超20%")

    # 仓位集中度
    if position_ratio > 0.30:
        score += 2
        reasons.append(f"仓位集中度{position_ratio*100:.0f}% 超30%上限")

    if score >= 4:
        return "danger", reasons
    elif score >= 2:
        return "warning", reasons
    else:
        return "safe", reasons


def run_full_scan(args) -> dict:
    """全量扫描所有持仓+自选"""
    # P1-1 修复: 持仓+现金从DB读取
    portfolio = load_portfolio_from_db()
    positions = portfolio["positions"]
    available_cash = portfolio["cash"]
    total_cost_basis = portfolio.get("total_cost_basis", 0)
    
    # 自选仍从 guard_config 读取（配置，非状态）
    config = load_guard_config()
    watchlist = config.get("watch_list", {})

    # 估算总资产 = 持仓成本市值 + 可用现金
    position_cost_value = sum(
        info["shares"] * info["cost"]
        for info in positions.values()
    )
    total_assets = position_cost_value + available_cash

    prev = load_previous_snapshot()

    results = {}
    flags = []

    # 分析持仓
    for code, info in positions.items():
        name = info["name"]
        shares = info["shares"]
        cost = info["cost"]
        result = analyze_position(
            code, name, shares, cost,
            total_assets, available_cash, prev
        )
        results[code] = result

        # 告警标记
        if result["risk_level"] in ("danger", "warning"):
            flags.append({
                "code": code,
                "name": name,
                "level": result["risk_level"],
                "reasons": result["risk_reasons"],
                "cvar": result["cvar"]["value"],
                "momentum_trend": result["momentum_analysis"].get("trend", "unknown"),
            })

    # 分析自选（仅风险指标，不评估仓位）
    for code, name in watchlist.items():
        if code in results:  # 已在持仓中
            continue
        prices = get_price_history(code)
        if len(prices) < 10:
            continue
        cvar = calc_cvar(prices) if len(prices) >= 20 else None
        momentum = calc_multi_momentum(prices) if len(prices) >= 8 else None
        mdd = calc_max_drawdown(prices)

        # 自选股检查是否有值得关注的信号
        watcher_flags = []
        if momentum:
            comp = momentum["composite"]
            cons = momentum["consistency"]
            if comp > 2 and cons > 0.6:
                watcher_flags.append(f"多周期上行 (合成{comp:+.1f}%)")
            if cvar is not None and cvar < CVAR_DANGER_THRESHOLD:
                watcher_flags.append(f"CVaR高危 ({cvar*100:.1f}%)")

        results[code] = {
            "code": code,
            "name": name,
            "type": "watchlist",
            "current_price": round(prices[-1], 2) if prices else 0,
            "cvar": round(cvar * 100, 2) if cvar is not None else None,
            "momentum": momentum,
            "max_drawdown": mdd,
            "watchlist_flags": watcher_flags,
            "data_quality": "ok" if len(prices) >= 20 else f"insufficient({len(prices)}d)",
        }

    # ── Q-phase: Copula 尾部相关性（持仓配对）──
    copula_pairs = {}
    position_codes = list(positions.keys())
    if len(position_codes) >= 2:
        try:
            from risk_metrics import calc_copula_tail
            import numpy as _np
            for i in range(len(position_codes)):
                for j in range(i+1, len(position_codes)):
                    c1, c2 = position_codes[i], position_codes[j]
                    p1 = get_price_history(c1)
                    p2 = get_price_history(c2)
                    if len(p1) >= 30 and len(p2) >= 30:
                        r1 = _np.diff(_np.log(p1))
                        r2 = _np.diff(_np.log(p2))
                        n = min(len(r1), len(r2))
                        cop = calc_copula_tail(r1[:n], r2[:n])
                        copula_pairs[f"{c1}_{c2}"] = cop
        except Exception:
            pass

    snapshot = {
        "generated_at": datetime.now().isoformat(),
        "total_assets_estimate": round(total_assets, 2),
        "available_cash": round(available_cash, 2),
        "positions": {k: v for k, v in results.items() if v.get("type") != "watchlist"},
        "watchlist": {k: v for k, v in results.items() if v.get("type") == "watchlist"},
        "copula": copula_pairs,  # Q-phase: Copula尾部相关性
        "flags": flags,
        "summary": {
            "total_positions": len(positions),
            "danger_count": sum(1 for f in flags if f["level"] == "danger"),
            "warning_count": sum(1 for f in flags if f["level"] == "warning"),
        },
    }

    # 落盘
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    return snapshot


def cli():
    import argparse
    p = argparse.ArgumentParser(description="风险监控桥接层")
    p.add_argument("--json", action="store_true", help="纯JSON输出")
    p.add_argument("--code", type=str, help="单标扫描")
    args = p.parse_args()

    if args.code:
        # P1-1 修复: 持仓从DB读取
        portfolio = load_portfolio_from_db()
        positions = portfolio["positions"]
        available_cash = portfolio["cash"]
        if args.code in positions:
            info = positions[args.code]
            result = analyze_position(
                args.code, info["name"], info["shares"], info["cost"],
                available_cash, available_cash
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"'{args.code}' 不在持仓中", file=sys.stderr)
            sys.exit(1)
        return

    snapshot = run_full_scan(args)

    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print("=" * 60)
        print("  风险监控桥接层 — CVaR + 多时间尺度动量")
        print(f"  快照时间: {snapshot['generated_at']}")
        print(f"  总资产估算: ¥{snapshot['total_assets_estimate']:,.0f}")
        print(f"  可用现金: ¥{snapshot['available_cash']:,.0f}")
        print("=" * 60)

        pos_data = snapshot["positions"]
        if not pos_data:
            print("\n无持仓数据")
        else:
            print(f"\n{'代码':<8} {'名称':<14} {'价格':>7} {'仓位%':>6} "
                  f"{'CVaR':>7} {'1d动量':>7} {'7d动量':>7} {'30d动量':>7} {'评级':>6}")
            print("-" * 80)
            for code, d in pos_data.items():
                mom = d.get("momentum", {}) or {}
                cvar_str = f"{d['cvar']['value']:+.1f}%" if d.get("cvar", {}).get("value") is not None else "N/A"
                print(f"{code:<8} {d['name']:<14} {d['current_price']:>7.2f} "
                      f"{d['position_ratio']:>5.1f}% {cvar_str:>7} "
                      f"{mom.get('1d', 0):>+6.1f}% {mom.get('7d', 0):>+6.1f}% "
                      f"{mom.get('30d', 0):>+6.1f}% {d['risk_level']:>6}")

        flags = snapshot["flags"]
        if flags:
            print(f"\n⚠️  告警标志 ({len(flags)}项)")
            for f in flags:
                level_icon = "🔴" if f["level"] == "danger" else "🟡"
                print(f"  {level_icon} {f['name']}({f['code']}) "
                      f"CVaR={f['cvar']}% | 动量={f['momentum_trend']}")
                for r in f["reasons"]:
                    print(f"     → {r}")

        watch = snapshot.get("watchlist", {})
        flagged_watch = {k: v for k, v in watch.items() if v.get("watchlist_flags")}
        if flagged_watch:
            print(f"\n👀 自选关注 ({len(flagged_watch)}标的)")
            for code, w in flagged_watch.items():
                mom = w.get("momentum", {}) or {}
                print(f"  {w['name']}({code}) | "
                      f"{', '.join(w['watchlist_flags'])} | "
                      f"动量合成{mom.get('composite', 0):+.1f}%")

        print(f"\n快照已保存: {SNAPSHOT_FILE}")
        print("=" * 60)


if __name__ == "__main__":
    cli()

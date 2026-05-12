"""
risk_check.py — TradingAgents 独立风控审核脚本

操盘手出交易信号后，风控层独立评估。
借鉴 TradingAgents 三角色辩论 + Portfolio Manager 终审模式。

核心原则：决策建议 ≠ 执行指令。两钥签名。

用法：
  /config/quant_env/bin/python risk_check.py verify 002594 BUY 100 --price 101.48
  /config/quant_env/bin/python risk_check.py portfolio
  /config/quant_env/bin/python risk_check.py verify 002594 BUY 100 --price 101.48 --json

P1-1 修复 (2026-05-12): 持仓/现金统一走 stock_kb DB，不再读 guard_config.json
"""

import json
import sys
import os
from datetime import datetime
from typing import Dict, Optional, List, Tuple

# ========== 常量 ==========

SNAPSHOT_PATH = "/config/quant_scripts/market_snapshot.json"
STATE_PATH = "/config/quant_scripts/guard_state.json"

# 仓位限制
MAX_TOTAL_POSITION = 0.95
MAX_SINGLE_POSITION = 0.50
MIN_TRADE_UNIT_STOCK = 100
MIN_TRADE_UNIT_ETF = 100

ETF_PREFIXES = ("51", "15", "16", "56", "58")


# ========== 数据读取 ==========

_portfolio_cache = None

def _get_portfolio_truth() -> dict:
    """P1-1 修复: 从 stock_kb DB 读取持仓+现金（唯一信息源）"""
    global _portfolio_cache
    if _portfolio_cache is None:
        from stock_kb import StockKB
        kb = StockKB()
        _portfolio_cache = kb.read_portfolio_truth()
    return _portfolio_cache

def load_positions_dict() -> Dict[str, Dict]:
    """P1-1 修复: 从DB读取，不再从guard_config.json"""
    return _get_portfolio_truth().get("positions", {})

def get_available_cash() -> float:
    """P1-1 修复: 从DB读取可用现金"""
    return _get_portfolio_truth().get("cash", 0.0)


def load_snapshot_data() -> Dict:
    if not os.path.exists(SNAPSHOT_PATH):
        return {}
    with open(SNAPSHOT_PATH, "r") as f:
        return json.load(f)


def load_price_history() -> Dict[str, List[float]]:
    """从 guard_state.json 读取CVaR价格历史"""
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r") as f:
            state = json.load(f)
        raw = state.get("price_history", {})
        result = {}
        for code, daily in raw.items():
            dates = sorted(daily.keys())
            result[code] = [daily[d] for d in dates]
        return result
    except Exception:
        return {}


def get_positions_summary() -> Dict:
    positions = load_positions_dict()
    snap = load_snapshot_data()
    price_history = load_price_history()
    total_value = 0.0
    details = []
    for code, pos in positions.items():
        name = pos.get("name", code)
        cost = float(pos.get("cost", 0))
        shares = int(pos.get("shares", 0))
        quote = snap.get(code, {})
        price = quote.get("price", quote.get("current_price", 0))
        if not price:
            price = float(pos.get("current_price", 0))
        market_value = price * shares
        pnl = (price - cost) * shares if cost and price else 0
        pnl_pct = (price - cost) / cost * 100 if cost and price else 0
        
        # 多时间尺度动量分析
        hist_prices = price_history.get(code, [])
        momentum = None
        if len(hist_prices) >= 2:
            from risk_metrics import calc_multi_momentum
            momentum = calc_multi_momentum(hist_prices)
        
        total_value += market_value
        details.append({
            "code": code, "name": name, "shares": shares,
            "cost": cost, "price": price, "market_value": market_value,
            "pnl": pnl, "pnl_pct": round(pnl_pct, 2),
            "momentum": momentum,
        })
    return {"total_value": total_value, "details": details}


# ========== 检查函数 ==========

def is_etf(ticker: str) -> bool:
    return ticker.startswith(ETF_PREFIXES) if len(ticker) >= 2 else False


def check_trade_unit(ticker: str, shares: int) -> Tuple[bool, str]:
    unit = MIN_TRADE_UNIT_ETF if is_etf(ticker) else MIN_TRADE_UNIT_STOCK
    if shares % unit != 0:
        return False, f"交易单位必须为 {unit} 的整数倍"
    if shares <= 0:
        return False, "交易数量必须为正"
    return True, "OK"


def check_position_limit(ticker: str, action: str, shares: int, price: float) -> Tuple[bool, str]:
    summary = get_positions_summary()
    total_value = summary["total_value"]
    current_value = 0
    for d in summary["details"]:
        if d["code"] == ticker:
            current_value = d["market_value"]
            break
    action_value = shares * price
    if action in ("BUY", "OVERWEIGHT"):
        new_total = total_value + action_value
        new_single = current_value + action_value
        print(f"  总市值: {total_value:.0f} → {new_total:.0f}")
        print(f"  单标市值: {current_value:.0f} → {new_single:.0f}")
        # P1-1 修复: 从DB读取可用现金
        available = get_available_cash()
        if action_value > available:
            return False, f"可用资金 {available:.0f} 不足"
        ratio = new_single / new_total if new_total > 0 else 0
        if ratio > MAX_SINGLE_POSITION:
            return False, f"单标占比 {ratio:.1%} 超过 {MAX_SINGLE_POSITION:.0%}"
    return True, "OK"


def risk_check_order(ticker: str, action: str, shares: int, price: float = None) -> Dict:
    checks = []
    all_pass = True
    now = datetime.now()

    print(f"\n═══ 风控审核: {action} {ticker} x{shares} ═══\n")

    # 检查1: 交易单位
    ok, msg = check_trade_unit(ticker, shares)
    checks.append({"check": "trade_unit", "pass": ok, "message": msg})
    print(f"  [1/3] 交易单位: {'✅' if ok else '❌'} {msg}")
    if not ok:
        all_pass = False

    # 检查2: 仓位限制
    print(f"  [2/3] 仓位限制:")
    if price:
        ok, msg = check_position_limit(ticker, action, shares, price)
        checks.append({"check": "position_limit", "pass": ok, "message": msg})
        print(f"    → {'✅' if ok else '❌'} {msg}")
        if not ok:
            all_pass = False
    else:
        print("    未提供价格，跳过")
        checks.append({"check": "position_limit", "pass": True, "message": "skipped"})

    # 检查3: 交易时段
    wd = now.weekday()
    h, m = now.hour, now.minute
    in_hours = (h == 9 and m >= 30) or (10 <= h <= 11) or (h == 13) or (h == 14 and m <= 56)
    if wd < 5 and in_hours:
        print(f"  [3/3] 交易时段: ✅ ({now.strftime('%H:%M')})")
        checks.append({"check": "hours", "pass": True, "message": "交易时段"})
    else:
        print(f"  [3/3] 交易时段: ⚠️ 非交易时段 ({now.strftime('%H:%M')})，作为计划单")
        checks.append({"check": "hours", "pass": True, "message": "计划单"})

    # 风控辩论
    print(f"\n  🔴 激进: {'同意' if all_pass else '风险可接受'}")
    if action in ("BUY", "OVERWEIGHT"):
        print(f"  🟢 保守: {'同意' if all_pass else '建议暂缓'}")
    else:
        print(f"  🟢 保守: {'同意' if all_pass else '逻辑可接受'}")
    print(f"  ⚪ 中性: {'批准' if all_pass else '建议修改'}")

    if all_pass:
        final, rationale = "APPROVE", "所有检查通过"
    else:
        fatal = [c for c in checks if not c["pass"]]
        if any("交易单位" in c["message"] for c in fatal):
            final, rationale = "REJECT", f"致命: {'; '.join(c['message'] for c in fatal)}"
        else:
            final, rationale = "MODIFY", f"修改: {'; '.join(c['message'] for c in fatal)}"

    print(f"\n  📌 裁决: {final} — {rationale}\n")

    return {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "ticker": ticker, "action": action, "shares": shares, "price": price,
        "checks": checks, "all_pass": all_pass, "final": final, "rationale": rationale,
        "risk_opinions": {
            "aggressive": "激进: 同意" if all_pass else "激进: 风险可接受",
            "conservative": "保守: 批准" if all_pass else "保守: 建议谨慎",
            "neutral": "中性: 同意执行" if all_pass else "中性: 建议修改",
        }
    }


def risk_check_portfolio() -> Dict:
    summary = get_positions_summary()
    details = summary["details"]
    total = summary["total_value"]
    warnings = []

    print(f"\n═══ 持仓风控扫描 ═══\n")

    for d in details:
        ratio = d["market_value"] / total if total > 0 else 0
        flag = "✅" if ratio <= MAX_SINGLE_POSITION else "⚠️"
        mom = d.get("momentum")
        mom_str = ""
        if mom and mom.get("1d") is not None:
            mom_str = f" 📊动量 1d={mom['1d']:+.1f}% 7d={mom['7d']:+.1f}% 30d={mom['30d']:+.1f}%"
            if mom["consistency"] >= 0.67:
                mom_str += " 📐一致"
        print(f"  {flag} {d['name']}({d['code']}): {d['market_value']:.0f} ({ratio:.1%}){mom_str}")
        if ratio > MAX_SINGLE_POSITION:
            warnings.append(f"{d['name']}占比{ratio:.1%}超限")
        if d["pnl_pct"] < -5:
            warnings.append(f"{d['name']}浮亏{d['pnl_pct']:.1%}")

    # P1-1 修复: 从DB读取可用现金
    available = get_available_cash()
    total_assets = total + available
    pr = total / total_assets if total_assets > 0 else 0
    print(f"\n  总市值: {total:.0f}  可用: {available:.0f}  仓位: {pr:.1%}")
    if pr > MAX_TOTAL_POSITION:
        warnings.append(f"总仓位{pr:.1%}超{MAX_TOTAL_POSITION:.0%}")

    if warnings:
        print(f"\n  ⚠️ {len(warnings)} 个风险点:")
        for w in warnings:
            print(f"    • {w}")
    else:
        print(f"\n  ✅ 全部正常")

    return {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_value": total, "positions": details,
            "warnings": warnings, "pass": len(warnings) == 0}


# ========== CLI入口 ==========

def main():
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify")
    v.add_argument("ticker")
    v.add_argument("action", choices=["BUY", "SELL", "HOLD", "OVERWEIGHT", "UNDERWEIGHT"])
    v.add_argument("shares", type=int)
    v.add_argument("--price", type=float)
    v.add_argument("--json", action="store_true")
    pf = sub.add_parser("portfolio")
    pf.add_argument("--json", action="store_true")
    args = p.parse_args()

    if args.cmd == "verify":
        r = risk_check_order(args.ticker, args.action, args.shares, args.price)
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
    elif args.cmd == "portfolio":
        r = risk_check_portfolio()
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

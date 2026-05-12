"""
rebalance_monitor.py — 组合再平衡提醒

每日检查持仓的仓位比例和现金占比，
超标时自动推送再平衡建议。

阈值：
- 现金 > 20% → 建议加仓
- 现金 < 5%  → 建议留缓冲
- 单标占比 > 50% → 建议分散
- 单标浮亏 > 5% → 建议评估止损

用法：
  python rebalance_monitor.py
  python rebalance_monitor.py --json   # JSON输出
"""

import json
import os
import sys
from datetime import datetime, date
from typing import Dict, List, Optional

SCRIPT_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(SCRIPT_DIR, "guard_config.json")
SNAPSHOT_PATH = os.path.join(SCRIPT_DIR, "market_snapshot.json")

# 阈值
MAX_CASH_IDLE = 0.20      # 现金 > 20% 闲置
MIN_CASH_BUFFER = 0.05    # 现金 < 5% 太激进
MAX_SINGLE_RATIO = 0.50   # 单标 > 50%
MAX_LOSS_PCT = -5.0       # 单标浮亏 > 5%

REBALANCE_LOG = os.path.join(SCRIPT_DIR, "rebalance_log.json")


def load_data() -> tuple:
    """加载配置和行情"""
    # P1-1 修复: 持仓+现金从DB读取，其他配置（自选/信号/阈值）从 guard_config.json
    config = {}
    snapshot = {}
    
    # 先读 guard_config.json（获取 watch_list/signals/thresholds 等配置）
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    
    # 覆盖持仓+现金为 DB 唯一真相
    from stock_kb import StockKB
    kb = StockKB()
    pf = kb.read_portfolio_truth()
    config["positions"] = pf["positions"]
    config["available_capital"] = pf["cash"]
    
    if os.path.exists(SNAPSHOT_PATH):
        with open(SNAPSHOT_PATH) as f:
            raw = json.load(f)
        # 快照可能是 {"quotes": {...}} 嵌套格式
        snapshot = raw.get("quotes", raw)
    
    return config, snapshot


def calc_holdings() -> Dict:
    """计算持仓状态"""
    config, snapshot = load_data()
    positions = config.get("positions", {})
    available = float(config.get("available_capital", 0))
    
    total_market = 0.0
    details = []
    
    for code, info in positions.items():
        name = info.get("name", code)
        cost = float(info.get("cost", 0))
        shares = int(info.get("shares", 0))
        
        # 从快照取价格
        quote = snapshot.get(code, {})
        price = quote.get("p", quote.get("price", 0))
        if not price:
            continue
        
        market_value = price * shares
        pnl = (price - cost) * shares
        pnl_pct = (price - cost) / cost * 100 if cost > 0 else 0
        total_market += market_value
        
        details.append({
            "code": code,
            "name": name,
            "shares": shares,
            "cost": cost,
            "price": price,
            "market_value": market_value,
            "pnl": pnl,
            "pnl_pct": round(pnl_pct, 2),
        })
    
    total_assets = total_market + available
    cash_ratio = available / total_assets if total_assets > 0 else 0
    
    # 计算各标的占比
    for d in details:
        d["ratio"] = round(d["market_value"] / total_assets * 100, 1) if total_assets > 0 else 0
    
    return {
        "total_market": total_market,
        "available_cash": available,
        "total_assets": total_assets,
        "cash_ratio": round(cash_ratio * 100, 1),
        "holdings": details,
        "count": len(details),
    }


def check_alerts(state: Dict) -> List[Dict]:
    """生成再平衡告警"""
    alerts = []
    
    # 1. 现金比例检查
    cash_ratio = state["cash_ratio"] / 100
    if cash_ratio > MAX_CASH_IDLE:
        idle_amount = state["available_cash"] - state["total_assets"] * MAX_CASH_IDLE
        alerts.append({
            "type": "cash_idle",
            "level": "⚠️",
            "message": f"现金{state['cash_ratio']:.0f}%闲置（{state['available_cash']:.0f}元），超{MAX_CASH_IDLE:.0%}阈值{idle_amount:.0f}元",
            "suggestion": f"建议用约{idle_amount:.0f}元建仓或加仓",
        })
    elif cash_ratio < MIN_CASH_BUFFER:
        alerts.append({
            "type": "cash_low",
            "level": "🟡",
            "message": f"现金仅{state['cash_ratio']:.0f}%，无缓冲空间",
            "suggestion": "建议保留至少5%现金应对赎回/补仓",
        })
    
    # 2. 单标过度集中
    for h in state["holdings"]:
        if h["ratio"] > MAX_SINGLE_RATIO * 100:
            alerts.append({
                "type": "concentration",
                "level": "🔴",
                "message": f"{h['name']}({h['code']})占比{h['ratio']:.0f}%，超{MAX_SINGLE_RATIO:.0%}上限",
                "suggestion": f"建议减仓至{MAX_SINGLE_RATIO:.0%}以下",
            })
    
    # 3. 浮亏过大
    for h in state["holdings"]:
        if h["pnl_pct"] < MAX_LOSS_PCT:
            alerts.append({
                "type": "loss_warning",
                "level": "🔴",
                "message": f"{h['name']}({h['code']})浮亏{h['pnl_pct']:.1f}%，超{MAX_LOSS_PCT:.0f}%阈值",
                "suggestion": "评估是否需要止损",
            })
    
    # 4. 持仓数量过少
    if state["count"] <= 1 and state["available_cash"] > 0:
        alerts.append({
            "type": "diversify",
            "level": "🟡",
            "message": f"仅{state['count']}只持仓，风险集中",
            "suggestion": "建议分散到2-3只标的",
        })
    
    return alerts


def save_log(state: Dict, alerts: List[Dict]):
    """保存再平衡日志"""
    log_entry = {
        "time": datetime.now().isoformat(),
        "date": date.today().isoformat(),
        "state": {
            "total_assets": state["total_assets"],
            "cash_ratio": state["cash_ratio"],
            "holdings": state["holdings"],
        },
        "alerts": alerts,
        "alert_count": len(alerts),
    }
    
    # 追加模式
    logs = []
    if os.path.exists(REBALANCE_LOG):
        with open(REBALANCE_LOG) as f:
            try:
                logs = json.load(f)
            except:
                logs = []
    logs.append(log_entry)
    
    # 保留最近30天
    if len(logs) > 30:
        logs = logs[-30:]
    
    with open(REBALANCE_LOG, "w") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    state = calc_holdings()
    alerts = check_alerts(state)
    save_log(state, alerts)

    if args.json:
        print(json.dumps({
            "state": {
                "total": round(state["total_assets"], 0),
                "cash": state["cash_ratio"],
                "holdings": state["holdings"],
            },
            "alerts": alerts,
        }, ensure_ascii=False, indent=2))
        return

    print(f"""
╔══════════════════════════════════╗
║  组合再平衡检查                  ║
╠══════════════════════════════════╣
║  总资产: {state['total_assets']:>10.0f}元        ║
║  现金:   {state['available_cash']:>10.0f}元 ({state['cash_ratio']}%) ║
║  持仓:   {state['count']}只                ║
╚══════════════════════════════════╝
""")
    
    for h in state["holdings"]:
        sign = "🟢" if h["pnl"] >= 0 else "🔴"
        print(f"  {sign} {h['name']}({h['code']}) {h['ratio']}% | "
              f"¥{h['market_value']:.0f} | {'+' if h['pnl']>=0 else ''}{h['pnl']:.0f}元")
    
    if alerts:
        print(f"\n⚠️  再平衡告警 ({len(alerts)}条):")
        for a in alerts:
            print(f"  {a['level']} {a['message']}")
            print(f"    → {a['suggestion']}")
    else:
        print("\n✅ 组合健康，无需调整")


if __name__ == "__main__":
    main()

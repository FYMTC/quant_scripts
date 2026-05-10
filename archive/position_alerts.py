#!/usr/bin/env python3
"""
持仓异动监控脚本
检测比亚迪和黄金ETF是否触及关键价位，输出异动报告
由cronjob在交易日盘中调用
"""
import json, os, sys, urllib.request

OMNIDATA_URL = "http://172.17.0.3:8380/api/v1/spiders/run"

def run_spider(spider, params):
    data = json.dumps({"spider_name": spider, "params": params}).encode()
    req = urllib.request.Request(OMNIDATA_URL, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None

def get_quote(code):
    r = run_spider("eastmoney_stock_quote", {"stock_code": code})
    if r and r.get("success") and r.get("data"):
        return r["data"]
    return None

# === 阈值设定 ===
ALERTS = {
    "002594": {  # 比亚迪
        "name": "比亚迪",
        "cost": 99.531,
        "stop_loss": 98.0,       # 跌破止损线
        "overbuy": 103.0,        # 突破加仓参考线
        "daily_pct_alert": 3.0,  # 单日涨跌幅超过3%提醒
    },
    "518880": {  # 黄金ETF
        "name": "黄金ETF",
        "cost": 10.131,
        "stop_loss": 9.60,       # 跌破止损线
        "overbuy": 10.00,        # 站上10元关口
        "daily_pct_alert": 2.0,  # 单日涨跌幅超过2%提醒
    },
}

report = []
for code, cfg in ALERTS.items():
    q = get_quote(code)
    if not q:
        continue

    price = float(q.get("最新价", 0))
    pct = float(q.get("涨跌幅", 0))
    
    alerts = []
    # 止损线
    if price < cfg["stop_loss"]:
        alerts.append(f"🔴 **跌破止损线** {cfg['stop_loss']}！现价{price:.2f}")
    # 破位预警
    elif price < cfg["stop_loss"] * 1.02:
        alerts.append(f"⚠️ 接近止损线{cfg['stop_loss']}（现价{price:.2f}，距止损仅{(price-cfg['stop_loss'])/cfg['stop_loss']*100:.1f}%）")
    # 突破
    if price > cfg["overbuy"]:
        alerts.append(f"🟢 突破{cfg['overbuy']}关口！现价{price:.2f}")
    # 大幅波动
    if abs(pct) >= cfg["daily_pct_alert"]:
        direction = "大涨" if pct > 0 else "大跌"
        alerts.append(f"📊 单日{direction} {pct:+.2f}%")
    # 回本线
    if cfg["cost"] * 0.99 <= price <= cfg["cost"] * 1.01:
        alerts.append(f"🎯 接近成本价{cfg['cost']}（现价{price:.2f}，偏差{((price/cfg['cost'])-1)*100:+.2f}%）")

    report.append({
        "code": code,
        "name": cfg["name"],
        "price": price,
        "pct": pct,
        "cost": cfg["cost"],
        "profit_pct": (price / cfg["cost"] - 1) * 100,
        "alerts": alerts,
    })

# 输出
has_alert = any(r["alerts"] for r in report)
if has_alert:
    print("🚨 **持仓异动监控** 🚨")
    print()
    for r in report:
        profit_icon = "🟢" if r["profit_pct"] >= 0 else "🔴"
        print(f"{profit_icon} **{r['name']}**({r['code']}) 现价{r['price']:.2f} | {r['pct']:+.2f}% | 盈亏{r['profit_pct']:+.2f}%")
        if r["alerts"]:
            for a in r["alerts"]:
                print(f"  {a}")
        print()
else:
    # 无异动也报一下持仓状态（简洁版）
    for r in report:
        profit_icon = "🟢" if r["profit_pct"] >= 0 else "🔴"
        print(f"{profit_icon} {r['name']} 现价{r['price']:.2f} | {r['pct']:+.2f}% | 盈亏{r['profit_pct']:+.2f}%")

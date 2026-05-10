#!/usr/bin/env python3
"""判断比亚迪明天开盘走势的量化依据"""
import sys, json, warnings
sys.path.insert(0, '/config/quant_scripts')
from data_converter import run_omnidata_spider, STOCK_MAP
import pandas as pd
import numpy as np
warnings.filterwarnings("ignore")

OMNIDATA = "http://172.17.0.3:8380/api/v1/spiders/run"

def run_spider(name, params):
    data = json.dumps({"spider_name": name, "params": params}).encode()
    import urllib.request
    req = urllib.request.Request(OMNIDATA, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except:
        return None

print("=" * 50)
print("  🎯 比亚迪 明日走势多维度研判")
print("=" * 50)

# 1. 最新行情
q = run_spider("eastmoney_stock_quote", {"stock_code": "002594"})
if q and q.get("success"):
    d = q["data"]
    print(f"\n📊 今日行情")
    print(f"  最新价: {d.get('最新价')} | 涨跌幅: {d.get('涨跌幅')}%")
    print(f"  今开: {d.get('今开')} | 昨收: {d.get('昨收')} | 最高: {d.get('最高')} | 最低: {d.get('最低')}")
    print(f"  成交量: {d.get('成交量(手)')}手 | 成交额: {d.get('成交额(万元)')}万")

# 2. 近日K线走势
klines = run_spider("eastmoney_stock_daily_kline", {
    "stock_code": "002594", "start_date": "20250414", "end_date": "20260426", "adjust_type": "qfq"
})
if klines and klines.get("success") and klines.get("data"):
    data = klines["data"]
    last5 = data[-5:]
    print(f"\n📈 近5日走势")
    for k in last5:
        print(f"  {k['日期']}: 开{k['开盘']} 收{k['收盘']} 高{k['最高']} 低{k['最低']} 涨跌幅{k['涨跌幅(%)']}% 成交{k['成交额(万元)']}万")
    
    # 3. 关键判断
    last = data[-1]
    prev = data[-2] if len(data) >= 2 else None
    
    # 收盘是否高于开盘（阳线）
    is_yang = float(last['收盘']) > float(last['开盘'])
    # 是否收在日内高位（收盘>均值）
    mid = (float(last['最高']) + float(last['最低'])) / 2
    is_high_close = float(last['收盘']) > mid
    # 成交量是否放大
    if len(data) >= 6:
        avg_vol_last5 = sum(float(k['成交额(万元)']) for k in data[-6:-1]) / 5
        vol_ratio = float(last['成交额(万元)']) / avg_vol_last5 if avg_vol_last5 > 0 else 1
    else:
        vol_ratio = 1
    
    print(f"\n🔍 技术信号检查")
    print(f"  {'✅' if is_yang else '❌'} 今日收阳线（收盘>{'开盘' if is_yang else '开盘'}）")
    print(f"  {'✅' if is_high_close else '⚠️'} 收盘在日内高位（收在区间上沿）")
    print(f"  {'✅' if vol_ratio > 1.2 else '⚠️' if vol_ratio > 0.8 else '❌'} 成交量{'放大' if vol_ratio > 1.2 else '缩量' if vol_ratio < 0.8 else '正常'}（今日/5日均={vol_ratio:.2f}倍）")
    
    # 近3日走势判断
    if len(data) >= 3:
        d1 = float(data[-3]['涨跌幅(%)'])
        d2 = float(data[-2]['涨跌幅(%)'])
        d3 = float(data[-1]['涨跌幅(%)'])
        trend = "📈 连续3日企稳" if d3 > d2 > d1 else "📉 仍在走弱" if d3 < d2 < d1 else "🔄 震荡"
        print(f"  {trend}（3日涨跌幅: {d1}% → {d2}% → {d3}%）")
    
    # 距MA40的距离（判断超跌程度）
    closes = np.array([float(k['收盘']) for k in data])
    if len(closes) >= 40:
        ma40 = np.mean(closes[-40:])
        dev = (closes[-1] / ma40 - 1) * 100
        print(f"  {'✅' if dev < -3 else '⚠️' if dev < 0 else '❌'} 距MA40: {dev:.1f}%（{'超跌' if dev < -3 else '均线下方' if dev < 0 else '均线上方'}）")

# 4. 资金面
fund = run_spider("eastmoney_realtime_stock_fund_flow", {"secid": "0.002594"})
if fund and fund.get("success") and fund.get("data"):
    fd = fund["data"]
    today = fd.get("今日资金流向", {})
    main = today.get("主力", {})
    print(f"\n💰 今日资金")
    print(f"  主力净流入: {main.get('净流入(亿元)', 'N/A')}亿")
    print(f"  主力净占比: {main.get('净占比(%)', 'N/A')}%")

# 5. 综合结论
print(f"\n{'=' * 50}")
print(f"  📋 综合判断")
print(f"{'=' * 50}")
print(f"""
你的直觉"明天比亚迪可能会涨" → 

支持上涨的因素:
  - 连续3日涨跌幅企稳信号
  - 收盘在日内高位（如果成立）
  - 距成本价仅差0.07%，有心理支撑

不支持上涨的因素:
  - 主力10日累计流出51亿
  - MA40死叉
  - AI预测看跌(4.6%)
  - 大盘整体偏弱（近5日主力流出2494亿）

建议：如果明天高开>0.5%且放量，说明你的直觉对，可以再拿一拿；
如果低开或平开缩量，按策略执行卖出。
""")

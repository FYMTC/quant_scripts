#!/config/quant_env/bin/python3
"""
512480 半导体ETF 轻量门禁 — 收盘分析 FINAL
补全资金流数据 + 板块数据
"""
import sys, os, json, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

def http_get_json(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return None

# ===== 1. ETF专属资金流API =====
# 512480 is a Shanghai ETF, try the ETF-specific flow API
def fetch_etf_flow():
    """东方财富ETF资金流"""
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?secid=1.512480&fields1=f1,f2,f3&fields2=f51,f52,f54,f55&lmt=3"
    d = http_get_json(url)
    if d and d.get("data") and d["data"].get("klines"):
        lines = d["data"]["klines"]
        result = []
        for line in lines:
            parts = line.split(",")
            if len(parts) >= 4:
                result.append({
                    "date": parts[0],
                    "主力净流入万": float(parts[1]) / 10000 if parts[1] != "-" else 0,
                    "超大单净流入万": float(parts[2]) / 10000 if parts[2] != "-" else 0,
                    "大单净流入万": float(parts[3]) / 10000 if parts[3] != "-" else 0,
                })
        return result
    return None

def fetch_etf_fund_flow_today():
    """获取ETF今日实时资金流"""
    # 使用push2 API的fflow kline
    url = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?secid=1.512480&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55&klt=1&lmt=240"
    d = http_get_json(url)
    if d and d.get("data") and d["data"].get("klines"):
        lines = d["data"]["klines"]
        total_main = 0
        total_super_large = 0
        total_large = 0
        total_medium = 0
        total_small = 0
        for line in lines:
            parts = line.split(",")
            if len(parts) >= 5:
                # f52=主力净流入, f53=小单, f54=中单, f55=大单, f56=超大单
                total_main += float(parts[1]) if parts[1] != "-" else 0
                total_small += float(parts[2]) if parts[2] != "-" else 0
                total_medium += float(parts[3]) if parts[3] != "-" else 0
                total_large += float(parts[4]) if parts[4] != "-" else 0
                # 第6个是超大单
                if len(parts) >= 6:
                    total_super_large += float(parts[5]) if parts[5] != "-" else 0
        return {
            "main_net_wan": total_main / 10000,
            "super_large_wan": total_super_large / 10000,
            "large_wan": total_large / 10000,
            "medium_wan": total_medium / 10000,
            "small_wan": total_small / 10000,
            "total_minutes": len(lines),
        }
    return None

# ===== 2. 板块数据 — 使用另一个endpoint =====
def fetch_sector_v2():
    """获取行业板块 — fid=f3 按涨跌幅排名"""
    url = "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t2&fields=f12,f14,f2,f3,f62,f184"
    d = http_get_json(url)
    if d and d.get("data") and d["data"].get("diff"):
        sectors = []
        for item in d["data"]["diff"]:
            sectors.append({
                "code": item.get("f12", ""),
                "name": item.get("f14", ""),
                "pct": float(item.get("f3") or 0),
                "main_net_flow": float(item.get("f62") or 0) / 10000,
            })
        return sectors
    return None

# ===== 3. 获取ETF份额变动 =====
def fetch_etf_share_change():
    """ETF份额变动（申赎数据）"""
    url = "https://datacenter.eastmoney.com/securities/api/data/v1/get?reportName=RPT_ETF_SHARECHANGE&columns=TRADE_DATE,FUND_SHARES_CHANGE,FUND_SHARES&filter=(SECURITY_CODE=%22512480%22)&pageNumber=1&pageSize=3&sortTypes=-1&sortColumns=TRADE_DATE"
    d = http_get_json(url)
    if d and d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return None

print("=" * 70)
print("  512480 半导体ETF国联安 — 轻量门禁 FINAL")
print(f"  时间: {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')} CST")
print("=" * 70)

# ---- 资金流 ----
print("\n[数据源1] ETF资金流逐笔汇总...")
flow = fetch_etf_fund_flow_today()
if flow:
    print(f"  ✅ 获取到 {flow['total_minutes']} 分钟数据")
    print(f"  主力净流入:   {flow['main_net_wan']:+.0f}万")
    print(f"  超大单净流入: {flow['super_large_wan']:+.0f}万")
    print(f"  大单净流入:   {flow['large_wan']:+.0f}万")
    print(f"  中单净流入:   {flow['medium_wan']:+.0f}万")
    print(f"  小单净流入:   {flow['small_wan']:+.0f}万")
else:
    print("  ⚠️ 逐笔API失败, 尝试日级别...")
    flow = fetch_etf_flow()
    if flow:
        latest = flow[-1]
        print(f"  ✅ 最近数据 ({latest['date']}):")
        print(f"  主力净流入: {latest['主力净流入万']:+.0f}万")
        print(f"  超大单: {latest['超大单净流入万']:+.0f}万  大单: {latest['大单净流入万']:+.0f}万")
    else:
        print("  ❌ 资金流数据获取失败")

# ---- 板块 ----
print("\n[数据源2] 行业板块排名...")
sectors = fetch_sector_v2()
if sectors:
    print(f"  ✅ 获取到 {len(sectors)} 个板块")
    # Find semiconductor
    for i, s in enumerate(sectors):
        if "半导体" in s.get("name", ""):
            print(f"  🎯 半导体: 排名 #{i+1} (按涨跌幅)")
            print(f"     涨跌幅: {s['pct']:+.2f}%")
            print(f"     主力净流入: {s['main_net_flow']:+.0f}万")
            break
    else:
        print("  ⚠️ 未找到\"半导体\"板块, 列出前10:")
        for s in sectors[:10]:
            print(f"     {s['name']}: {s['pct']:+.2f}% 主力{s['main_net_flow']:+.0f}万")
else:
    print("  ❌ 板块数据获取失败")

# ---- ETF份额 ----
print("\n[数据源3] ETF份额变动...")
shares = fetch_etf_share_change()
if shares:
    for row in shares:
        print(f"  {row.get('TRADE_DATE','?')}: 份额变动 {row.get('FUND_SHARES_CHANGE','?')}万份, 总份额 {row.get('FUND_SHARES','?')}万份")
else:
    print("  ⚠️ 份额数据获取失败")

# ---- ETF实时报价 ----
print("\n[数据源4] ETF实时报价...")
quote_d = http_get_json("https://push2.eastmoney.com/api/qt/stock/get?secid=1.512480&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f168,f170,f62,f184,f185,f186,f187")
if quote_d and quote_d.get("data"):
    rd = quote_d["data"]
    div = 1000
    print(f"  现价: {int(rd.get('f43') or 0)/div:.3f}")
    print(f"  涨跌: {float(rd.get('f170') or 0)/100:+.2f}%")
    print(f"  今开: {int(rd.get('f46') or 0)/div:.3f}")
    print(f"  最高: {int(rd.get('f44') or 0)/div:.3f}")
    print(f"  最低: {int(rd.get('f45') or 0)/div:.3f}")
    print(f"  昨收: {int(rd.get('f60') or 0)/div:.3f}")
    print(f"  成交额: {int(rd.get('f48') or 0)/10000:.0f}万")
    print(f"  换手率: {float(rd.get('f168') or 0)/100:.2f}%")
else:
    print("  ❌ 实时报价获取失败")

print("\nDone.")

#!/config/quant_env/bin/python3
"""
直接通过 Python urllib 获取东方财富资金流向和板块数据
绕过 shell pipe 限制和 OmniData browser 超时
"""
import json, time
from urllib.request import Request, urlopen
from urllib.error import URLError

def em_get(url, timeout=10):
    """Python原生HTTP GET"""
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://quote.eastmoney.com/",
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        print(f"  HTTP error: {e}")
        return None

# 1. 获取512480实时资金流向
print("=== 1. 512480 实时资金流向 ===")
url1 = "https://push2.eastmoney.com/api/qt/stock/get?secid=1.512480&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f168,f170,f62,f116,f117,f184,f185,f186,f187,f188,f189,f190,f191,f192"
raw1 = em_get(url1)
if raw1:
    d1 = json.loads(raw1)
    if d1.get("rc") == 0 and d1.get("data"):
        rd = d1["data"]
        div = 1000
        print(f"  名称: {rd.get('f58','')}")
        print(f"  价格: {int(rd.get('f43',0))/div:.3f}")
        print(f"  涨跌幅: {float(rd.get('f170',0))/100:+.2f}%")
        print(f"  最高: {int(rd.get('f44',0))/div:.3f}")
        print(f"  最低: {int(rd.get('f45',0))/div:.3f}")
        print(f"  昨收: {int(rd.get('f60',0))/div:.3f}")
        print(f"  成交额(万): {int(rd.get('f48',0))/10000:.0f}")
        print(f"  换手率: {float(rd.get('f168',0))/100:.2f}%")
        # 资金流向
        main_net = float(rd.get('f62', 0))  # 主力净流入(元)
        super_large = float(rd.get('f184', 0))  # 超大单(元)
        large = float(rd.get('f185', 0))  # 大单(元)
        medium = float(rd.get('f186', 0))  # 中单(元)
        small = float(rd.get('f187', 0))  # 小单(元)
        print(f"  主力净流入(万): {main_net/10000:+.0f}")
        print(f"  超大单(万): {super_large/10000:+.0f}")
        print(f"  大单(万): {large/10000:+.0f}")
        print(f"  中单(万): {medium/10000:+.0f}")
        print(f"  小单(万): {small/10000:+.0f}")
    else:
        print(f"  API返回异常: rc={d1.get('rc')}")
else:
    print("  请求失败")

time.sleep(1.5)

# 2. 获取行业板块资金流向排名
print("\n=== 2. 行业板块资金流向排名 ===")
url2 = "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=90&po=1&np=1&fltt=2&invt=2&fid=f62&fs=m:90+t2&fields=f12,f14,f2,f3,f62,f184,f66,f69"
raw2 = em_get(url2)
if raw2:
    d2 = json.loads(raw2)
    if d2.get("data") and d2["data"].get("diff"):
        items = d2["data"]["diff"]
        print(f"  共{len(items)}个板块")
        # 找半导体
        for i, item in enumerate(items):
            name = item.get("f14", "")
            if "半导体" in name:
                rank = i + 1
                pct = item.get("f3", 0)
                main_flow = float(item.get("f62", 0)) / 10000  # 万
                print(f"\n  ★ 半导体板块: 排名#{rank}/{len(items)}")
                print(f"    代码: {item.get('f12','')}")
                print(f"    涨跌幅: {pct:+.2f}%")
                print(f"    主力净流入: {main_flow:+.0f}万")
                print(f"    最新价: {item.get('f2','')}")
                break
        else:
            print("  未找到半导体板块")
        
        # Top 5 行业
        print(f"\n  Top 5 行业板块(按主力净流入):")
        for item in items[:5]:
            print(f"    #{items.index(item)+1} {item.get('f14','')}: 涨跌{item.get('f3',0):+.2f}% 主力{float(item.get('f62',0))/10000:+.0f}万")
    else:
        print(f"  无数据: {list(d2.keys())}")
else:
    print("  请求失败")

time.sleep(1.5)

# 3. 大单成交明细（可选）
print("\n=== 3. 大盘指数 ===")
# 上证
url3a = "https://push2.eastmoney.com/api/qt/stock/get?secid=1.000001&fields=f43,f170,f48"
raw3a = em_get(url3a)
if raw3a:
    d3a = json.loads(raw3a)
    if d3a.get("data"):
        rd = d3a["data"]
        print(f"  上证指数: {float(rd.get('f43',0)):.2f} ({float(rd.get('f170',0))/100:+.2f}%) 成交{float(rd.get('f48',0))/100000000:.1f}亿")

url3b = "https://push2.eastmoney.com/api/qt/stock/get?secid=0.399006&fields=f43,f170,f48"
raw3b = em_get(url3b)
if raw3b:
    d3b = json.loads(raw3b)
    if d3b.get("data"):
        rd = d3b["data"]
        print(f"  创业板指: {float(rd.get('f43',0)):.2f} ({float(rd.get('f170',0))/100:+.2f}%) 成交{float(rd.get('f48',0))/100000000:.1f}亿")

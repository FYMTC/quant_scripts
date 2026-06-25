#!python3
"""Debug: Parse raw kline data to understand units."""
import json, urllib.request

url = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?secid=1.512480&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55&klt=1&lmt=5"
req = urllib.request.Request(url, headers={
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/",
})
with urllib.request.urlopen(req, timeout=10) as resp:
    d = json.loads(resp.read().decode("utf-8"))

print("API Response:")
print(json.dumps(d, ensure_ascii=False, indent=2))

if d.get("data") and d["data"].get("klines"):
    lines = d["data"]["klines"]
    print(f"\nFirst {len(lines)} kline rows:")
    for line in lines:
        parts = line.split(",")
        print(f"  {parts}")
        if len(parts) >= 5:
            # f52, f53, f54, f55
            for i, val in enumerate(parts[1:5], 1):
                if val and val != "-":
                    num = float(val)
                    print(f"    f5{i+1}: {num:,.0f} (= {num/10000:.4f}万)")

# Also try with klt=5 (5-min) to compare
url2 = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?secid=1.512480&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55&klt=5&lmt=5"
req2 = urllib.request.Request(url2, headers={
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/",
})
with urllib.request.urlopen(req2, timeout=10) as resp2:
    d2 = json.loads(resp2.read().decode("utf-8"))

if d2.get("data") and d2["data"].get("klines"):
    lines2 = d2["data"]["klines"]
    print(f"\n\nFirst {len(lines2)} 5-min kline rows:")
    for line in lines2:
        parts = line.split(",")
        print(f"  {parts}")

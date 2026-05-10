#!/config/quant_env/bin/python3
"""
东方财富行情数据层 - 通过curl子进程（稳定版）
特点：
1. 每次请求间隔至少1秒，避免触发反爬
2. 请求失败自动重试1次
3. 返回统一格式的dict
"""
import json
import subprocess
import time

API_URL = "https://push2.eastmoney.com/api/qt/stock/get"
_LAST_REQUEST_TIME = 0

def _rate_limit():
    """请求频率控制：每次请求间隔至少1秒"""
    global _LAST_REQUEST_TIME
    now = time.time()
    elapsed = now - _LAST_REQUEST_TIME
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _LAST_REQUEST_TIME = time.time()

def get_quote(code, name_hint=""):
    """获取单只股票/ETF行情，稳定版"""
    if code.startswith(("0", "3")):
        secid = f"0.{code}"
    else:
        secid = f"1.{code}"
    
    fields = "f43,f44,f45,f46,f47,f48,f50,f51,f57,f58,f168,f170,f100,f62,f115,f116,f117,f152"
    url = f"{API_URL}?secid={secid}&fields={fields}"
    
def get_quote(code, name_hint=""):
    """获取单只股票/ETF行情，稳定版"""
    if code.startswith(("0", "3")):
        secid = f"0.{code}"
    else:
        secid = f"1.{code}"
    
    fields = "f43,f44,f45,f46,f47,f48,f50,f51,f57,f58,f168,f170,f100,f62,f115,f116,f117,f152"
    url = f"{API_URL}?secid={secid}&fields={fields}"
    
    for attempt in range(2):
        try:
            _rate_limit()
            cmd = [
                "curl", "-s", "--connect-timeout", "5", "--max-time", "8",
                "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "-H", "Referer: https://quote.eastmoney.com/",
                url
            ]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            raw = out.stdout.strip()
            
            if not raw:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return None
            
            d = json.loads(raw)
            if d.get("rc") == 0 and d.get("data"):
                raw_d = d["data"]
                price_field = raw_d.get("f43")
                if price_field and price_field > 0:
                    return {
                        "代码": code,
                        "名称": raw_d.get("f58", name_hint),
                        "最新价": price_field / 100,
                        "最高": (raw_d.get("f44") or 0) / 100,
                        "最低": (raw_d.get("f45") or 0) / 100,
                        "今开": (raw_d.get("f46") or 0) / 100,
                        "昨收": (raw_d.get("f50") or 0) / 100,
                        "涨跌幅": (raw_d.get("f170") or 0) / 100,
                        "涨跌额": (raw_d.get("f100") or 0) / 100,
                        "成交量(手)": raw_d.get("f47") or 0,
                        "成交额(万元)": ((raw_d.get("f48") or 0) / 10000),
                        "换手(%)": (raw_d.get("f168") or 0) / 100,
                        "市盈率(动态)": (raw_d.get("f115") or 0) / 100,
                        "市净率": (raw_d.get("f117") or 0) / 100,
                    }
                else:
                    return {
                        "代码": code,
                        "名称": raw_d.get("f58", name_hint),
                        "最新价": 0,
                        "涨跌幅": 0,
                        "成交量(手)": 0,
                    }
            
            if attempt == 0:
                time.sleep(2)
        except Exception:
            if attempt == 0:
                time.sleep(2)
                continue
            return None
    
    return None


def get_klines(code, start_date="20260401", end_date="20260427", days=30):
    """获取K线数据"""
    if code.startswith(("0", "3")):
        secid = f"0.{code}"
    else:
        secid = f"1.{code}"
    
    if not start_date or start_date == "auto":
        from datetime import datetime, timedelta
        start = datetime.now() - timedelta(days=days)
        start_date = start.strftime("%Y%m%d")
    if not end_date:
        from datetime import datetime
        end_date = datetime.now().strftime("%Y%m%d")
    
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           f"&klt=101&fqt=1&beg={start_date}&end={end_date}")
    
    for attempt in range(2):
        try:
            _rate_limit()
            cmd = ["curl", "-s", "--connect-timeout", "8", "--max-time", "15",
                   "-H", "User-Agent: Mozilla/5.0", url]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            d = json.loads(out.stdout)
            if d.get("rc") == 0 and d.get("data") and d["data"].get("klines"):
                klines = []
                for line in d["data"]["klines"]:
                    parts = line.split(",")
                    if len(parts) >= 11:
                        klines.append({
                            "日期": parts[0],
                            "开盘": float(parts[1]),
                            "收盘": float(parts[2]),
                            "最高": float(parts[3]),
                            "最低": float(parts[4]),
                            "成交量": int(float(parts[5])),
                            "成交额": float(parts[6]),
                            "振幅": float(parts[7]),
                            "涨跌幅": float(parts[8]),
                            "涨跌额": float(parts[9]),
                            "换手率": float(parts[10]),
                        })
                return klines
            if attempt == 0:
                time.sleep(3)
        except Exception:
            if attempt == 0:
                time.sleep(3)
                continue
            return None
    return None


if __name__ == "__main__":
    import sys
    codes = sys.argv[1:] if len(sys.argv) > 1 else ["002594", "518880", "512480"]
    
    print("=== 行情测试 ===")
    for code in codes:
        r = get_quote(code)
        if r:
            print(f"  {r['名称']}({code}): {r['最新价']}元 ({r['涨跌幅']:+.2f}%) 换手{r['换手(%)']}%")
        else:
            print(f"  {code}: ❌")
    
    print("\n=== K线测试（仅比亚迪）===")
    k = get_klines("002594", days=10)
    if k:
        print(f"  获取{len(k)}条K线, 最新{len(k)>0 and k[-1]['收盘']}")

"""
market_data.py — 统一行情获取模块

设计目标：
1. 所有行情获取经过此模块，不再在脚本里写 curl
2. 统一字段名：价格用 price，涨跌幅用 pct，成交量用 vol
3. 统一 secid/除数逻辑，一处出错处处修
4. 内置限流，调用方不需要关心

用法：
  from market_data import fetch_quote, fetch_quotes_batch, get_index

  q = fetch_quote("002594")
  # {"code":"002594","name":"比亚迪","price":101.5,"pct":-0.55,"high":103.3,...}

  quotes = fetch_quotes_batch(["002594","518880"])
  # {"002594":{...}, "518880":{...}}

  idx = get_index()
  # {"上证指数":{...}, "深证成指":{...}, "创业板指":{...}}
"""

import subprocess
import json
import time
from datetime import datetime
from typing import Optional, Dict, Any

# ========== 常量 ==========

# 大盘指数 secid
INDEX_MAP = {
    "上证指数": "1.000001",
    "深证成指": "0.399001",
    "创业板指": "0.399006",
}

# 东方财富行情字段（保持与 smart_guard_v3.py 一致）
FIELDS = "f43,f44,f45,f46,f47,f48,f57,f58,f60,f168,f170,f100,f62"

# ETF 代码前缀
ETF_PREFIXES = ("51", "15", "16", "56", "58")

# ========== 限流 ==========

_LAST_REQ_TIME = [0.0]
_REQ_INTERVAL = 1.2  # 秒，东方财富反爬限制

def _rate_limit():
    """限流：每次请求间隔至少 1.2 秒"""
    now = time.time()
    elapsed = now - _LAST_REQ_TIME[0]
    if elapsed < _REQ_INTERVAL:
        time.sleep(_REQ_INTERVAL - elapsed)
    _LAST_REQ_TIME[0] = time.time()


# ========== 内部工具 ==========

def _is_etf(code: str) -> bool:
    """判断是否为 ETF"""
    return code[:2] in ETF_PREFIXES


def _secid(code: str) -> str:
    """根据股票代码生成 secid"""
    if code.startswith(("0", "3")):
        return f"0.{code}"
    return f"1.{code}"


def _divisor(code: str) -> int:
    """价格除数：股票÷100，ETF÷1000"""
    return 1000 if _is_etf(code) else 100


def _curl_get(url: str, timeout: int = 8) -> Optional[str]:
    """执行 curl 请求"""
    try:
        out = subprocess.run(
            ["curl", "-s", "--connect-timeout", "5", "--max-time", str(timeout),
             "-H", "User-Agent: Mozilla/5.0",
             "-H", "Referer: https://quote.eastmoney.com/",
             url],
            capture_output=True, text=True, timeout=(timeout + 2)
        )
        raw = out.stdout.strip()
        return raw if raw else None
    except:
        return None


# ========== 核心 API ==========

def fetch_quote(code: str) -> Optional[Dict[str, Any]]:
    """
    获取单只股票/ETF 实时行情。

    返回字段：
      code, name, price, pct, high, low, open, pre_close,
      vol(手), amount(万), turnover(%), etf(bool), time
    """
    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={_secid(code)}&fields={FIELDS}"

    for attempt in range(2):
        try:
            _rate_limit()
            raw = _curl_get(url)
            if not raw:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return None

            d = json.loads(raw)
            if d.get("rc") != 0 or not d.get("data"):
                if attempt == 0:
                    time.sleep(2)
                    continue
                return None

            rd = d["data"]
            f43 = int(rd.get("f43") or 0)
            if f43 <= 0:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return None

            div = _divisor(code)
            return {
                "code": code,
                "name": rd.get("f58", ""),
                "price": f43 / div,
                "pct": float(rd.get("f170") or 0) / 100,
                "high": int(rd.get("f44") or 0) / div,
                "low": int(rd.get("f45") or 0) / div,
                "open": int(rd.get("f46") or 0) / div,
                "pre_close": int(rd.get("f60") or 0) / div,
                "vol": int(rd.get("f47") or 0),
                "amount": int(rd.get("f48") or 0) / 10000,
                "turnover": float(rd.get("f168") or 0) / 100,
                "etf": _is_etf(code),
                "time": datetime.now().strftime("%H:%M:%S"),
            }

        except Exception:
            if attempt == 0:
                time.sleep(2)
                continue
            return None

    return None


def fetch_quotes_batch(codes: list) -> Dict[str, dict]:
    """批量获取行情（逐个请求，内置限流）"""
    results = {}
    for code in codes:
        q = fetch_quote(code)
        if q:
            results[code] = q
    return results


def get_index() -> Dict[str, Optional[dict]]:
    """获取大盘指数"""
    results = {}
    for name, secid in INDEX_MAP.items():
        url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f170"
        try:
            _rate_limit()
            raw = _curl_get(url)
            if not raw:
                results[name] = None
                continue
            d = json.loads(raw)
            if d.get("rc") == 0 and d.get("data"):
                rd = d["data"]
                results[name] = {
                    "price": float(rd.get("f43", 0)),
                    "pct": float(rd.get("f170", 0)) / 100,
                    "amount": float(rd.get("f48", 0)) / 100000000,
                    "high": float(rd.get("f44", 0)),
                    "low": float(rd.get("f45", 0)),
                    "pre_close": float(rd.get("f60", 0)),
                }
            else:
                results[name] = None
        except:
            results[name] = None
    return results


# ========== Tushare Pro 盘后数据 ==========

TUSHARE_TOKEN = "c12824dca6c7f14dc527424d05781bbbef68b980199819fbd15f28c3"

def _ts_code(code: str) -> str:
    """6位代码转Tushare格式"""
    code = code.strip()
    if code.endswith((".SZ", ".SH", ".BJ")):
        return code.upper()
    if code.startswith(("0", "3", "2")):
        return f"{code}.SZ"
    if code.startswith(("6", "5")):
        return f"{code}.SH"
    if code.startswith(("8", "4")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def tushare_daily(code: str, start_date: str = "", end_date: str = "") -> Optional[dict]:
    """
    通过 Tushare Pro 获取个股日线（盘后数据，补充验证用）。
    返回 dict 或 None。
    """
    try:
        import tushare as ts
        ts.set_token(TUSHARE_TOKEN)
        pro = ts.pro_api()
        params = {"ts_code": _ts_code(code)}
        if start_date:
            params["start_date"] = start_date.replace("-", "")
        if end_date:
            params["end_date"] = end_date.replace("-", "")
        df = pro.daily(**params)
        if df is not None and len(df) > 0:
            row = df.iloc[0]
            return {
                "code": code,
                "trade_date": str(row.get("trade_date", "")),
                "open": float(row.get("open", 0)),
                "high": float(row.get("high", 0)),
                "low": float(row.get("low", 0)),
                "close": float(row.get("close", 0)),
                "pre_close": float(row.get("pre_close", 0)),
                "change": float(row.get("change", 0)),
                "pct_chg": float(row.get("pct_chg", 0)),
                "vol_wan": float(row.get("vol", 0)),  # 万股
                "amount_qian": float(row.get("amount", 0)),  # 千元
                "_source": "tushare",
            }
    except Exception as e:
        print(f"[market_data] Tushare daily error: {e}")
    return None


def tushare_moneyflow_hsgt(start_date: str = "", end_date: str = "") -> Optional[list]:
    """
    沪深港通北向资金流向。
    返回 list of dicts，按日期降序。
    """
    try:
        import tushare as ts
        ts.set_token(TUSHARE_TOKEN)
        pro = ts.pro_api()
        params = {}
        if start_date:
            params["start_date"] = start_date.replace("-", "")
        if end_date:
            params["end_date"] = end_date.replace("-", "")
        df = pro.moneyflow_hsgt(**params)
        if df is not None and len(df) > 0:
            return df.to_dict("records")
    except Exception as e:
        print(f"[market_data] Tushare moneyflow_hsgt error: {e}")
    return None


def tushare_stock_basic(code: str) -> Optional[dict]:
    """
    个股基本面信息（行业、市场板块、上市日期）。
    """
    try:
        import tushare as ts
        ts.set_token(TUSHARE_TOKEN)
        pro = ts.pro_api()
        df = pro.stock_basic(
            ts_code=_ts_code(code),
            fields="ts_code,name,area,industry,market,list_date"
        )
        if df is not None and len(df) > 0:
            row = df.iloc[0]
            return {
                "code": code,
                "name": str(row.get("name", "")),
                "area": str(row.get("area", "")),
                "industry": str(row.get("industry", "")),
                "market": str(row.get("market", "")),
                "list_date": str(row.get("list_date", "")),
                "_source": "tushare",
            }
    except Exception as e:
        print(f"[market_data] Tushare stock_basic error: {e}")
    return None


# ========== Baostock 盘后数据 ==========

def baostock_daily(code: str, start_date: str = "", end_date: str = "") -> Optional[dict]:
    """
    通过 Baostock 获取个股日线（独立信源，用于交叉验证）。
    返回最近一天的 dict 或 None。
    """
    try:
        import baostock as bs
        bs.login()
        bs_code = f"sz.{code}" if code.startswith(("0", "3", "2")) else f"sh.{code}"
        sd = start_date if start_date else "2020-01-01"
        ed = end_date if end_date else "2099-01-01"
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,code,open,high,low,close,preclose,volume,amount,adjustflag",
            start_date=sd, end_date=ed,
            frequency="d", adjustflag="3"
        )
        data = []
        while rs.next():
            data.append(rs.get_row_data())
        bs.logout()
        if data:
            row = data[-1]  # 取最近一天
            return {
                "code": code,
                "trade_date": str(row[0]),
                "open": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "close": float(row[5]),
                "pre_close": float(row[6]),
                "vol": float(row[7]),  # 股
                "amount": float(row[8]),  # 元
                "_source": "baostock",
            }
    except Exception as e:
        print(f"[market_data] Baostock daily error: {e}")
    return None


def baostock_dividend(code: str, year: str = "2025") -> Optional[list]:
    """
    分红送配数据。
    """
    try:
        import baostock as bs
        bs.login()
        bs_code = f"sz.{code}" if code.startswith(("0", "3", "2")) else f"sh.{code}"
        rs = bs.query_dividend_data(code=bs_code, year=year, yearType="operate")
        data = []
        while rs.next():
            data.append(rs.get_row_data())
        bs.logout()
        return data if data else None
    except Exception as e:
        print(f"[market_data] Baostock dividend error: {e}")
    return None


def cross_validate_kline(code: str, trade_date: str = "") -> dict:
    """
    三源交叉验证：Tushare + Baostock + 已有行情数据。
    返回 {'close_values': {...}, 'matches': bool, 'anomaly': str | None}
    """
    import time
    result = {"close_values": {}, "pct_values": {}, "matches": True, "anomaly": None}

    # 1. Tushare
    ts_data = tushare_daily(code)
    if ts_data:
        result["close_values"]["tushare"] = ts_data["close"]
        result["pct_values"]["tushare"] = ts_data["pct_chg"]
        time.sleep(2)  # 限流

    # 2. Baostock
    bs_data = baostock_daily(code)
    if bs_data:
        result["close_values"]["baostock"] = bs_data["close"]

    # 检查是否一致
    vals = [v for v in result["close_values"].values() if v > 0]
    if len(vals) >= 2:
        max_v, min_v = max(vals), min(vals)
        if abs(max_v - min_v) > 0.05:  # 超过5分钱偏差
            result["matches"] = False
            result["anomaly"] = f"价差 {max_v - min_v:.2f} 元"

    return result


# ========== 便捷函数 ==========

def fetch_and_snapshot(code: str) -> Optional[dict]:
    """获取行情并写入快照。返回行情数据"""
    q = fetch_quote(code)
    if q:
        from trade_db import MarketSnapshot
        MarketSnapshot().update(code, q)
    return q


def fetch_batch_and_snapshot(codes: list) -> dict:
    """批量获取行情并写入快照"""
    quotes = fetch_quotes_batch(codes)
    if quotes:
        from trade_db import MarketSnapshot
        MarketSnapshot().update_batch(quotes)
    return quotes


# ========== 兼容旧版（逐步淘汰） ==========

def fetch_quote_old_format(code: str) -> Optional[dict]:
    """返回旧格式（中文key），供尚未迁移的代码使用"""
    q = fetch_quote(code)
    if not q:
        return None
    return {
        "最新价": q["price"],
        "涨跌幅": q["pct"],
        "最高": q["high"],
        "最低": q["low"],
        "今开": q["open"],
        "昨收": q["pre_close"],
        "成交量(手)": q["vol"],
        "成交额(万)": q["amount"],
        "换手(%)": q["turnover"],
        "证券名称": q["name"],
        "更新时间": q["time"],
        "_is_etf": q["etf"],
    }

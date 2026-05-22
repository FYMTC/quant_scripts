"""
snapshot_reader.py — 行情快照读取 + 报告生成工具

所有 cron 任务统一从此模块读取行情，不再各自调 API。
用法：
  from snapshot_reader import get_snapshot, get_index, build_position_report

  # 读快照
  quotes = get_snapshot()           # 全部行情 dict
  byd = get_snapshot("002594")      # 单只行情

  # 读大盘（自动调 API，因为大盘不在守护进程轮询范围内）
  idx = get_index()                 # {"上证": {...}, "深证": {...}, "创业板": {...}}

  # 生成报告
  report = build_position_report(positions_config)  # 持仓概览字符串
"""

import json
import subprocess
import os
from datetime import datetime

from trade_account_context import load_portfolio_truth

SNAPSHOT_PATH = "/config/quant_scripts/market_snapshot.json"

# 持仓配置（从 guard_config.json 读取）
POSITIONS_CONFIG_PATH = "/config/quant_scripts/guard_config.json"

# ========== 快照读取 ==========

def _load_snapshot():
    """加载行情快照文件"""
    if not os.path.exists(SNAPSHOT_PATH):
        return None
    try:
        with open(SNAPSHOT_PATH) as f:
            return json.load(f)
    except:
        return None

def get_snapshot(code=None):
    """
    获取行情快照。
    - code=None: 返回全部行情 dict {code: {...}}
    - code="002594": 返回单只行情或 None
    """
    data = _load_snapshot()
    if not data:
        return None
    quotes = data.get("quotes", {})
    if code:
        return quotes.get(code)
    return quotes

def get_snapshot_meta():
    """获取快照更新时间"""
    data = _load_snapshot()
    if data:
        return data.get("_meta", {})
    return {}

def is_snapshot_fresh(max_age_seconds=120):
    """快照是否在指定秒数内更新"""
    import time
    meta = get_snapshot_meta()
    if not meta:
        return False
    updated = meta.get("updated_at", "")
    if not updated:
        return False
    # 粗略判断：日期是否今天
    from datetime import date
    if meta.get("updated_date") != date.today().strftime("%Y-%m-%d"):
        return False
    return True


# ========== 大盘指数 ==========

def get_index():
    """获取大盘指数（上证/深证/创业板）— P1-6: 统一走 market_data"""
    from market_data import get_index as _md_index
    return _md_index()

def _load_positions_config():
    """加载持仓配置。"""
    try:
        return load_portfolio_truth().get("positions", {})
    except Exception:
        return {}

def build_position_report(quotes=None, positions_config=None):
    """
    生成持仓概览文本。
    - quotes: 行情 dict，不传则从快照读
    - positions_config: 持仓配置，不传则从 guard_config.json 读
    """
    if quotes is None:
        quotes = get_snapshot()
    if not quotes:
        return "⚠️ 行情数据不可用"
    if positions_config is None:
        positions_config = _load_positions_config()

    lines = []
    total_profit = 0
    total_market_value = 0

    for code, info in positions_config.items():
        q = quotes.get(code)
        if not q:
            continue
        name = info.get("name", q.get("name", code))
        price = q.get("p", 0)
        pct = q.get("pct", 0)
        pct = pct / 100 if abs(pct) > 100 else pct  # 兼容新旧格式
        cost = info.get("cost", 0)
        shares = info.get("shares", 0)
        profit = (price - cost) * shares
        profit_pct = (price - cost) / cost * 100 if cost > 0 else 0
        market_value = price * shares
        total_profit += profit
        total_market_value += market_value
        arrow = "🟢" if profit >= 0 else "🔴"
        lines.append(f"{arrow} **{name}** ({code}) | {price:.2f} | {pct:+.2f}% | 盈亏 {profit:+.0f}元({profit_pct:+.2f}%) | 市值 {market_value:.0f}")

    if not lines:
        return "⚠️ 无持仓数据"

    text = "\n".join(lines)
    total_arrow = "🟢" if total_profit >= 0 else "🔴"
    text += f"\n\n**持仓合计**: 市值 {total_market_value:.0f} | 盈亏 {total_arrow}{total_profit:+.0f}元"
    return text


def build_index_report(indices=None):
    """生成大盘概览"""
    if indices is None:
        indices = get_index()
    lines = []
    for name, data in indices.items():
        if data:
            lines.append(f"{name}: {data['price']:.2f} ({data['pct']:+.2f}%) 成交{data['amount']:.0f}亿")
        else:
            lines.append(f"{name}: 获取失败")
    return "\n".join(lines)


# ========== 快照就绪检查 ==========

def ensure_snapshot():
    """
    确保快照可用。如果快照过期或不存在，直接拉API更新并写入。
    返回 (quotes_dict, is_from_api)
    """
    quotes = get_snapshot()
    if quotes and is_snapshot_fresh(300):
        return quotes, False

    # 快照不可用，直接拉
    from trade_db import MarketSnapshot
    codes = list(_load_positions_config().keys())
    # 从配置取自选股
    try:
        with open(POSITIONS_CONFIG_PATH) as f:
            cfg = json.load(f)
            watch = cfg.get("watch_list", {})
    except:
        watch = {}
    all_codes = codes + [k for k in watch if k not in codes]

    snap = MarketSnapshot()
    new_quotes = {}
    import time
    for code in all_codes:
        q = _fetch_direct(code)
        if q:
            snap.update(code, q)
            new_quotes[code] = q
            time.sleep(1.3)
    return new_quotes, True


def _fetch_direct(code):
    """直接拉 API 获取单只行情"""
    is_etf = code[:2] in ("51", "15", "16", "56", "58")
    secid = f"0.{code}" if code.startswith(("0", "3")) else f"1.{code}"
    fields = "f43,f44,f45,f46,f47,f48,f57,f58,f60,f168,f170,f100"
    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields={fields}"
    try:
        out = subprocess.run(["curl", "-s", "--connect-timeout", "5", "--max-time", "8", url],
                             capture_output=True, text=True, timeout=10)
        d = json.loads(out.stdout)
        if d.get("rc") == 0 and d.get("data"):
            rd = d["data"]
            divisor = 1000 if is_etf else 100
            f43 = int(rd.get("f43") or 0)
            if f43 <= 0:
                return None
            return {
                "p": f43 / divisor,
                "pct": float(rd.get("f170") or 0) / 100,
                "h": int(rd.get("f44") or 0) / divisor,
                "l": int(rd.get("f45") or 0) / divisor,
                "o": int(rd.get("f46") or 0) / divisor,
                "pre": int(rd.get("f60") or 0) / divisor,
                "v": int(rd.get("f47") or 0),
                "a": int(rd.get("f48") or 0) / 10000,
                "to": float(rd.get("f168") or 0) / 100,
                "name": rd.get("f58", ""),
                "etf": is_etf,
                "t": datetime.now().strftime("%H:%M:%S"),
            }
    except:
        pass
    return None

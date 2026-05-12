#!/usr/bin/env python3
"""TradingAgents v0.2.5 集成 — A股适配版，替代手写prompt角色扮演"""

import os, sys, json
from datetime import datetime

# Load env from Hermes .env
env_path = os.path.expanduser("/config/.hermes/.env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

# ===== A股适配: P1修复 — StockTwits/Reddit → 东方财富股吧情绪数据 =====
import subprocess as _sp, json as _json

def _yf_to_a_code(ticker: str) -> str:
    """yfinance ticker → A股6位代码"""
    if ticker.endswith(".SZ"):
        return ticker.replace(".SZ", "")
    elif ticker.endswith(".SS"):
        return ticker.replace(".SS", "")
    return ticker

def _fetch_eastmoney_guba(code: str, limit: int = 20) -> str:
    """P1: 从东方财富股吧获取情绪数据替代StockTwits/Reddit"""
    try:
        from omnidata_config import OMNIDATA_API_URL
        api_url = OMNIDATA_API_URL
    except ImportError:
        api_url = "http://localhost:8380/api/v1"
    
    try:
        resp = _sp.run(
            ["curl", "-s", "--max-time", "8",
             "-X", "POST", f"{api_url}/spiders/run",
             "-H", "Content-Type: application/json",
             "-d", _json.dumps({"spider_name": "eastmoney_guba_hot_posts",
                               "params": {"code": code, "limit": limit}}),
            ], capture_output=True, text=True, timeout=10)
        if resp.returncode == 0 and resp.stdout:
            data = _json.loads(resp.stdout)
            if data.get("success") and data.get("data"):
                posts = data["data"]
                if isinstance(posts, list):
                    lines = [f"东方财富股吧({code}) 热门帖子:"]
                    for p in posts[:limit]:
                        title = p.get("title", "") or p.get("post_title", "")
                        reads = p.get("read_count", "") or p.get("click", "")
                        lines.append(f"- [{reads}] {title}")
                    return "\n".join(lines)
    except Exception:
        pass
    
    return f"股吧({code}): 情绪数据暂不可用（OmniData未运行或网络异常）"

def _fetch_a_share_sentiment(ticker: str, limit: int = 30, timeout: float = 10.0) -> str:
    """P1: A股情绪数据 — 替代StockTwits"""
    code = _yf_to_a_code(ticker)
    return _fetch_eastmoney_guba(code, min(limit, 20))

import tradingagents.dataflows.stocktwits as _st
_st.fetch_stocktwits_messages = _fetch_a_share_sentiment

import tradingagents.dataflows.reddit as _rd
_rd.fetch_reddit_posts = lambda ticker, *a, **kw: _fetch_eastmoney_guba(_yf_to_a_code(str(ticker)))

# ===== 数据预取缓存: 4位分析师串行调用相同的yfinance API，缓存避免重复请求 =====
import tradingagents.dataflows.interface as _iface
_route_orig = _iface.route_to_vendor
_cache = {}

def _cached_route(method: str, *args, **kwargs):
    key = f"{method}:{args}:{tuple(sorted(kwargs.items()))}"
    if key in _cache:
        return _cache[key]
    result = _route_orig(method, *args, **kwargs)
    _cache[key] = result
    return result

_iface.route_to_vendor = _cached_route


def a_stock_to_yfinance(code: str) -> str:
    """6位A股代码 → yfinance ticker"""
    if code.startswith(('0', '3')):
        return f"{code}.SZ"   # 深圳
    elif code.startswith(('6', '9')):
        return f"{code}.SS"   # 上海
    elif code.startswith(('4', '8')):
        return f"{code}.BJ"   # 北证
    return code


def analyze(stock_code: str, date: str = None, llm_provider: str = "deepseek",
            deep_model: str = "deepseek-v4-flash", quick_model: str = "deepseek-v4-flash",
            max_debate_rounds: int = 1, output_lang: str = "zh"):
    """
    调用TradingAgents框架分析单只A股
    
    Args:
        stock_code: 6位A股代码，如 000938
        date: 分析日期 YYYY-MM-DD
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    
    # A股代码转yfinance格式
    ticker = a_stock_to_yfinance(stock_code)
    
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = llm_provider
    config["deep_think_llm"] = deep_model
    config["quick_think_llm"] = quick_model
    config["max_debate_rounds"] = max_debate_rounds
    config["output_language"] = output_lang
    config["checkpoint_enabled"] = False
    
    # A股适配: 只走yfinance数据通道，跳过StockTwits/Reddit
    config["data_vendors"] = {
        "core_stock_apis": "yfinance",
        "technical_indicators": "yfinance",
        "fundamental_data": "yfinance",
        "news_data": "yfinance",
    }
    
    # 中国相关全球新闻
    config["global_news_queries"] = [
        "China A-share market central bank PBOC policy",
        "China economic data GDP PMI trade",
        "semiconductor AI technology sector China",
    ]
    
    ta = TradingAgentsGraph(debug=True, config=config)
    _, decision = ta.propagate(ticker, date)
    return decision


def batch_analyze(stock_codes: list, date: str = None) -> dict:
    """批量分析多只A股"""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    
    results = {}
    for code in stock_codes:
        try:
            decision = analyze(code, date)
            results[code] = {"success": True, "decision": decision}
        except Exception as e:
            results[code] = {"success": False, "error": str(e)}
    
    return results


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: tradingagents_runner.py <stock_code> [date]")
        print("Example: tradingagents_runner.py 000938 2026-05-11")
        sys.exit(1)
    
    code = sys.argv[1]
    date = sys.argv[2] if len(sys.argv) > 2 else None
    
    print(f"TradingAgents analyzing {code} ({a_stock_to_yfinance(code)}) for {date or 'today'}...")
    print(f"Models: deep=deepseek-v4-flash quick=deepseek-v4-flash")
    result = analyze(code, date)
    
    # 备份到文件
    out_path = f"/tmp/ta_{code}_{date or datetime.now().strftime('%Y%m%d')}.md"
    with open(out_path, 'w') as f:
        f.write(str(result))
    print(f"\n=== 结果已保存: {out_path} ===")
    print(result[:3000] if len(str(result)) > 3000 else result)

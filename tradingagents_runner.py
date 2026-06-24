#!/usr/bin/env python3
"""TradingAgents v0.2.5 集成 — A股适配版，替代手写prompt角色扮演"""

import os, sys, json
from datetime import datetime

# Load env from Hermes .env
env_path = cfg.path.hermes_env
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
from system_config import cfg
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

# ===== Q-phase集成: 量化上下文注入 =====

def fetch_quant_context(stock_code: str) -> str:
    """
    获取标的全套量化指标上下文，注入 TradingAgents 分析。
    
    解决痛点：量化引擎和信号链断裂——GARCH/HMM/CVaR/动量数据产出但LLM分析师收不到。
    """
    context_parts = ["[量化上下文 — 自动注入]"]
    
    # 1. 行情 + 技术指标
    try:
        from data_converter import fetch_kline_baostock
        from datetime import date
        end = date.today().strftime("%Y%m%d")
        start = (date.today().replace(year=date.today().year-1)).strftime("%Y%m%d")
        records = fetch_kline_baostock(stock_code, start, end)
        if records and len(records) >= 20:
            closes = [float(r['收盘']) for r in records]
            latest = closes[-1]
            prev = closes[-2] if len(closes) > 1 else latest
            chg = (latest - prev) / prev * 100
            
            # 均线
            ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else latest
            ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else latest
            ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else latest
            
            context_parts.append(f"\n价格: ¥{latest:.2f} (日变动{chg:+.2f}%)")
            context_parts.append(f"均线: MA5={ma5:.2f} MA20={ma20:.2f} MA60={ma60:.2f}")
            context_parts.append(f"MA状态: {'多头' if latest > ma5 > ma20 else '空头' if latest < ma5 < ma20 else '震荡'}")
            
            # 2. 风险指标
            from risk_metrics import calc_cvar, calc_multi_momentum, calc_garch_vol, calc_gbm_cvar
            
            cvar = calc_cvar(closes)
            mom = calc_multi_momentum(closes)
            garch = calc_garch_vol(closes)
            
            if cvar is not None:
                context_parts.append(f"\nCVaR(95%): {cvar*100:.1f}% (日亏损风险)")
            if mom:
                context_parts.append(f"动量: 1d={mom['1d']:+.1f}% 7d={mom['7d']:+.1f}% 30d={mom['30d']:+.1f}% 一致性={mom['consistency']:.0%}")
            if garch and garch.get('converged'):
                context_parts.append(f"GARCH波动率: 条件={garch['ann_vol']:.1%} 历史={garch['simple_ann_vol']:.1%} 状态={garch['vol_regime']}")
                context_parts.append(f"GARCH参数: α={garch['params']['alpha']} β={garch['params']['beta']} 持续性={garch['params']['persistence']}")
            
            # 3. HMM市场状态
            try:
                from market_regime import fit_hmm, fetch_index_data
                import numpy as np
                idx_closes = fetch_index_data('000001', 500)
                if idx_closes is not None and len(idx_closes) >= 60:
                    idx_rets = np.diff(np.log(idx_closes))
                    hmm_result = fit_hmm(idx_rets)
                    if hmm_result and 'error' not in hmm_result:
                        state = hmm_result['current_state']
                        probs = hmm_result['current_probs']
                        context_parts.append(f"\nHMM市场状态: {state}")
                        context_parts.append(f"概率: 熊市{probs[0]:.0%} 震荡{probs[1]:.0%} 牛市{probs[2]:.0%}")
                        if state == 'bear':
                            context_parts.append("⚠️ 熊市模式: BUY阈值应从0.8上调至1.2，止损从-5%收紧至-3%")
            except Exception:
                pass
            
            # 4. GBM风险
            gbm = calc_gbm_cvar(closes)
            if gbm and 'error' not in gbm:
                context_parts.append(f"\nGBM-CVaR(20d): {gbm['cvar']*100:.1f}% (10000路径模拟)")
                if gbm.get('historical_cvar'):
                    context_parts.append(f"对比: 历史CVaR={gbm['historical_cvar']*100:.1f}%")
            
            # 5. 前高/阻力位
            if len(closes) >= 60:
                recent_high = max(closes[-60:])
                from_high = (latest - recent_high) / recent_high * 100
                context_parts.append(f"\n60日前高: ¥{recent_high:.2f} (距前高{from_high:+.1f}%)")
                if abs(from_high) < 5 and chg > 0:
                    context_parts.append("WARNING: 接近前高阻力位! 跳空+冲高回落=假突破概率增大")
    except Exception as e:
        context_parts.append(f"\n(量化上下文获取异常: {e})")
    
    return "\n".join(context_parts)


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
    
    # Q-phase集成: 注入量化上下文
    quant_ctx = fetch_quant_context(stock_code)
    config["quant_context"] = quant_ctx
    # 将量化数据追加到新闻查询中，确保LLM分析师看到
    config["global_news_queries"].append(f"QUANTITATIVE DATA for {ticker}: {quant_ctx[:500]}")
    
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

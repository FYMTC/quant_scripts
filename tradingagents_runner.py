#!/usr/bin/env python3
"""TradingAgents v0.2.5 集成 — A股适配版，替代手写prompt角色扮演"""

import os, sys, json
from datetime import datetime

from system_config import cfg

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

# ===== tradingagents 包可选导入：包未装时优雅降级 =====
# fetch_quant_context() 仅依赖 risk_metrics/market_regime/data_converter，不需要 tradingagents 包，
# 因此即使 TA 包未安装，量化上下文注入仍可工作；仅 analyze() 多分析师评分不可用。
# TA 0.7.0 API：TradingAgentsConfig（pydantic）替代 DEFAULT_CONFIG（dict）；TradingAgentsGraph(config=...)
_TA_AVAILABLE = False
TradingAgentsGraph = None
TradingAgentsConfig = None
try:
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.config import TradingAgentsConfig
    _TA_AVAILABLE = True
except ImportError as _e:
    # 包未安装：仅记录，不抛出，保证 fetch_quant_context 仍可被 agent_desk 调用
    _TA_IMPORT_ERROR = str(_e)

# DeepSeek 兼容 OpenAI API：TA 0.7.0 llm_provider 不支持 "deepseek"，用 "openai" + env 映射
if os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = os.environ["DEEPSEEK_API_KEY"]
if not os.environ.get("OPENAI_API_BASE"):
    os.environ["OPENAI_API_BASE"] = "https://api.deepseek.com/v1"

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

# ===== A股适配 monkeypatch（TA 0.7.0 已移除 stocktwits/reddit/interface 模块）=====
# 0.7.0 dataflows 改为 news.py/providers.py/yfinance.py；A股情绪数据（东方财富股吧）注入待重写。
# _fetch_eastmoney_guba/_fetch_a_share_sentiment 保留备用，待 news.py 适配后接入。


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


def analyze(stock_code: str, date: str = None, llm_provider: str = "openai",
            deep_model: str = "deepseek-chat", quick_model: str = "deepseek-chat",
            max_debate_rounds: int = 1, output_lang: str = "zh-CN"):
    """
    调用TradingAgents 0.7.0 框架分析单只A股
    
    Args:
        stock_code: 6位A股代码，如 000938
        date: 分析日期 YYYY-MM-DD
        llm_provider: TA 0.7.0 仅支持 openai/anthropic/google_genai/xai/openrouter/ollama/litellm；
                     DeepSeek 通过 openai 兼容 API（env 映射在模块顶部：DEEPSEEK_API_KEY→OPENAI_API_KEY）
    """
    if not _TA_AVAILABLE:
        raise RuntimeError(
            f"TradingAgents 包未安装，多分析师评分不可用（import error: {_TA_IMPORT_ERROR}）。"
            f"fetch_quant_context() 仍可独立工作。"
        )
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    
    # A股代码转yfinance格式
    ticker = a_stock_to_yfinance(stock_code)
    
    # TA 0.7.0: TradingAgentsConfig（pydantic）替代 DEFAULT_CONFIG（dict）
    # 旧 output_lang → 新 response_language（zh-CN）；llm_provider deepseek → openai 兼容
    lang = output_lang if output_lang in ("zh-CN", "zh-TW", "en-US") else "zh-CN"
    config = TradingAgentsConfig(
        llm_provider=llm_provider,
        deep_think_llm=deep_model,
        quick_think_llm=quick_model,
        max_debate_rounds=max_debate_rounds,
        max_risk_discuss_rounds=1,
        max_recur_limit=100,
        response_language=lang,
    )
    
    # Q-phase集成: 注入量化上下文
    # TA 0.7.0 没有 config["quant_context"]；通过实例级 monkeypatch propagator.create_initial_state，
    # 把量化上下文作为 SystemMessage 插入 AgentState.messages，让所有分析师/辩论/风控 LLM 都能看到
    quant_ctx = fetch_quant_context(stock_code)
    
    ta = TradingAgentsGraph(config=config, debug=True)
    _propagator = ta.propagator
    _orig_cis = _propagator.create_initial_state
    from langchain_core.messages import SystemMessage as _SystemMessage
    
    def _cis_with_quant(company_name, trade_date, _orig=_orig_cis, _ctx=quant_ctx):
        state = _orig(company_name, trade_date)
        state.messages.insert(0, _SystemMessage(content=f"[量化上下文 — 自动注入]\n{_ctx}"))
        return state
    
    _propagator.create_initial_state = _cis_with_quant
    
    _, recommendation = ta.propagate(ticker, date)
    
    # TA 0.7.0 返回 TradeRecommendation 对象；旧调用方期望 str/decision
    if hasattr(recommendation, "model_dump_json"):
        return recommendation.model_dump_json()
    if hasattr(recommendation, "json"):
        return recommendation.json()
    return str(recommendation)


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

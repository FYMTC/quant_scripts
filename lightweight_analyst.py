#!/usr/bin/env python3
"""轻量版TradingAgents v2 — 直调DeepSeek flash API，4维快速筛选
v2: 增加东方财富行情+财务公告抓取，缩小与全量版差距
用法: python lightweight_analyst.py <stock_code> [--context "补充信息"]
输出: JSON (scores/verdict/composite)
命名: lightweight_analyst.py — 与cron的delegate_task模拟模式区分
"""

import os, sys, json, urllib.request, re
from datetime import datetime

from system_config import cfg

# 加载.env
env_path = cfg.path.hermes_env
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
if not API_KEY:
    print(json.dumps({"error": "DEEPSEEK_API_KEY not set"}))
    sys.exit(1)

from omnidata_config import OMNIDATA_API_URL
OMNIDATA = f"{OMNIDATA_API_URL}/spiders/run"

def api_call(spider: str, params: dict, timeout: int = 10):
    """通用OmniData API调用"""
    payload = json.dumps({"spider_name": spider, "params": params}).encode()
    req = urllib.request.Request(OMNIDATA, data=payload,
        headers={"Content-Type": "application/json"})
    data = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    return data.get("data") if data.get("success") else None

def fetch_quote(code: str) -> str:
    """获取实时行情+估值"""
    try:
        data = api_call("eastmoney_stock_quote", {"stock_code": code})
        if not data:
            return "行情不可用"
        fields = []
        for k in ["最新价", "涨跌幅", "总市值(亿元)", "市盈率(动态)", "市净率", 
                   "换手(%)", "量比", "52周最高", "52周最低"]:
            if k in data:
                fields.append(f"{k}: {data[k]}")
        return ", ".join(fields)
    except Exception as e:
        return f"行情失败: {e}"

def fetch_financial_news(code: str) -> str:
    """获取财务相关新闻+公告"""
    lines = []
    # 新闻
    try:
        data = api_call("eastmoney_search", {"keyword": code, "search_type": "news", "page_size": 5})
        if data and isinstance(data, list):
            for item in data[:3]:
                title = item.get("标题", "")[:80]
                t = item.get("时间", "")[:16]
                if any(kw in title for kw in ["业绩", "利润", "营收", "订单", "合同", "中标", "减持", "质押"]):
                    lines.append(f"[新闻 {t}] {title}")
    except: pass
    
    # 财务公告
    try:
        data = api_call("eastmoney_search", {"keyword": code, "search_type": "ann", "page_size": 3})
        if data and isinstance(data, list):
            for item in data[:2]:
                title = item.get("标题", "")[:80]
                t = item.get("时间", "")[:16]
                lines.append(f"[公告 {t}] {title}")
    except: pass
    
    return "\n".join(lines) if lines else "无财务相关信息"

def fetch_baostock_financials(code: str) -> str:
    """从Baostock获取最近4季度关键财务指标"""
    try:
        import subprocess, tempfile
        # 确定baostock代码格式
        if code.startswith(('0', '3')):
            bs_code = f"sz.{code}"
        elif code.startswith(('6', '9')):
            bs_code = f"sh.{code}"
        else:
            return ""
        
        script = f"""
import baostock as bs, json
from system_config import cfg
bs.login()
fields = "code,report_date,roeAvg,roeTTM,epsTTM,npMargin,debtToAssets,currentRatio,grossProfitMargin"
rs = bs.query_growth_data("{bs_code}", fields, year=2026, quarter=1)
data = []
while (rs.error_code == '0') and rs.next():
    data.append(rs.get_row_data())
if not data:
    rs = bs.query_growth_data("{bs_code}", fields, year=2025, quarter=4)
    while (rs.error_code == '0') and rs.next():
        data.append(rs.get_row_data())
bs.logout()

if data:
    latest = data[0]
    print(f"ROE:{{latest[2]}}%|EPS:{{latest[4]}}|净利率:{{latest[5]}}%|负债率:{{latest[6]}}%|流动比:{{latest[7]}}|毛利:{{latest[8]}}%")
else:
    print("无财务数据")
"""
        r = subprocess.run([cfg.python, "-c", script],
                          capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception as e:
        return f"[财务数据: {e}]"
    return "无财务数据"

def call_flash(prompt: str, max_tokens: int = 1000) -> str:
    """调用DeepSeek v4-flash"""
    # P3-1: base url 支持 env 覆盖（DEEPSEEK_BASE_URL），默认 https://api.deepseek.com
    deepseek_base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    req = urllib.request.Request(
        f"{deepseek_base}/chat/completions",
        data=json.dumps({
            "model": "deepseek-v4-flash",
            "messages": [
                {"role": "system", "content": "你是量化分析助手。只输出JSON，不输出解释。评分严格——不因主题热就给高分，看实际财务数据。"},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        }
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
    return resp["choices"][0]["message"]["content"]

def analyze(code: str, context: str = "") -> dict:
    """轻量版四维分析v2 — 加入行情+财务数据"""
    quote = fetch_quote(code)
    news = fetch_financial_news(code)
    baostock = fetch_baostock_financials(code)
    
    prompt = f"""分析A股{code}，四维评分。以下是收集到的客观数据，基于数据评分，不要凭空猜测。

{context}

【实时行情】{quote}
【财务指标】{baostock}
【相关公告/新闻】
{news[:1000]}

评分规则（严格）：
- catalyst(催化): +2=国策级利好+业绩验证, +1=主题匹配但未兑现, 0=无催化, -1=利空压制
- valuation(估值): +2=PE<15且利润增长, +1=PE合理<行业, 0=PE持平行业, -1=PE偏高或利润下滑, -2=PE虚高或亏损
- technical(技术): +2=放量突破+均线多头, +1=趋势向上, 0=震荡, -1=破位下行
- risk(风险): +2=无硬伤, +1=小瑕疵, 0=一般, -1=显著缺陷(高负债/质押/减持), -2=致命缺陷(亏损/ST/违规)
composite = (catalyst+valuation+technical+risk)/4.0

输出纯JSON：
{{"code":"{code}","name":"股票名","model":"deepseek-v4-flash","scores":{{"catalyst":X,"valuation":X,"technical":X,"risk":X}},"composite":X.X,"verdict":"BUY|OVERWEIGHT|HOLD|UNDERWEIGHT|SELL","reasoning":"基于数据的判断","key_risk":"最大风险点"}}"""

    result = call_flash(prompt, max_tokens=1000)
    result = result.strip()
    result = re.sub(r'```[^`]*```', '', result)
    
    # 找JSON
    match = re.search(r'\{[^{}]*"composite"[^{}]*\}', result, re.DOTALL)
    if not match:
        match = re.search(r'\{.+"verdict".+\}', result, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except:
            pass
    raise Exception(f"无法解析JSON\n原始({len(result)}字):\n{result[:600]}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python lightweight_analyst.py <stock_code> [--context 'info']")
        sys.exit(1)
    
    code = sys.argv[1]
    context = ""
    if "--context" in sys.argv:
        idx = sys.argv.index("--context")
        context = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
    
    try:
        result = analyze(code, context)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"code": code, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)

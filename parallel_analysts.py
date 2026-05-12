"""
parallel_analysts.py — TradingAgents 多角色分析师团队并行分析脚本

借鉴 TradingAgents (arXiv 2412.20138) 的7角色分工架构，
用 Hermes delegate_task 实现并行分析师团队。

用法：
  python parallel_analysts.py 002594  --analysts technical,news,fundamental  --date 2026-04-28

分析完成后，结果输出为 JSON 到 quant_scripts/analyst_reports/ 目录。
"""

import json
import sys
import os
import subprocess
from datetime import datetime
from typing import List, Dict, Optional

# ========== 常量 ==========

OMNIDATA_URL = "http://172.17.0.3:8380"
REPORTS_DIR = os.path.join(os.path.dirname(__file__), "analyst_reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

# 持仓配置（用于获取成本和现价）
POSITIONS_PATH = "/config/quant_scripts/guard_config.json"
SNAPSHOT_PATH = "/config/quant_scripts/market_snapshot.json"

# P1-1 修复: 从 DB 读取持仓名称映射
def _get_position_names() -> dict:
    """从 stock_kb DB 读取持仓代码→名称映射"""
    try:
        from stock_kb import StockKB
        kb = StockKB()
        pf = kb.read_portfolio_truth()
        return {code: info["name"] for code, info in pf["positions"].items()}
    except Exception:
        return {}

# 分析师角色定义
ANALYST_ROLES = {
    "technical": {
        "name": "技术面分析师",
        "goal": (
            "分析{ticker}的技术面。输出：K线形态、均线排列（5/10/20/60日）、"
            "MACD状态、RSI值、布林带位置、成交量异动。打分-2到+2。"
        ),
        "data_hint": "使用OmniData获取K线数据 POST {omnidata}/api/v1/spiders/run "
                      '{{"spider_id": "eastmoney_kline", "data_format": "json", '
                      '"params": {{"stock_code": "{ticker}", "kline_type": "daily", '
                      '"start_date": "{lookback}", "end_date": "{date}"}}}}'
    },
    "sentiment": {
        "name": "情绪面分析师",
        "goal": (
            "分析{ticker}的市场情绪。输出：近期散户讨论热度、资金流向（主力/散户净流入）、"
            "龙虎榜数据（如有）、涨跌家数比。打分-2到+2。"
        ),
        "data_hint": (
            "使用OmniData获取资金流向 POST {omnidata}/api/v1/spiders/run "
            '{{"spider_id": "eastmoney_capital_flow", "data_format": "json", '
            '"params": {{"stock_code": "{ticker}"}}}}'
        )
    },
    "news": {
        "name": "新闻面分析师",
        "goal": (
            "分析{ticker}的新闻面。输出：近3天重大新闻（业绩/行业政策/机构研报）、"
            "公司公告、行业动向、主要机构评级变化。打分-2到+2。"
        ),
        "data_hint": (
            "使用web_search搜索'{name} 2026年{month}月 新闻'补充信息，"
            "同时用OmniData获取新闻 POST {omnidata}/api/v1/spiders/run "
            '{{"spider_id": "eastmoney_news", "data_format": "json", '
            '"params": {{"stock_code": "{ticker}"}}}}'
        )
    },
    "fundamental": {
        "name": "基本面分析师",
        "goal": (
            "分析{ticker}的基本面。输出：当前PE/PB/ROE、营收增速、利润增速、"
            "行业地位、估值历史分位。打分-2到+2。"
        ),
        "data_hint": (
            "使用web_search搜索'{name} PE PB ROE 估值 2026'补充数据，"
            "用OmniData获取财务数据 POST {omnidata}/api/v1/spiders/run "
            '{{"spider_id": "eastmoney_financial", "data_format": "json", '
            '"params": {{"stock_code": "{ticker}"}}}}'
        )
    }
}


def get_stock_name(ticker: str) -> str:
    """从快照或配置文件读取股票名称"""
    # 优先从快照读
    if os.path.exists(SNAPSHOT_PATH):
        try:
            with open(SNAPSHOT_PATH, "r") as f:
                snap = json.load(f)
            if ticker in snap:
                return snap[ticker].get("name", "")
        except:
            pass
    
    # 从持仓配置读
    # P1-1 修复: 优先从DB读取
    pos_names = _get_position_names()
    if ticker in pos_names:
        return pos_names[ticker]
    if os.path.exists(POSITIONS_PATH):
        try:
            with open(POSITIONS_PATH, "r") as f:
                config = json.load(f)
            for pos in config.get("positions", []):
                if pos.get("code") == ticker:
                    return pos.get("name", "")
        except:
            pass
    
    return ticker


def fetch_quick_quote(ticker: str) -> Optional[Dict]:
    """快速获取实时行情（调用market_data模块）"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", 
             f"import sys; sys.path.insert(0,'/config/quant_scripts'); "
             f"from market_data import fetch_quote; "
             f"import json; print(json.dumps(fetch_quote('{ticker}')))"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except:
        pass
    return None


def build_analyst_tasks(ticker: str, date: str, analysts: List[str]) -> List[Dict]:
    """构建分析师subagent任务列表"""
    name = get_stock_name(ticker)
    month = date.split("-")[1]
    # 回看30天
    try:
        d = datetime.strptime(date, "%Y-%m-%d")
        from datetime import timedelta
        lookback = (d - timedelta(days=30)).strftime("%Y-%m-%d")
    except:
        lookback = date
    
    tasks = []
    for role in analysts:
        if role not in ANALYST_ROLES:
            continue
        info = ANALYST_ROLES[role]
        
        # 构建context
        context = info["data_hint"].format(
            ticker=ticker, name=name, date=date, 
            month=month, lookback=lookback, omnidata=OMNIDATA_URL
        )
        context += (
            f"\n\n当前行情参考："
        )
        quote = fetch_quick_quote(ticker)
        if quote:
            context += (
                f"收盘价{quote.get('price','?')}元，"
                f"涨跌幅{quote.get('pct','?')}%，"
                f"成交量{quote.get('vol','?')}手"
            )
        else:
            context += "行情数据待subagent自行获取"
        
        tasks.append({
            "goal": info["goal"].format(ticker=ticker, name=name),
            "context": context,
            "toolsets": ["terminal", "web"]
        })
    
    return tasks


def save_report(ticker: str, date: str, analysts: List[str], 
                reports: List[Dict], summary: str):
    """保存分析报告到文件"""
    report = {
        "ticker": ticker,
        "date": date,
        "analysts": analysts,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reports": reports,
        "summary": summary
    }
    
    filename = f"{ticker}_{date}_analyst_report.json"
    path = os.path.join(REPORTS_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    # 同时保存人类可读的markdown版本
    md_path = path.replace(".json", ".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(summary)
    
    print(f"\n📁 报告已保存:")
    print(f"  JSON: {path}")
    print(f"  Markdown: {md_path}")
    return path


def main():
    import argparse
    parser = argparse.ArgumentParser(description="TradingAgents 分析师团队并行分析")
    parser.add_argument("ticker", help="股票代码，如 002594")
    parser.add_argument("--analysts", default="technical,news,fundamental",
                      help="分析角色，逗号分隔。可选: technical,sentiment,news,emotional,policy")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                      help="分析日期 YYYY-MM-DD")
    parser.add_argument("--output", help="输出报告路径（可选，默认自动生成）")
    
    args = parser.parse_args()
    ticker = args.ticker
    date = args.date
    analysts = [a.strip() for a in args.analysts.split(",") if a.strip()]
    
    print(f"""
╔══════════════════════════════════════════════╗
║  TradingAgents 分析师团队启动                 ║
╠══════════════════════════════════════════════╣
║  标的: {ticker:<6} {get_stock_name(ticker):<16} ║
║  日期: {date:<24} ║
║  分析师: {','.join(analysts):<22} ║
╚══════════════════════════════════════════════╝
""")
    
    print("📡 构建分析师任务...")
    tasks = build_analyst_tasks(ticker, date, analysts)
    
    print(f"🚀 启动 {len(tasks)} 位并行分析师...")
    print(f"  角色: {', '.join(ANALYST_ROLES[r]['name'] for r in analysts)}")
    
    # 输出任务配置（供Hermes使用）
    config = {
        "action": "delegate_task",
        "tasks": tasks
    }
    config_path = os.path.join(REPORTS_DIR, f"{ticker}_{date}_tasks.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    
    print(f"\n✅ 任务配置已生成: {config_path}")
    print()
    print("=" * 60)
    print("📋 下一步: 在Hermes中执行 delegate_task，传入上述tasks")
    print("  或: 运行 consolidate_report.py 综合裁决")
    print("=" * 60)


if __name__ == "__main__":
    main()

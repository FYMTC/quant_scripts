#!/usr/bin/env python3
"""
量化分析全能报告生成器

每天自动运行，输出：
1. 最新大盘资金流向
2. 自选股多因子评分排序
3. 滚动预测信号
4. 风险分析
5. 板块热点
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from data_converter import run_omnidata_spider, STOCK_MAP
from factor_screening import factor_screening, format_screening_report
from rolling_predict import run_rolling_pipeline
from risk_portfolio import individual_risk_analysis, format_risk_report, \
    portfolio_optimization, format_portfolio_report


# 自选股列表
FOCUS_STOCKS = [
    "600105", "600522", "600487",  # 通信
    "300589", "002297", "600118",  # 军工
    "002466", "002594", "002506", "002309",  # 新能源
    "002415", "600703", "002049",  # 蓝筹科技
    "603950", "688551", "000720",  # 小盘
    "000001", "000858", "600519", "600887",  # 蓝筹消费
    "518880",  # 黄金ETF
]

# 排除已知故障
WORKING_STOCKS = [c for c in FOCUS_STOCKS if c not in (
    "600487", "000720", "600118", "002506", "600707", "603950", "688551", "688015", "002309")]


def get_market_overview():
    """获取大盘概况"""
    result = run_omnidata_spider("eastmoney_market_flow", {"limit": 1})
    if result and result.get("success") and result.get("data"):
        data = result["data"][0]
        return {
            "上证": f"{data.get('上证收盘价', 'N/A')} ({data.get('上证涨跌幅(%)', 'N/A'):+.2f}%)",
            "深证": f"{data.get('深证收盘价', 'N/A')} ({data.get('深证涨跌幅(%)', 'N/A'):+.2f}%)",
            "主力净流入": f"{data.get('主力净流入净额(亿元)', 0):.2f}亿",
            "超大单净流入": f"{data.get('超大单净流入净额(亿元)', 0):.2f}亿",
        }
    return None


def get_sector_top():
    """获取热门板块排行"""
    result = run_omnidata_spider("eastmoney_industry_sector_flow", {"limit": 5})
    if result and result.get("success") and result.get("data"):
        data = sorted(result["data"], key=lambda x: x["涨跌幅(%)"], reverse=True)
        top5 = [f"{s['板块名称']}({s['涨跌幅(%)']:+.2f}%)" for s in data[:5]]
        return top5
    return None


def get_news_highlights():
    """获取最新财经快讯"""
    import urllib.request
    # 先用东方财富快讯（更稳定）
    for spider, params in [
        ("eastmoney_fast_news", {"page_size": 5, "fast_column": "102"}),
        ("wallstreetcn_global_news", {"channel": "a-stock", "limit": 3}),
        ("cls_global_news", {"symbol": "重点", "rn": 3}),
    ]:
        data = json.dumps({"spider_name": spider, "params": params}).encode()
        req = urllib.request.Request(
            "http://172.17.0.3:8380/api/v1/spiders/run",
            data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())
                if result.get("success") and result.get("data"):
                    items = result["data"].get("news_list") or result["data"].get("list") or []
                    if items:
                        return [(item.get("title", item.get("内容", ""))[:60],
                                 item.get("pub_time", "")[:10]) for item in items[:3]]
        except Exception:
            continue
    return None


def generate_full_report():
    """生成完整量化分析报告"""
    lines = []
    lines.append("📊 **每日量化分析报告**")
    import datetime
    lines.append(f"🗓 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # 1. 大盘
    lines.append("━" * 25)
    lines.append("**一、大盘概况**")
    market = get_market_overview()
    if market:
        for k, v in market.items():
            lines.append(f"├ {k}: {v}")
    lines.append("")

    # 2. 板块热点
    lines.append("**二、热门板块 (Top5)**")
    sectors = get_sector_top()
    if sectors:
        for i, s in enumerate(sectors, 1):
            lines.append(f"├ {i}. {s}")
    lines.append("")

    # 3. 快讯
    lines.append("**三、今日要闻**")
    news = get_news_highlights()
    if news:
        for title, t in news:
            lines.append(f"├ {title}")
    lines.append("")

    # 4. 多因子选股排序
    lines.append("**四、自选股多因子评分**")
    results = factor_screening(WORKING_STOCKS)
    if results:
        for grade, icon in [("值得操作", "✅"), ("可留观察", "🟡"), ("建议剔除", "🔴")]:
            group = [r for r in results if r["grade"] == grade]
            if not group:
                continue
            lines.append(f"{icon} **{grade}** ({len(group)}只)")
            for r in group[:5]:  # 每组最多5只
                pct = f"{r['pct_change']:+.2f}%" if r.get('pct_change') else "N/A"
                lines.append(f"  {r['name']}({r['code']}) {r['close']} | {pct} | 评分{r['composite_score']}")
        lines.append(f"  ... 全部 {len(results)} 只")
    lines.append("")

    # 5. 滚动预测信号
    lines.append("**五、AI滚动预测信号**")
    predictions = run_rolling_pipeline(
        [c for c in WORKING_STOCKS if c != "518880"],
        start_date="20230101", end_date="20250426",
    )
    if predictions:
        for s in predictions[:8]:
            icon = "🟢" if s["signal"] == "看涨" else ("🔴" if s["signal"] == "看跌" else "🟡")
            lines.append(f"{icon} {s['name']} {s['signal']}({s.get('probability', '?')}%) | {s.get('trend', '?')}")
    lines.append("")

    # 6. 核心持仓风险
    lines.append("**六、核心持仓风险分析**")
    for code in ["002594", "518880", "600519"]:
        risk = individual_risk_analysis(code)
        if risk:
            name = STOCK_MAP.get(code, code)
            lines.append(f"**{name}**({code})")
            lines.append(f"├ 年化波动: {risk['年化波动率(%)']}% | 最大回撤: {risk['最大回撤(%)']}%")
            lines.append(f"├ VaR: {risk['VaR_95(%)']}% | 夏普: {risk['夏普比']}")
            lines.append(f"└ 近60日: {risk['近60日收益(%)']:+.2f}% | 连续下跌: {risk['最大连续下跌天数']}天")
            lines.append("")

    # 7. 组合优化建议
    lines.append("**七、组合优化建议**")
    risk_free_stocks = [c for c in ["002594", "600519", "000858", "000001", "601318"]
                        if c in WORKING_STOCKS]
    if len(risk_free_stocks) >= 2:
        portfolio = portfolio_optimization(risk_free_stocks)
        if portfolio:
            for method, data in portfolio["methods"].items():
                top_weights = sorted(zip(portfolio["stock_names"], data["weights"]),
                                     key=lambda x: x[1], reverse=True)[:3]
                line = f"├ {method}: 年化{data['年化收益(%)']}% 波动{data['年化波动(%)']}% 夏普{data['夏普比']}"
                lines.append(line)
                for name, w in top_weights:
                    if w > 5:
                        lines.append(f"│   {name}: {w}%")

    return "\n".join(lines)


if __name__ == "__main__":
    report = generate_full_report()
    print(report)

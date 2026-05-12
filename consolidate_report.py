"""
consolidate_report.py — TradingAgents 分析师报告综合裁决脚本

读取 parallel_analysts.py 产出的分析师报告，进行加权评分综合裁决，
输出结构化决策建议（action_plan 格式）。

用法：
  python consolidate_report.py 002594 2026-04-28
  python consolidate_report.py --input /path/to/report.json

输出：
  - 结构化的决策 JSON
  - 人类可读的报告摘要
"""

import json
import sys
import os
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ========== 常量 ==========

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "analyst_reports")

# 各维度默认权重
DEFAULT_WEIGHTS = {
    "technical": 0.30,
    "sentiment": 0.10,
    "news": 0.35,
    "fundamental": 0.25,
}

# 评分到操作映射
SCORE_TO_ACTION = [
    (1.5, "BUY",       "强烈买入，全面看多"),
    (0.8, "BUY",       "偏多，适度加仓"),
    (0.3, "OVERWEIGHT","谨慎乐观，小幅加仓"),
    (-0.3, "HOLD",     "观望，维持现有仓位"),
    (-0.8, "UNDERWEIGHT","偏空，减仓"),
    (-1.5, "SELL",     "强烈看空，清仓"),
    (-99, "SELL",       "极端看空，立即离场"),
]


def find_report(ticker: str, date: str) -> Optional[str]:
    """查找分析师报告文件"""
    # 尝试精确匹配
    pattern = f"{ticker}_{date}_analyst_report.json"
    path = os.path.join(REPORTS_DIR, pattern)
    if os.path.exists(path):
        return path
    
    # 模糊匹配
    for f in os.listdir(REPORTS_DIR):
        if f.startswith(ticker) and f.endswith(".json"):
            path = os.path.join(REPORTS_DIR, f)
            try:
                with open(path, "r") as fh:
                    data = json.load(fh)
                if data.get("ticker") == ticker:
                    return path
            except:
                continue
    return None


def load_report(path: str) -> Dict:
    """加载报告文件"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_score_from_report(report: Dict, role: str) -> Tuple[float, str]:
    """从单个分析师报告中提取评分和摘要
    
    尝试多种方式提取分数：
    1. 报告中的 explicit 评分字段
    2. 关键词匹配（'评分'、'打分'、'综合'等）
    3. 情感分析启发式
    """
    text = report.get("summary", "")
    if not text:
        return 0.0, "无法解析"
    
    score = 0.0
    summary = ""
    
    # 提取摘要（取前300字符内含评分的关键部分）
    lines = text.split("\n")
    for line in lines:
        # 匹配 "评分: +1.5" 或 "综合打分: -1" 等
        m = re.search(r'[综合]?[评分分打].*?[：:]\s*([+-]?\d+\.?\d*)', line)
        if m:
            score = float(m.group(1))
            summary = line.strip()[:200]
            break
        
        # 匹配表格中的评分行
        m = re.search(r'\|.*?\|\s*([+-]?\d+\.?\d*)\s*\|', line)
        if m:
            score = float(m.group(1))
            summary = line.strip()[:200]
            break
    
    if not summary:
        # 尝试从末尾几行提取结论
        for line in lines[-5:]:
            if any(kw in line for kw in ["结论", "判断", "评级", "综合"]):
                summary = line.strip()[:200]
                break
        if not summary:
            summary = text[:200] + "..."
    
    return score, summary


def score_to_action(composite_score: float) -> Tuple[str, str, float]:
    """综合评分转为操作指令"""
    for threshold, action, description in SCORE_TO_ACTION:
        if composite_score >= threshold:
            return action, description, threshold
    
    return "SELL", "强烈看空", -99


def generate_verdict(
    ticker: str, 
    name: str,
    date: str,
    scores: Dict[str, float],
    summaries: Dict[str, str],
    weights: Dict[str, float],
    current_price: Optional[float] = None,
) -> Dict:
    """生成综合裁决"""
    
    # 加权计算
    weighted_sum = 0.0
    total_weight = 0.0
    detail_lines = []
    
    for role, score in scores.items():
        w = weights.get(role, 0.2)
        weighted_sum += score * w
        total_weight += w
        
        sign = "+" if score > 0 else ""
        label = {
            "technical": "技术面",
            "sentiment": "情绪面",
            "news": "新闻面",
            "fundamental": "基本面"
        }.get(role, role)
        
        detail_lines.append(
            f"{label} [{sign}{score}] (权重{w:.0%}): {summaries.get(role, '')[:80]}"
        )
    
    composite = weighted_sum / total_weight if total_weight > 0 else 0.0
    
    action, description, threshold = score_to_action(composite)
    
    # 仓位建议
    if action == "BUY":
        position_sizing = "可加仓 20-30% 现有仓位"
        price_target = f"{current_price * 1.05:.2f}" if current_price else "待定"
        stop_loss = f"{current_price * 0.97:.2f}" if current_price else "待定"
        rationale = f"综合评分 {composite:.2f}，{description}"
    elif action == "OVERWEIGHT":
        position_sizing = "小幅加仓 10%"
        price_target = f"{current_price * 1.03:.2f}" if current_price else "待定"
        stop_loss = f"{current_price * 0.975:.2f}" if current_price else "待定"
        rationale = f"综合评分 {composite:.2f}，{description}"
    elif action == "HOLD":
        position_sizing = "维持现有仓位不变"
        price_target = "维持"
        stop_loss = f"{current_price * 0.95:.2f}" if current_price else "待定"
        rationale = f"综合评分 {composite:.2f}，方向不明确，观望"
    elif action in ("UNDERWEIGHT", "SELL"):
        position_sizing = "减仓 50%" if action == "UNDERWEIGHT" else "清仓"
        price_target = "离场"
        stop_loss = "即时减仓"
        rationale = f"综合评分 {composite:.2f}，{description}"
    
    verdict = {
        "ticker": ticker,
        "name": name,
        "date": date,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "analyst_scores": scores,
        "weights_applied": {r: weights.get(r, 0.2) for r in scores},
        "composite_score": round(composite, 2),
        "action": action,
        "confidence": abs(composite) / 2.0 * 100,  # 0~100%
        "action_plan": {
            "action": action,
            "ticker": ticker,
            "current_price": current_price,
            "price_target": price_target,
            "stop_loss": stop_loss,
            "position_sizing": position_sizing,
            "rationale": rationale,
        },
        "detail_lines": detail_lines,
    }
    
    return verdict


def render_verdict(verdict: Dict) -> str:
    """渲染裁决为人类可读格式"""
    ap = verdict["action_plan"]
    detail = "\n".join(f"  {d}" for d in verdict["detail_lines"])
    
    action_emoji = {"BUY": "🚀", "OVERWEIGHT": "📈", "HOLD": "⏸️", 
                    "UNDERWEIGHT": "📉", "SELL": "🚨"}
    emoji = action_emoji.get(verdict["action"], "❓")
    
    lines = f"""
╔══════════════════════════════════════╗
║  TradingAgents 综合裁决报告           ║
╠══════════════════════════════════════╣
║  标的: {verdict['ticker']:<6} {verdict['name']:<20} ║
║  日期: {verdict['date']:<24} ║
╚══════════════════════════════════════╝

📊 各维度评分:
{detail}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚖️  综合评分: {verdict['composite_score']:+.2f}
📌 操作建议: {emoji} {verdict['action']} (置信度 {verdict['confidence']:.0f}%)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

▶ 操作逻辑: {ap['rationale']}
▶ 当前价: {ap['current_price']}元
▶ 目标价: {ap['price_target']}
▶ 止损位: {ap['stop_loss']}
▶ 仓位: {ap['position_sizing']}
"""
    return lines


def main():
    import argparse
    parser = argparse.ArgumentParser(description="TradingAgents 综合裁决")
    parser.add_argument("ticker", nargs="?", help="股票代码")
    parser.add_argument("date", nargs="?", help="分析日期 YYYY-MM-DD")
    parser.add_argument("--input", help="直接指定报告JSON路径")
    parser.add_argument("--weights", default="",
                      help="自定义权重，如 technical=0.3,news=0.4")
    parser.add_argument("--price", type=float, help="当前价格（可选）")
    parser.add_argument("--name", help="股票名称（可选）")
    
    args = parser.parse_args()
    
    # 加载报告
    if args.input:
        report_path = args.input
    elif args.ticker and args.date:
        report_path = find_report(args.ticker, args.date)
        if not report_path:
            print(f"❌ 未找到 {args.ticker} {args.date} 的分析师报告")
            print(f"   请先运行 parallel_analysts.py {args.ticker}")
            sys.exit(1)
    else:
        # 找最新的报告
        json_files = [f for f in os.listdir(REPORTS_DIR) if f.endswith(".json")]
        if not json_files:
            print("❌ 没有找到任何分析师报告")
            sys.exit(1)
        latest = max(json_files, key=lambda f: os.path.getmtime(
            os.path.join(REPORTS_DIR, f)))
        report_path = os.path.join(REPORTS_DIR, latest)
        print(f"📂 使用最新报告: {report_path}")
    
    report = load_report(report_path)
    ticker = report.get("ticker", args.ticker or "unknown")
    name = args.name or report.get("name", ticker)
    date = report.get("date", args.date or "unknown")
    
    # 解析自定义权重
    weights = DEFAULT_WEIGHTS.copy()
    if args.weights:
        for w in args.weights.split(","):
            if "=" in w:
                role, val = w.split("=")
                weights[role.strip()] = float(val.strip())
    
    # 提取各分析师评分
    scores = {}
    summaries = {}
    for r in report.get("reports", []):
        role = r.get("role", "")
        if role:
            score, summary = extract_score_from_report(r, role)
            scores[role] = score
            summaries[role] = summary
    
    # 如果报告中没有逐角色数据，尝试从summary整体解析
    if not scores:
        # 启发式提取
        text = report.get("summary", "")
        for role in ["technical", "sentiment", "news", "fundamental"]:
            label = {"technical":"技术", "sentiment":"情绪", 
                    "news":"新闻", "fundamental":"基本面"}[role]
            m = re.search(rf'{label}[^\d]*?([+-]?\d+\.?\d*)', text)
            if m:
                scores[role] = float(m.group(1))
                summaries[role] = f"从报告中提取 {label} 评分"
    
    if not scores:
        print("❌ 未能从报告中提取各维度评分")
        print("   请确认报告格式或手动输入评分")
        sys.exit(1)
    
    # 获取当前价格
    current_price = args.price
    if not current_price:
        from market_data import fetch_quote
        try:
            q = fetch_quote(ticker)
            current_price = q.get("price")
        except:
            pass
    
    # 生成裁决
    verdict = generate_verdict(ticker, name, date, scores, summaries, weights, current_price)
    
    # P1-5 修复: 四步门禁代码强制 — BUY/SELL 必须通过 DecisionGate
    action = verdict["action"]
    if action in ("BUY", "SELL", "OVERWEIGHT", "UNDERWEIGHT"):
        from decision_gate import DecisionGate
        gate = DecisionGate()
        gate_result = gate.check(
            ticker=ticker,
            direction=action,
            analyst_scores=scores,
            current_price=current_price or 0,
            shares=100,  # 默认，Gate 4 会重新计算
            weights=weights,
        )
        verdict["gate_result"] = gate_result
        verdict["gate_passed"] = gate_result["verdict"] == "APPROVE"
        
        if not verdict["gate_passed"]:
            verdict["action"] = "HOLD"  # 门禁未通过 → 降级为观望
            verdict["action_plan"]["action"] = "HOLD"
            verdict["action_plan"]["rationale"] = (
                f"门禁拒绝: {'; '.join(gate_result['reasons'])}"
            )
            verdict["action_plan"]["position_sizing"] = "门禁未通过，维持现有仓位"
    
    # 输出
    print(render_verdict(verdict))
    
    # 保存
    verdict_path = os.path.join(REPORTS_DIR, f"{ticker}_{date}_verdict.json")
    with open(verdict_path, "w", encoding="utf-8") as f:
        json.dump(verdict, f, ensure_ascii=False, indent=2)
    print(f"📁 裁决已保存: {verdict_path}")


if __name__ == "__main__":
    # 需要能导入market_data
    sys.path.insert(0, os.path.dirname(__file__))
    main()

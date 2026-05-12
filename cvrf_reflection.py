#!/config/quant_env/bin/python3
"""
cvrf_reflection.py — Conceptual Verbal Reinforcement 自动经验提炼

对标 FinCon 论文的 CVRF (Conceptual Verbal Reinforcement) 机制。
每次夜报前运行，自动比较两轮决策链，提炼"系统性投资信念"。

原理：对比最近两轮同类型 cron 报告的 summary + key_metrics，
发现重复出现的成功/失败模式，输出结构化分析供 cron agent 使用。

输出：stdout → 注入 cron 上下文，cron agent 据此写 stock_kb + 注册 signal
"""

import json
import sys
import os
from datetime import datetime, timedelta

# 加载路径
sys.path.insert(0, "/config/quant_scripts")
from trade_db import CronReport, DB_PATH
from stock_kb import StockKB

REPORT_TYPES_FOR_REFLECTION = ["close", "night"]  # 比较收盘和夜报


def load_guard_config():
    """P1-1 修复: 从 stock_kb DB 加载持仓（不再从guard_config.json）"""
    kb = StockKB()
    return kb.read_portfolio_truth()


def get_last_two_reports(report_type="night", days_back=5):
    """获取最近两轮指定类型报告"""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT date, job_name, summary, key_metrics, created_at
           FROM cron_reports
           WHERE report_type=?
           ORDER BY id DESC LIMIT 2""",
        [report_type]
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def compare_and_reflect(reports, positions):
    """对比两轮报告，输出结构化 CVRF 上下文"""
    if len(reports) < 2:
        return {"status": "need_more_data", "message": "需要至少2轮报告才能提炼"}

    r1, r2 = reports[1], reports[0]  # r1=旧, r2=新

    metrics1 = json.loads(r1.get("key_metrics", "{}"))
    metrics2 = json.loads(r2.get("key_metrics", "{}"))

    # 提取关键指标变化
    changes = {}
    all_keys = set(list(metrics1.keys()) + list(metrics2.keys()))
    for k in sorted(all_keys):
        v1 = metrics1.get(k)
        v2 = metrics2.get(k)
        if v1 is not None and v2 is not None and v1 != v2:
            try:
                delta = float(v2) - float(v1)
                pct = delta / abs(float(v1)) * 100 if float(v1) != 0 else 0
                changes[k] = {
                    "before": v1, "after": v2,
                    "delta": round(delta, 2),
                    "delta_pct": round(pct, 1)
                }
            except (ValueError, TypeError):
                changes[k] = {"before": v1, "after": v2}

    # 当前持仓
    pos_list = []
    positions_data = positions.get("positions", {})
    for code, info in positions_data.items():
        pos_list.append(f"{info['name']}({code}): {info['shares']}股, 成本{info['cost']}")

    # 输出结构化上下文
    output = {
        "status": "ready",
        "cvrf_analysis": {
            "before": {
                "date": r1["date"],
                "summary": r1["summary"][:200],
            },
            "after": {
                "date": r2["date"],
                "summary": r2["summary"][:200],
            },
            "metrics_changes": changes,
            "positions": pos_list,
        }
    }

    return output


def main():
    config = load_guard_config()
    positions = config.get("positions", {})

    print("=" * 60)
    print("  CVRF 自动经验提炼 — 概念化语言强化")
    print(f"  运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    # 分析最近两轮夜报
    reports = get_last_two_reports("night")
    result = compare_and_reflect(reports, config)
    if result["status"] == "need_more_data":
        # 退一步看收盘总结
        reports = get_last_two_reports("close")
        result = compare_and_reflect(reports, config)

    if result["status"] != "ready":
        print(f"\n⚠️  {result['message']}")
        print("\n建议：明日关注持仓变化是否形成可记录的规律。")
        return

    analysis = result["cvrf_analysis"]

    print(f"\n📊 决策链对比")
    print(f"  ┌─ 上轮: {analysis['before']['date']} → {analysis['before']['summary'][:80]}...")
    print(f"  └─ 本轮: {analysis['after']['date']} → {analysis['after']['summary'][:80]}...")

    if analysis["metrics_changes"]:
        print(f"\n📈 关键指标变化 ({len(analysis['metrics_changes'])}项)")
        for k, v in sorted(analysis["metrics_changes"].items()):
            print(f"  {k}: {v['before']} → {v['after']} ({v.get('delta_pct', '?')}%)")

    print(f"\n📋 当前持仓 ({len(analysis['positions'])}标的)")
    for p in analysis["positions"]:
        print(f"  {p}")

    print(f"\n🔍 请分析以上数据，提炼系统性投资信念：")
    print(f"  1) 对比两轮决策：什么做对了？什么做错了？")
    print(f"  2) 是否存在重复出现的模式？===>>> 注册为 signal")
    print(f"  3) 将关键洞察写入 stock_kb.add_insight()")
    print()
    print("=" * 60)


if __name__ == "__main__":
    main()

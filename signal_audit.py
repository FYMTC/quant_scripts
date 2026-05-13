#!/usr/bin/env python3
"""
signal_audit.py — 信号审计系统

两个入口：
  audit_daily()     → 夜间审计（21:00 cron调用）
  evaluate_quality() → 盘后评估昨日信号质量

审计内容：
  1. 统计今日信号活动（触发/过滤/分析/决策）
  2. 评估BUY/SELL信号质量（与次日走势对比）
  3. 调优每标的阈值
  4. 发现新模式 → 写入stock_signal_profile
  5. 生成审计报告 → 注入21:00 cron上下文
"""

import json
import os
import subprocess
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional

BASE = os.path.dirname(os.path.abspath(__file__))
AUDIT_LOG_PATH = os.path.join(BASE, "signal_audit.jsonl")
PROFILE_DIR = os.path.join(BASE, "signal_profiles")


def audit_daily(rollback_date: str = None) -> dict:
    """
    每日审计：汇总今日信号活动 + 调优阈值 + 发现模式。
    rollback_date: 回溯审计日期（默认今天），用于修正昨日decision_quality。
    """
    if rollback_date is None:
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
    else:
        today = rollback_date
        yesterday = (datetime.strptime(rollback_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

    # 1. 收集今日日志
    today_entries = _read_logs(today)
    yesterday_entries = _read_logs(yesterday)

    if not today_entries:
        return {
            "date": today,
            "status": "empty",
            "message": "今日无信号活动记录",
            "summary": {}
        }

    # 2. 分类统计
    stats = _categorize_entries(today_entries)

    # 3. 评估昨日信号质量（用今日实际走势回测）
    quality_updates = _evaluate_quality(yesterday_entries, today)

    # 4. 按标的分组统计
    per_stock = _per_stock_stats(today_entries)

    # 5. 调优阈值（基于假阳性率）
    tuning_results = {}
    for code in per_stock:
        try:
            result = subprocess.run(
                ["/config/quant_env/bin/python",
                 os.path.join(BASE, "stock_signal_profile.py"),
                 "tune", code],
                capture_output=True, text=True, timeout=10,
                cwd=BASE
            )
            if result.returncode == 0 and result.stdout.strip():
                tuning_results[code] = json.loads(result.stdout.strip())
        except Exception:
            pass

    # 6. 发现新模式（跨标的共性）
    patterns = _discover_patterns(per_stock)

    # 7. 生成报告
    report = _generate_report(today, stats, per_stock, tuning_results,
                               quality_updates, patterns)

    return {
        "date": today,
        "status": "ok",
        "entries_count": len(today_entries),
        "summary": stats,
        "per_stock": per_stock,
        "tuning": tuning_results,
        "quality_updates": quality_updates,
        "patterns": patterns,
        "report": report,
    }


def _read_logs(target_date: str) -> List[dict]:
    """读取指定日期的审计日志"""
    entries = []
    if not os.path.exists(AUDIT_LOG_PATH):
        return entries
    try:
        with open(AUDIT_LOG_PATH) as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if entry.get("date") == target_date:
                        entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return entries


def _categorize_entries(entries: List[dict]) -> dict:
    """分类统计"""
    stats = {
        "total": len(entries),
        "trigger": 0,
        "filter_reject": 0,
        "filter_pass": 0,
        "analyze": 0,
        "decision": 0,
        "decision_buy": 0,
        "decision_sell": 0,
        "decision_wait": 0,
        "decision_dismiss": 0,
        "stocks_involved": set(),
    }
    for e in entries:
        at = e.get("action_type", "")
        if at == "TRIGGER":
            stats["trigger"] += 1
        elif at == "FILTER_REJECT":
            stats["filter_reject"] += 1
        elif at == "FILTER_PASS":
            stats["filter_pass"] += 1
        elif at == "ANALYZE":
            stats["analyze"] += 1
        elif at == "DECISION":
            stats["decision"] += 1
            d = e.get("decision", "")
            if d == "BUY":
                stats["decision_buy"] += 1
            elif d == "SELL":
                stats["decision_sell"] += 1
            elif d == "WAIT":
                stats["decision_wait"] += 1
            elif d == "DISMISS":
                stats["decision_dismiss"] += 1
        stats["stocks_involved"].add(e.get("stock_code", ""))

    stats["stocks_involved"] = sorted(list(stats["stocks_involved"]))
    return stats


def _per_stock_stats(entries: List[dict]) -> dict:
    """按标的统计"""
    per = {}
    for e in entries:
        code = e.get("stock_code", "unknown")
        if code not in per:
            per[code] = {"trigger": 0, "analyze": 0, "decision": 0, "decisions": []}
        at = e.get("action_type", "")
        if at == "TRIGGER":
            per[code]["trigger"] += 1
        elif at == "ANALYZE":
            per[code]["analyze"] += 1
        elif at == "DECISION":
            per[code]["decision"] += 1
            per[code]["decisions"].append(e.get("decision", ""))
    return per


def _evaluate_quality(yesterday_entries: List[dict], today: str) -> dict:
    """
    回溯评估昨日决策质量。
    以今天的实际走势判断昨天的BUY/SELL是对是错。
    """
    updates = {}
    yesterday_decisions = [e for e in yesterday_entries
                           if e.get("action_type") == "DECISION"
                           and e.get("decision") in ("BUY", "SELL")]

    for e in yesterday_decisions:
        code = e.get("stock_code", "")
        decision = e.get("decision", "")
        # 获取今日涨跌
        today_pct = _get_today_change(code)
        if today_pct is None:
            continue

        # 评估
        is_good = False
        if decision == "BUY" and today_pct > 0:
            is_good = True  # 建议买入后涨了=好
        elif decision == "SELL" and today_pct < 0:
            is_good = True  # 建议卖出后跌了=好

        quality = "good" if is_good else "bad"
        updates[code] = {
            "decision": decision,
            "today_pct": today_pct,
            "quality": quality,
            "yesterday_signal_id": e.get("signal_id", ""),
        }

        # 记录触发质量
        try:
            subprocess.run(
                ["/config/quant_env/bin/python",
                 os.path.join(BASE, "stock_signal_profile.py"),
                 "record", code, "tp" if is_good else "fp"],
                capture_output=True, timeout=5,
                cwd=BASE
            )
        except Exception:
            pass

    return updates


def _get_today_change(code: str) -> Optional[float]:
    """获取今日涨跌幅（腾讯API）"""
    try:
        import urllib.request
        url = f"https://qt.gtimg.cn/q={_add_prefix(code)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("gbk", errors="replace")
        parts = raw.split("~")
        if len(parts) > 32:
            return float(parts[32])
    except Exception:
        pass
    return None


def _add_prefix(code: str) -> str:
    if code.startswith(("0", "3")):
        return f"sz{code}"
    return f"sh{code}"


def _discover_patterns(per_stock: dict) -> List[str]:
    """跨标的模式发现"""
    patterns = []

    # 模式1：同一天多只标的触发同一类型信号 → 板块效应
    # （简化版：如果>3只标的同时触发，标记板块共振）
    if len(per_stock) >= 3:
        patterns.append(f"今日{len(per_stock)}只标的触发信号，检查是否存在板块共振")

    # 模式2：某标的连续触发但从未执行 → 阈值可能太敏感
    for code, stats in per_stock.items():
        if stats["trigger"] >= 3 and stats["decision"] == 0:
            patterns.append(f"{code}: 触发{stats['trigger']}次但零决策，阈值可能过敏感")

    return patterns


def _generate_report(today: str, stats: dict, per_stock: dict,
                     tuning: dict, quality: dict, patterns: List[str]) -> str:
    """生成审计报告文本"""
    lines = [
        f"## 📊 信号审计报告 | {today}",
        "",
        "### 今日概览",
        f"  信号活动: {stats['total']}条记录",
        f"  触发: {stats['trigger']}次 | 过滤拒绝: {stats['filter_reject']}次 | 通过: {stats['filter_pass']}次",
        f"  全量分析: {stats['analyze']}次",
        f"  决策: {stats['decision']}次 (BUY {stats['decision_buy']} | SELL {stats['decision_sell']} | WAIT {stats['decision_wait']} | DISMISS {stats['decision_dismiss']})",
        f"  涉及标的: {', '.join(stats['stocks_involved'])}",
        "",
        "### 各标的明细",
    ]

    for code, s in per_stock.items():
        decisions_str = ", ".join(s["decisions"]) if s["decisions"] else "无"
        tune_info = ""
        if code in tuning:
            t = tuning[code]
            if t.get("status") == "tuned":
                changes = ", ".join(f"{k}:{v}" for k, v in t.get("changes", {}).items())
                tune_info = f" ⚙️已调参: {changes}"
            elif t.get("status") == "maintain":
                tune_info = " ✓阈值保持"

        lines.append(f"  **{code}**: 触发{s['trigger']}次 分析{s['analyze']}次 决策:{decisions_str}{tune_info}")

    if quality:
        lines.append("")
        lines.append("### 昨日决策回溯")
        for code, q in quality.items():
            icon = "✅" if q["quality"] == "good" else "❌"
            lines.append(f"  {icon} {code} {q['decision']}: 今日{q['today_pct']:+.2f}% → {q['quality']}")

    if patterns:
        lines.append("")
        lines.append("### 发现模式")
        for p in patterns:
            lines.append(f"  • {p}")

    return "\n".join(lines)


# === CLI ===

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  signal_audit.py daily [YYYY-MM-DD]  → 每日审计")
        print("  signal_audit.py report [YYYY-MM-DD]  → 输出报告")
        sys.exit(0)

    cmd = sys.argv[1]
    rollback = sys.argv[2] if len(sys.argv) > 2 else None

    if cmd == "daily":
        result = audit_daily(rollback)
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))

    elif cmd == "report":
        result = audit_daily(rollback)
        print(result.get("report", "无数据"))

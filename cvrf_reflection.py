#!python3
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
import argparse
from datetime import datetime, timedelta

# T1.10 三期（2026-07-04）：修复 cfg 导入顺序 bug
# 原 line 20 `sys.path.insert(0, cfg.root)` 在 `from system_config import cfg` 之前 → NameError 自 2026-05-08 起
import system_config as _sc  # 先导入拿到 cfg.root
sys.path.insert(0, _sc.cfg.root)
from trade_db import CronReport, DB_PATH, TradeDB
from stock_kb import StockKB
from system_config import cfg

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
    print(f"  3) 将关键洞察写入 stock_kb.add_insight() — DS-3: 使用 confidence='pending'")
    print(f"     → 后续用 cvrf_approve.py approve --all 批量确认")
    print()
    print("=" * 60)


# ========== T1.10 三期：周度经验 GC（§7.4） ==========

WIN_RATE_TIGHTEN_THRESHOLD = 0.60   # 胜率 > 60% → 收紧阈值
WIN_RATE_REMOVE_THRESHOLD = 0.40    # 胜率 < 40% → 标记移除
MIN_CLUSTER_SIZE = 5                # 聚类最小样本数（不足跳过）
POST_EVENT_EVAL_DAYS = 5            # 事件后评估天数


def _fetch_decision_events(weeks_back: int) -> list:
    """查 trading_journal 近 weeks_back 周的决策事件（type='决策事件'）。"""
    import sqlite3
    cutoff = (datetime.now() - timedelta(weeks=weeks_back)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT date, code, name, action, event_id, signal_id, request_id,
                  resolver_path, decision_gate_json, rationale
           FROM trading_journal
           WHERE type='决策事件' AND date >= ?
           ORDER BY date DESC""",
        [cutoff]
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _normalize_rationale(rationale: str) -> str:
    """从 rationale 抽取模式关键词（去掉所有数值，便于聚类）。
    
    rationale 示例: "急跌-3.23%+cp=0.32+close>MA20+股吧情绪偏空=抄底机会"
    → "急跌|cp|close>MA|股吧情绪偏空"（去掉数值，保留结构化关键词）
    
    "急跌-3.23%" 与 "急跌-2.5%" 归为同模式 "急跌"。
    """
    if not rationale:
        return ""
    import re
    # 先按 +/= 分割
    parts = re.split(r'[+=]', rationale)
    keywords = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # 保留含中文或字母（非纯数字）的关键词
        if not re.search(r'[\u4e00-\u9fa5a-zA-Z]', p):
            continue
        # 去掉所有数值（含小数/百分比/负号）：-3.23% / 0.32 / 20
        kw = re.sub(r'[\-+]?\d+\.?\d*%?', '', p)
        # 去掉尾部残留的 = 或 -（但保留 < > 表示方向）
        kw = re.sub(r'[=-]+\s*$', '', kw).strip()
        if kw:
            keywords.append(kw)
    return "|".join(keywords[:4])  # 最多 4 个关键词


def _cluster_by_rationale(events: list) -> dict:
    """按 rationale 关键词聚类决策事件。"""
    clusters = {}
    for ev in events:
        pattern = _normalize_rationale(ev.get("rationale", ""))
        if not pattern:
            pattern = "_uncategorized_"
        clusters.setdefault(pattern, []).append(ev)
    return clusters


def _fetch_post_event_price(code: str, event_date: str, days: int = POST_EVENT_EVAL_DAYS) -> float:
    """baostock 取事件后 N 日的价格变动百分比。
    
    返回 (close_event_date, close_after_n_days, pct_change)。
    失败返回 None。
    """
    try:
        import baostock as bs
        import io, contextlib
        market = 'sz' if code.startswith('00') or code.startswith('30') else 'sh'
        # 事件日 + 10 自然日覆盖 N 个交易日的窗口
        start = datetime.strptime(event_date, "%Y-%m-%d")
        end = start + timedelta(days=days + 10)
        with contextlib.redirect_stdout(io.StringIO()):
            bs.login()
            rs = bs.query_history_k_data_plus(
                f'{market}.{code}',
                'date,close',
                start_date=event_date,
                end_date=end.strftime('%Y-%m-%d'),
                frequency='d', adjustflag='2'
            )
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            bs.logout()
        if len(rows) < 2:
            return None
        closes = [float(r[1]) for r in rows if r[1]]
        if len(closes) < 2:
            return None
        entry_close = closes[0]
        # 取第 min(days, len-1) 个交易日的收盘
        exit_idx = min(days, len(closes) - 1)
        exit_close = closes[exit_idx]
        if entry_close <= 0:
            return None
        return (entry_close, exit_close, (exit_close / entry_close - 1) * 100)
    except Exception:
        return None


def _evaluate_event_outcome(event: dict) -> str:
    """评估单个决策事件的方向正确性。
    
    返回 "win" / "loss" / "neutral"。
    
    评估优先级：
    1. stock_trades 同 code 同日 ±1d 有成交 + pnl != None → win if pnl > 0
    2. 无成交 / pnl=None → baostock 事件后 5 日价格变动判方向
       - BUY: 价格上涨 → win
       - SELL: 价格下跌 → win（卖在高位）
       - HOLD/WAIT/T_FLIP: neutral（无法客观评估）
    """
    import sqlite3
    code = event.get("code", "")
    event_date = event.get("date", "")
    action = (event.get("action") or "").upper()
    
    if not code or not event_date:
        return "neutral"
    
    # HOLD/WAIT/T_FLIP 无法客观评估
    if action in ("HOLD", "WAIT", "T_FLIP", ""):
        return "neutral"
    
    # 1. 查 stock_trades
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        # 事件日 ±1 天
        d = datetime.strptime(event_date, "%Y-%m-%d")
        rows = conn.execute(
            """SELECT action, pnl, pnl_pct FROM stock_trades
               WHERE stock_code=? AND trade_date BETWEEN ? AND ?
               ORDER BY trade_date DESC LIMIT 1""",
            [code, (d - timedelta(days=1)).strftime("%Y-%m-%d"),
             (d + timedelta(days=1)).strftime("%Y-%m-%d")]
        ).fetchall()
        conn.close()
        if rows:
            r = rows[0]
            pnl = r["pnl"]
            if pnl is not None:
                return "win" if float(pnl) > 0 else "loss"
            # pnl=None（开仓未平）→ 走 baostock 兜底
    except Exception:
        pass
    
    # 2. baostock 兜底
    price_info = _fetch_post_event_price(code, event_date, POST_EVENT_EVAL_DAYS)
    if not price_info:
        return "neutral"
    _, _, pct = price_info
    if action == "BUY":
        return "win" if pct > 0 else "loss"
    elif action == "SELL":
        return "win" if pct < 0 else "loss"
    return "neutral"


def run_weekly_gc(weeks_back: int = 4) -> dict:
    """T1.10 三期（§7.4）：周度经验 GC。
    
    1. 查 trading_journal 近 weeks_back 周决策事件（type='决策事件'）
    2. 按 rationale 关键词聚类
    3. 对每个聚类（≥ MIN_CLUSTER_SIZE 条）：
       - 评估每个事件 win/loss/neutral
       - 胜率 = win / (win + loss)
       - 胜率 > 60% → tune_thresholds + add_pattern("GC_TIGHTEN")
       - 胜率 < 40% → add_pattern("GC_REMOVE")
       - 40%-60% → 不动
    4. 输出 GC 报告 → cfg.path.cvrf_weekly_gc
    """
    events = _fetch_decision_events(weeks_back)
    
    result = {
        "run_at": datetime.now().isoformat(),
        "weeks_back": weeks_back,
        "total_events": len(events),
        "clusters": [],
        "profile_mutations": 0,
        "status": "ok"
    }
    
    if not events:
        result["status"] = "insufficient_data"
        result["message"] = f"近 {weeks_back} 周无 type='决策事件' 记录，GC 跳过"
        _save_gc_report(result)
        return result
    
    clusters = _cluster_by_rationale(events)
    
    try:
        from stock_signal_profile import tune_thresholds, add_pattern
        _profile_available = True
    except Exception:
        _profile_available = False
    
    for pattern, evs in clusters.items():
        if len(evs) < MIN_CLUSTER_SIZE:
            # 样本不足，跳过但不报错
            result["clusters"].append({
                "pattern": pattern,
                "count": len(evs),
                "win_rate": None,
                "action": "skip",
                "note": f"样本不足(<{MIN_CLUSTER_SIZE})，跳过"
            })
            continue
        
        # 评估每个事件
        win, loss, neutral = 0, 0, 0
        codes_affected = set()
        for ev in evs:
            outcome = _evaluate_event_outcome(ev)
            if outcome == "win":
                win += 1
            elif outcome == "loss":
                loss += 1
            else:
                neutral += 1
            if ev.get("code"):
                codes_affected.add(ev["code"])
        
        decisive = win + loss
        win_rate = (win / decisive) if decisive > 0 else None
        
        cluster_result = {
            "pattern": pattern,
            "count": len(evs),
            "win": win, "loss": loss, "neutral": neutral,
            "win_rate": round(win_rate, 2) if win_rate is not None else None,
            "codes_affected": sorted(codes_affected),
            "action": "maintain",
            "threshold_changes": {}
        }
        
        if win_rate is None:
            cluster_result["action"] = "skip"
            cluster_result["note"] = "无 decisive 事件（全 neutral）"
        elif win_rate >= WIN_RATE_TIGHTEN_THRESHOLD:
            # 胜率高 → 收紧阈值
            cluster_result["action"] = "tighten"
            if _profile_available:
                for code in sorted(codes_affected):
                    try:
                        tune_result = tune_thresholds(code)
                        if tune_result.get("changes"):
                            cluster_result["threshold_changes"][code] = tune_result["changes"]
                        add_pattern(code, f"GC_TIGHTEN: {pattern} win_rate={win_rate:.0%}")
                        result["profile_mutations"] += 1
                    except Exception:
                        pass
        elif win_rate < WIN_RATE_REMOVE_THRESHOLD:
            # 胜率低 → 标记移除
            cluster_result["action"] = "remove"
            cluster_result["note"] = f"胜率 {win_rate:.0%} < {WIN_RATE_REMOVE_THRESHOLD:.0%}，模式标记移除"
            if _profile_available:
                for code in sorted(codes_affected):
                    try:
                        add_pattern(code, f"GC_REMOVE: {pattern} win_rate={win_rate:.0%}")
                        result["profile_mutations"] += 1
                    except Exception:
                        pass
        else:
            cluster_result["action"] = "maintain"
            cluster_result["note"] = f"胜率 {win_rate:.0%} 在 {WIN_RATE_REMOVE_THRESHOLD:.0%}-{WIN_RATE_TIGHTEN_THRESHOLD:.0%} 之间，保持"
        
        result["clusters"].append(cluster_result)
    
    _save_gc_report(result)
    return result


def _save_gc_report(result: dict) -> None:
    """保存 GC 报告到 cfg.path.cvrf_weekly_gc（原子写）。"""
    import tempfile
    out_path = cfg.path.cvrf_weekly_gc
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    # 原子写：tmp + os.replace
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(out_path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, out_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="CVRF 自动经验提炼")
    p.add_argument("--mode", choices=["nightly", "weekly-gc"], default="nightly",
                   help="nightly=夜报反省（默认）; weekly-gc=周度经验 GC")
    p.add_argument("--weeks-back", type=int, default=4,
                   help="weekly-gc 模式：回溯周数（默认 4）")
    args = p.parse_args()
    
    if args.mode == "weekly-gc":
        result = run_weekly_gc(args.weeks_back)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        main()

#!/usr/bin/env python3
"""
backtest_rotation.py — T1.10 三期 轮动信号回测

§8.3 12 周回测窗口，滚动 4 周命中率 < 50% → 暂停轮动布局
§10.1 通过标准：滚动 4 周命中率 ≥ 50%

设计（见 .trae/documents/T1.10-phase3-completion.md Task 4）：
  - recompute_weekly_rotation(week_start)：复用 rotation_scanner.compute_industry_metrics
    （历史锚定 end_date=week_start）+ rank_industries，重算该周的 TOP3 up/down 行业
  - evaluate_week_hit(week_start, signal)：取 TOP3 up/down 成分股在 week_start 后
    5 交易日的平均收益，hit = up_avg > down_avg 且 up_avg > 0
  - run_backtest(weeks=12)：循环过去 N 个周六，算滚动 4 周命中率

输出：cfg.path.backtest_rotation JSON
上游：rotation_scanner._run_backtest_gate() subprocess 调用拿门槛结果
"""

import sys
import os
import json
import io
import contextlib
import argparse
import tempfile
from datetime import datetime, timedelta
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from system_config import cfg

DEFAULT_WEEKS = 12
HIT_THRESHOLD = 0.50
MIN_WEEKS_FOR_VERDICT = 8      # < 8 周不下定论
EVAL_FORWARD_DAYS = 5          # 事件后评估交易日


def recompute_weekly_rotation(week_start: str) -> Dict:
    """对给定周起始日重算轮动信号（TOP3 up / TOP3 down）。

    universe 用当前持仓+watchlist+screener_top15（历史不变的简化近似）。
    数据用 week_start 之前 20 日 K 线（历史锚定）。
    """
    from rotation_scanner import fetch_universe_codes, compute_industry_metrics, rank_industries

    codes = fetch_universe_codes()
    if len(codes) < 5:
        return {
            "week": week_start, "status": "insufficient_universe",
            "top3_up": [], "top3_down": [], "universe_size": len(codes),
        }

    metrics = compute_industry_metrics(codes, lookback_days=20, end_date=week_start)
    ranking = rank_industries(metrics)
    return {
        "week": week_start,
        "status": "ok",
        "universe_size": len(codes),
        "top3_up": ranking["top3_up"],
        "top3_down": ranking["top3_down"],
    }


def _fetch_forward_returns(code: str, start_date: str, days: int = EVAL_FORWARD_DAYS) -> Optional[tuple]:
    """baostock 取 start_date 后 N 个交易日的收益。

    返回 (close_start, close_after_n, pct_change) 或 None。
    """
    try:
        import baostock as bs
        market = 'sz' if code.startswith('00') or code.startswith('30') or code.startswith('15') else 'sh'
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end = (start_dt + timedelta(days=days + 10)).strftime('%Y-%m-%d')
        with contextlib.redirect_stdout(io.StringIO()):
            bs.login()
            rs = bs.query_history_k_data_plus(
                f'{market}.{code}', 'date,close',
                start_date=start_date, end_date=end,
                frequency='d', adjustflag='2'
            )
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            bs.logout()
        closes = [float(r[1]) for r in rows if r and len(r) > 1 and r[1]]
        if len(closes) < 2:
            return None
        entry = closes[0]
        exit_idx = min(days, len(closes) - 1)
        exit_close = closes[exit_idx]
        if entry <= 0:
            return None
        return (entry, exit_close, (exit_close / entry - 1) * 100)
    except Exception:
        return None


def _industry_avg_forward_return(industry_entry: Dict, week_start: str) -> Optional[float]:
    """算某行业 top_stocks 在 week_start 后 5 交易日的平均收益（%）。"""
    stocks = industry_entry.get("top_stocks") or []
    if not stocks:
        return None
    rets = []
    for s in stocks:
        code = s.get("code")
        if not code:
            continue
        r = _fetch_forward_returns(code, week_start, EVAL_FORWARD_DAYS)
        if r:
            rets.append(r[2])
    if not rets:
        return None
    return sum(rets) / len(rets)


def evaluate_week_hit(week_start: str, rotation_signal: Dict) -> Dict:
    """评估该周轮动信号命中。

    hit = (TOP3 up 平均收益 > TOP3 down 平均收益) 且 (up 平均收益 > 0)
    无数据 → hit=False, status=insufficient
    """
    if rotation_signal.get("status") != "ok":
        return {"week": week_start, "hit": False, "status": "insufficient",
                "top3_up_ret": None, "top3_down_ret": None}

    up_rets = []
    for ind in rotation_signal.get("top3_up", []):
        r = _industry_avg_forward_return(ind, week_start)
        if r is not None:
            up_rets.append(r)
    down_rets = []
    for ind in rotation_signal.get("top3_down", []):
        r = _industry_avg_forward_return(ind, week_start)
        if r is not None:
            down_rets.append(r)

    up_avg = round(sum(up_rets) / len(up_rets), 2) if up_rets else None
    down_avg = round(sum(down_rets) / len(down_rets), 2) if down_rets else None

    if not up_rets or not down_rets:
        return {"week": week_start, "hit": False, "status": "insufficient",
                "top3_up_ret": up_avg, "top3_down_ret": down_avg}

    hit = (up_avg > down_avg) and (up_avg > 0)
    return {
        "week": week_start, "hit": hit, "status": "ok",
        "top3_up_ret": up_avg, "top3_down_ret": down_avg,
    }


def _iter_saturday_starts(weeks: int) -> List[str]:
    """返回过去 weeks 个周六日期（含本周），从近到远。"""
    today = datetime.now().date()
    days_to_sat = (5 - today.weekday()) % 7  # 周一=0 ... 周六=5
    this_sat = today + timedelta(days=days_to_sat)
    return [(this_sat - timedelta(weeks=i)).strftime("%Y-%m-%d") for i in range(weeks)]


def run_backtest(weeks: int = DEFAULT_WEEKS) -> Dict:
    """主入口：循环过去 weeks 个周六，算滚动 4 周命中率。"""
    result = {
        "run_at": datetime.now().isoformat(),
        "weeks_back": weeks,
        "weekly_hits": [],
        "rolling_4wk_hit_rate": None,
        "passed": None,
        "threshold": HIT_THRESHOLD,
        "status": "ok",
    }

    saturdays = _iter_saturday_starts(weeks)
    # 从远到近跑（列表末尾 = 最近一周，便于滚动 4 周取末尾）
    for week_start in reversed(saturdays):
        signal = recompute_weekly_rotation(week_start)
        eva = evaluate_week_hit(week_start, signal)
        result["weekly_hits"].append({
            "week": week_start,
            "hit": eva["hit"],
            "top3_up_ret": eva.get("top3_up_ret"),
            "top3_down_ret": eva.get("top3_down_ret"),
            "status": eva.get("status"),
        })

    hits = [h["hit"] for h in result["weekly_hits"]]
    last_4 = hits[-4:] if len(hits) >= 4 else hits
    if last_4:
        result["rolling_4wk_hit_rate"] = round(sum(1 for h in last_4 if h) / len(last_4), 4)

    if weeks < MIN_WEEKS_FOR_VERDICT:
        result["status"] = "insufficient_data"
        result["passed"] = None
        result["reason"] = f"仅 {weeks} 周 (<{MIN_WEEKS_FOR_VERDICT})，不足下定论"
    else:
        rate = result["rolling_4wk_hit_rate"]
        result["passed"] = bool(rate is not None and rate >= HIT_THRESHOLD)

    _save_backtest_result(result)
    return result


def _save_backtest_result(result: Dict) -> None:
    """原子写入 cfg.path.backtest_rotation。"""
    out_path = cfg.path.backtest_rotation
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
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


def render_report(result: Dict) -> str:
    lines = [
        "=" * 55,
        "  轮动信号回测 (T1.10 三期)",
        f"  运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"  回测周数: {result.get('weeks_back')}",
        f"  状态: {result.get('status')}",
        "=" * 55,
    ]
    for h in result.get("weekly_hits", []):
        hit_mark = "✓" if h.get("hit") else "✗"
        lines.append(
            f"  {h['week']} {hit_mark} "
            f"up={h.get('top3_up_ret')} down={h.get('top3_down_ret')} [{h.get('status')}]"
        )
    if result.get("rolling_4wk_hit_rate") is not None:
        lines.append(f"\n滚动 4 周命中率: {result['rolling_4wk_hit_rate']:.1%}")
        verdict = "通过" if result.get("passed") else "未通过"
        lines.append(f"门槛 (≥{result.get('threshold', 0.5):.0%}): {verdict}")
    if result.get("reason"):
        lines.append(f"⚠️  {result['reason']}")
    return "\n".join(lines)


def cli():
    p = argparse.ArgumentParser(description="轮动信号回测")
    p.add_argument("--json", action="store_true", help="输出 JSON（默认人类可读）")
    p.add_argument("--weeks", type=int, default=DEFAULT_WEEKS,
                   help=f"回测周数（默认 {DEFAULT_WEEKS}）")
    args = p.parse_args()
    result = run_backtest(weeks=args.weeks)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_report(result))


if __name__ == "__main__":
    cli()

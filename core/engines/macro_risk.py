#!python3
"""macro_risk.py — 将 event_calendar + 减仓计划注入 apps JSON 输出。"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from core.engines.event_calendar import assess_event_risk, merge_recommendation, update_cron_state
from core.engines.portfolio_de_risk import build_de_risk_plan
from core.engines import signal_lineage as lineage


def _enrich_holdings_open_date(holdings: List[dict]) -> List[dict]:
    """给 holdings 补充 open_date（最近 BUY 日期），用于新仓保护期判断。

    数据来源优先级：stock_trades.trade_date > stock_kb.first_tracked_at。
    查询失败静默跳过（不阻塞减仓逻辑）。
    """
    if not holdings:
        return holdings
    try:
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "trade_log.db")
        if not os.path.isfile(db_path):
            return holdings
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # 1. 查 stock_trades 最近 BUY 记录
        trade_dates = {}
        for r in conn.execute(
            "SELECT stock_code, MAX(trade_date) as last_buy FROM stock_trades "
            "WHERE action='BUY' GROUP BY stock_code"
        ):
            trade_dates[r["stock_code"]] = r["last_buy"]
        # 2. 查 stock_kb.first_tracked_at 兜底
        kb_dates = {}
        for r in conn.execute("SELECT code, first_tracked_at FROM stock_kb"):
            kb_dates[r["code"]] = r["first_tracked_at"]
        conn.close()
        for h in holdings:
            code = h.get("code")
            h["open_date"] = trade_dates.get(code) or kb_dates.get(code)
        return holdings
    except Exception:
        return holdings


def assess_and_enrich(
    bundle: Dict[str, Any],
    *,
    slot: str = "intraday",
    scan_news: bool = True,
    holdings_key: str = "holdings",
) -> Dict[str, Any]:
    """在 apps 输出 dict 上附加 event_risk / de_risk_plan，并收紧 recommendation。"""
    ev = assess_event_risk(scan_news=scan_news)
    update_cron_state(ev, slot=slot)

    lid = lineage.append(
        "MACRO_ASSESS",
        slot,
        payload={
            "summary": f"event_level={ev.get('event_level')} hits={len(ev.get('keyword_hits') or [])}",
            "event_level": ev.get("event_level"),
            "keyword_hits": (ev.get("keyword_hits") or [])[:8],
        },
    )

    holdings: List[dict] = bundle.get(holdings_key) or []
    holdings = _enrich_holdings_open_date(holdings)
    total_assets = float(bundle.get("total_assets") or 0)
    if total_assets <= 0 and holdings:
        cash = float(bundle.get("cash") or 0)
        mval = sum(float(h.get("price") or 0) * int(h.get("shares") or 0) for h in holdings)
        total_assets = mval + cash

    playbook = dict(ev.get("playbook") or {})
    playbook["level"] = ev.get("event_level")
    de_risk = build_de_risk_plan(holdings, total_assets, playbook, lineage_id=lid)
    if de_risk.get("actions"):
        lineage.append(
            "DE_RISK_PLAN",
            slot,
            payload={"summary": de_risk.get("message"), "actions": de_risk.get("actions")},
            lineage_id=lid,
        )

    base_rec = bundle.get("recommendation") or "READY"
    bundle["event_risk"] = ev
    bundle["event_lineage_id"] = lid
    bundle["de_risk_plan"] = de_risk
    bundle["recommendation"] = merge_recommendation(base_rec, ev)
    bundle["macro_block_new_buy"] = not bool(playbook.get("allow_new_buy", True))
    return bundle

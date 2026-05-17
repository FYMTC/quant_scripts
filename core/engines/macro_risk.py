#!/config/quant_env/bin/python3
"""macro_risk.py — 将 event_calendar + 减仓计划注入 apps JSON 输出。"""

from __future__ import annotations

from typing import Any, Dict, List

from core.engines.event_calendar import assess_event_risk, merge_recommendation, update_cron_state
from core.engines.portfolio_de_risk import build_de_risk_plan
from core.engines import signal_lineage as lineage


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

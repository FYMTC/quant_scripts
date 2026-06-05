#!/config/quant_env/bin/python3
"""
event_calendar.py — 宏观 / 地缘 / 系统性风险日历（R2）

输出 event_level: NORMAL | WATCH | HIGH | CRITICAL
结合：关键词新闻扫描、HMM 市场状态、指数回撤、人工 override、risk_snapshot。
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KEYWORDS_PATH = os.path.join(ROOT, "data", "event_risk_keywords.yaml")
OVERRIDE_PATH = os.path.join(ROOT, "data", "event_risk_override.json")
FORWARD_PATH = os.path.join(ROOT, "data", "event_calendar_forward.json")
CRON_STATE_PATH = os.path.join(ROOT, "data", "cron_state.json")
RISK_SNAPSHOT = os.path.join(ROOT, "data", "risk_snapshot.json")
OMNIDATA = os.environ.get("OMNIDATA_URL", "http://localhost:8380/api/v1/spiders/run")

LEVEL_ORDER = ("NORMAL", "WATCH", "HIGH", "CRITICAL")


def _load_yaml(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_json(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _level_max(a: str, b: str) -> str:
    ia = LEVEL_ORDER.index(a) if a in LEVEL_ORDER else 0
    ib = LEVEL_ORDER.index(b) if b in LEVEL_ORDER else 0
    return LEVEL_ORDER[max(ia, ib)]


def _match_keywords(text: str, cfg: dict) -> Tuple[str, List[str]]:
    """返回 (level_from_keywords, hits)"""
    if not text:
        return "NORMAL", []
    score = 0
    hits: List[str] = []
    groups = (cfg.get("keyword_groups") or {})
    for gname, g in groups.items():
        w = int(g.get("weight") or 1)
        for kw in g.get("keywords") or []:
            try:
                if re.search(kw, text, re.I):
                    score += w
                    hits.append(f"{gname}:{kw}")
            except re.error:
                if kw in text:
                    score += w
                    hits.append(f"{gname}:{kw}")

    if score >= 8:
        return "CRITICAL", hits
    if score >= 5:
        return "HIGH", hits
    if score >= 2:
        return "WATCH", hits
    return "NORMAL", hits


def _fetch_news_snippets(queries: List[str], timeout: int = 8) -> str:
    """OmniData 新闻标题拼接（失败则空串）。"""
    chunks: List[str] = []
    for q in queries[:4]:
        try:
            payload = json.dumps(
                {"spider_name": "eastmoney_search", "params": {"keyword": q, "search_type": "news", "page_size": 8}}
            ).encode()
            req = urllib.request.Request(
                OMNIDATA, data=payload, headers={"Content-Type": "application/json"}
            )
            raw = urllib.request.urlopen(req, timeout=timeout).read()
            data = json.loads(raw)
            if not data.get("success"):
                continue
            items = data.get("data") or []
            if isinstance(items, list):
                for it in items[:8]:
                    if isinstance(it, dict):
                        chunks.append(str(it.get("标题") or it.get("title") or ""))
        except Exception:
            continue
    return "\n".join(chunks)


def _hmm_level() -> Tuple[str, dict]:
    try:
        from market_regime import fit_hmm, fetch_index_data
        import numpy as np

        closes = fetch_index_data("000001", 500)
        if closes is None or len(closes) < 60:
            return "NORMAL", {}
        rets = np.diff(np.log(closes))
        hmm = fit_hmm(rets)
        if not hmm or hmm.get("error"):
            return "NORMAL", hmm or {}
        state = hmm.get("current_state", "sideways")
        probs = hmm.get("current_probs") or [0, 0, 0]
        bear_p = float(probs[0]) if len(probs) > 0 else 0
        detail = {"hmm_state": state, "bear_prob": round(bear_p, 3)}
        if state == "bear" and bear_p >= 0.55:
            return "HIGH", detail
        if state == "bear" and bear_p >= 0.4:
            return "WATCH", detail
        return "NORMAL", detail
    except Exception as e:
        return "NORMAL", {"hmm_error": str(e)[:120]}


def _index_drawdown_level() -> Tuple[str, dict]:
    try:
        from data_converter import fetch_kline_baostock

        end = date.today().strftime("%Y%m%d")
        start = date.today().replace(year=date.today().year - 1).strftime("%Y%m%d")
        rec = fetch_kline_baostock("000001", start, end)
        if not rec or len(rec) < 20:
            return "NORMAL", {}
        closes = [float(r["收盘"]) for r in rec]
        peak = max(closes[-60:]) if len(closes) >= 60 else max(closes)
        last = closes[-1]
        dd = (last - peak) / peak if peak > 0 else 0
        detail = {"index_proxy": "000001", "drawdown_60d_pct": round(dd * 100, 2)}
        if dd <= -0.08:
            return "HIGH", detail
        if dd <= -0.05:
            return "WATCH", detail
        return "NORMAL", detail
    except Exception as e:
        return "NORMAL", {"index_error": str(e)[:120]}


def _portfolio_stress_level() -> Tuple[str, dict]:
    snap = _load_json(RISK_SNAPSHOT)
    flags = snap.get("flags") or []
    n_danger = sum(1 for f in flags if f.get("level") == "danger")
    n_warn = sum(1 for f in flags if f.get("level") == "warning")
    detail = {"danger_flags": n_danger, "warning_flags": n_warn}
    if n_danger >= 2:
        return "HIGH", detail
    if n_danger >= 1 or n_warn >= 3:
        return "WATCH", detail
    return "NORMAL", detail


def _decay_level(rl: str) -> str:
    """衰减一级：HIGH→WATCH→NORMAL，CRITICAL→HIGH"""
    order = {"CRITICAL": "HIGH", "HIGH": "WATCH", "WATCH": "NORMAL", "NORMAL": "NORMAL"}
    return order.get(rl.upper(), "NORMAL")


def _forward_events_active() -> Tuple[str, List[dict]]:
    """
    读取 event_calendar_forward.json，返回当前在影响窗口内的
    最高 risk_level 和活跃事件列表。

    窗口 = [event_date - lead_days,  event_date + decay_days]
    - lead_days 前：完整 risk_level（预防性预警）
    - decay_days 后：衰减为 decay_level（事后余震），默认降一级
    - 每天热加载 —— Hermes 新增/修改事件后即时生效。
    """
    fwd = _load_json(FORWARD_PATH)
    events = fwd.get("events") or []
    today = date.today()
    from datetime import timedelta

    active: List[dict] = []
    peak_level = "NORMAL"
    for ev in events:
        if not ev.get("active", True):
            continue
        try:
            event_date = date.fromisoformat(ev["date"])
        except (ValueError, KeyError):
            continue
        lead = int(ev.get("lead_days", 0))
        decay = int(ev.get("decay_days", 0))
        window_start = event_date - timedelta(days=lead)
        window_end = event_date + timedelta(days=decay)
        if window_start <= today <= window_end:
            # 事后衰减期用衰减级别
            if today > event_date and decay > 0:
                rl = ev.get("decay_level") or _decay_level(ev.get("risk_level", "WATCH"))
            else:
                rl = ev.get("risk_level", "WATCH").upper()
            effective_rl = rl if rl.upper() in LEVEL_ORDER else "WATCH"
            ev_copy = dict(ev)
            ev_copy["effective_level"] = effective_rl
            ev_copy["in_decay"] = today > event_date
            active.append(ev_copy)
            peak_level = _level_max(peak_level, effective_rl.upper())
    return peak_level, active


def _expired_events_for_review() -> List[dict]:
    """
    返回已完全过期（超出 decay_days 窗口）但仍标记 active 的事件。
    供 Hermes 夜报复盘时审查：是停用还是延长衰减期。
    """
    fwd = _load_json(FORWARD_PATH)
    events = fwd.get("events") or []
    today = date.today()
    from datetime import timedelta

    candidates: List[dict] = []
    for ev in events:
        if not ev.get("active", True):
            continue
        try:
            event_date = date.fromisoformat(ev["date"])
        except (ValueError, KeyError):
            continue
        decay = int(ev.get("decay_days", 0))
        window_end = event_date + timedelta(days=decay)
        if today > window_end:
            candidates.append(ev)
    return candidates


def assess_event_risk(*, scan_news: bool = True) -> dict:
    """
    主入口：评估当日宏观/地缘风险级别与 playbook。
    前瞻事件日历 (event_calendar_forward.json) 在 lead_days 窗口内自动
    提升 event_level —— Hermes 可通过自然语言添加风险事件，下次评估即时生效。
    """
    cfg = _load_yaml(KEYWORDS_PATH)
    playbooks = cfg.get("playbooks") or {}

    override = _load_json(OVERRIDE_PATH)
    if override.get("force_level") in LEVEL_ORDER:
        level = override["force_level"]
        hits = [f"override:{override.get('reason', 'manual')}"]
        news_text = ""
    else:
        news_text = ""
        if scan_news:
            news_text = _fetch_news_snippets(
                ["中美 会见", "地缘 风险", "光模块 砍单", "A股 暴跌", "科技 泡沫"]
            )
        level, hits = _match_keywords(news_text, cfg)

    # ── 前瞻事件日历（热加载，lead_days 窗口内自动提升） ──
    fwd_level, fwd_active = _forward_events_active()
    level = _level_max(level, fwd_level)

    level = _level_max(level, _hmm_level()[0])
    level = _level_max(level, _index_drawdown_level()[0])
    level = _level_max(level, _portfolio_stress_level()[0])

    hmm_detail = _hmm_level()[1]
    idx_detail = _index_drawdown_level()[1]
    pf_detail = _portfolio_stress_level()[1]

    playbook = dict(playbooks.get(level) or playbooks.get("NORMAL") or {})
    playbook["level"] = level

    rec = "READY"
    if level == "CRITICAL":
        rec = "BLOCKED"
    elif level == "HIGH":
        rec = "CAUTION"
    elif level == "WATCH":
        rec = "CAUTION"

    out = {
        "assessed_at": datetime.now().isoformat(),
        "date": date.today().isoformat(),
        "event_level": level,
        "recommendation_override": rec,
        "keyword_hits": hits[:30],
        "news_scan_chars": len(news_text),
        "playbook": playbook,
        "components": {
            "hmm": hmm_detail,
            "index_drawdown": idx_detail,
            "portfolio_flags": pf_detail,
            "override": override if override else None,
            "forward_events": {
                "active_count": len(fwd_active),
                "contributed_level": fwd_level if fwd_active else None,
                "events": [
                    {
                        "id": e["id"],
                        "date": e["date"],
                        "title": e.get("title", ""),
                        "risk_level": e.get("risk_level", ""),
                        "days_until": (date.fromisoformat(e["date"]) - date.today()).days if e.get("date") else None,
                    }
                    for e in fwd_active[:10]
                ],
            },
        },
    }
    return out


def get_playbook(level: Optional[str] = None) -> dict:
    cfg = _load_yaml(KEYWORDS_PATH)
    playbooks = cfg.get("playbooks") or {}
    if level:
        return dict(playbooks.get(level) or {})
    ev = assess_event_risk(scan_news=False)
    return dict(ev.get("playbook") or {})


def merge_recommendation(base: str, event_assessment: dict) -> str:
    """宏观优先：CRITICAL→BLOCKED；HIGH 至少 CAUTION。"""
    ov = event_assessment.get("recommendation_override", "READY")
    order = {"READY": 0, "CAUTION": 1, "BLOCKED": 2}
    return max(base, ov, key=lambda x: order.get(x, 0))


def update_cron_state(event_assessment: dict, *, slot: str = "macro") -> dict:
    state = _load_json(CRON_STATE_PATH)
    state["date"] = date.today().isoformat()
    state["event_level"] = event_assessment.get("event_level")
    state["event_assessed_at"] = event_assessment.get("assessed_at")
    state.setdefault("previous_verdicts", {})[slot] = event_assessment.get("recommendation_override")
    state["playbook"] = event_assessment.get("playbook")
    state["keyword_hits"] = event_assessment.get("keyword_hits", [])[:15]
    os.makedirs(os.path.dirname(CRON_STATE_PATH), exist_ok=True)
    with open(CRON_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    return state


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--update-cron-state", action="store_true")
    args = ap.parse_args()
    r = assess_event_risk(scan_news=not args.no_news)
    if args.update_cron_state:
        update_cron_state(r)
    print(json.dumps(r, ensure_ascii=False, indent=2))

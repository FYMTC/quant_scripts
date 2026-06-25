#!python3
"""
signal_lineage.py — 标的/信号全生命周期追溯（append-only）

阶段 stage 建议：
  SCREENER_ORIGIN | MACRO_ASSESS | SIGNAL_REGISTER | TRIGGER | FILTER_* |
  DESK_ENQUEUE | ANALYZE | GATE | PROPOSE | RESOLVE | DE_RISK_PLAN
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional

LINEAGE_LOG = "/config/quant_scripts/data/signal_lineage.jsonl"


def new_lineage_id(prefix: str = "lin") -> str:
    return f"{prefix}_{date.today().strftime('%Y%m%d')}_{uuid.uuid4().hex[:10]}"


def append(
    stage: str,
    source: str,
    *,
    code: str = "",
    lineage_id: Optional[str] = None,
    payload: Optional[dict] = None,
    parent_lineage_id: Optional[str] = None,
) -> str:
    """写入一条追溯记录；返回 lineage_id（新建或沿用）。"""
    lid = lineage_id or new_lineage_id()
    entry = {
        "ts": datetime.now().isoformat(),
        "date": date.today().isoformat(),
        "lineage_id": lid,
        "parent_lineage_id": parent_lineage_id,
        "stage": stage,
        "source": source,
        "code": code or "",
        "payload": payload or {},
    }
    os.makedirs(os.path.dirname(LINEAGE_LOG), exist_ok=True)
    with open(LINEAGE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return lid


def read_lineage(lineage_id: str, *, max_entries: int = 80) -> List[dict]:
    if not lineage_id or not os.path.isfile(LINEAGE_LOG):
        return []
    rows: List[dict] = []
    with open(LINEAGE_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("lineage_id") == lineage_id or e.get("parent_lineage_id") == lineage_id:
                rows.append(e)
    return rows[-max_entries:]


def format_timeline(lineage_id: str, *, max_chars: int = 1800) -> str:
    """供微信/请示附带的流程摘要。"""
    rows = read_lineage(lineage_id)
    if not rows:
        return f"(无 lineage 记录: {lineage_id})"

    lines = [f"── 流程追溯 {lineage_id} ──"]
    for e in rows:
        ts = (e.get("ts") or "")[11:19]
        stage = e.get("stage", "?")
        src = e.get("source", "")
        code = e.get("code") or ""
        pl = e.get("payload") or {}
        brief = pl.get("summary") or pl.get("message") or pl.get("decision") or pl.get("verdict") or ""
        if isinstance(brief, dict):
            brief = json.dumps(brief, ensure_ascii=False)[:120]
        else:
            brief = str(brief)[:120]
        code_s = f" [{code}]" if code else ""
        lines.append(f"{ts} {stage}{code_s} ({src}) {brief}".rstrip())

    text = "\n".join(lines)
    if len(text) > max_chars:
        return text[: max_chars - 20] + "\n…(已截断)"
    return text


def link_audit(lineage_id: str, audit_entry: dict) -> dict:
    """合并进 signal_audit 条目。"""
    audit_entry["lineage_id"] = lineage_id
    return audit_entry

#!/usr/local/bin/python3
"""Runtime strategy validation for zero-trade feedback loops."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("QUANT_RUNTIME_DATA_DIR") or os.path.join(ROOT, "data")
PLAN_BUNDLE_PATH = os.path.join(DATA, "plan_bundle.json")
VALIDATION_DB_PATH = os.path.join(DATA, "strategy_validation.db")
FEATURE_SNAPSHOT_PATH = os.path.join(DATA, "feature_snapshot.json")


def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(VALIDATION_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS strategy_candidate_trials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            score REAL,
            strategy_id TEXT,
            selector_ref TEXT,
            recommendation TEXT,
            blocked INTEGER DEFAULT 0,
            blocked_reason TEXT,
            market_regime TEXT,
            risk_level TEXT,
            cvar REAL,
            feature_missing INTEGER DEFAULT 0,
            close_price REAL,
            next_open REAL,
            next_close REAL,
            return_pct_open REAL,
            return_pct_close REAL,
            win_open INTEGER,
            win_close INTEGER,
            checked_at TEXT,
            source TEXT,
            raw_payload TEXT,
            UNIQUE(trade_date, code, strategy_id)
        )
        """
    )
    return conn


def _trade_date_from_bundle(bundle: Dict[str, Any]) -> str:
    generated_at = str(bundle.get("generated_at") or "")
    if len(generated_at) >= 10:
        return generated_at[:10]
    return datetime.now().strftime("%Y-%m-%d")


def _pick_rows(bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = bundle.get("candidates_top") or bundle.get("candidates") or []
    return rows if isinstance(rows, list) else []


def _constraint_summary(bundle: Dict[str, Any]) -> str:
    parts = []
    for row in bundle.get("constraints") or []:
        if not isinstance(row, dict) or row.get("pass", True):
            continue
        msg = row.get("message") or row.get("check") or "constraint_blocked"
        parts.append(str(msg))
    return " | ".join(parts[:5])


def _candidate_block_reason(bundle: Dict[str, Any], candidate: Dict[str, Any]) -> str:
    blocked_reasons = []
    if str(bundle.get("recommendation") or "") == "BLOCKED":
        summary = _constraint_summary(bundle)
        if summary:
            blocked_reasons.append(summary)
    missing = candidate.get("feature_missing_reason")
    if missing:
        blocked_reasons.append(str(missing))
    return " | ".join(blocked_reasons)


def record_plan_candidates(
    plan_bundle: Optional[Dict[str, Any]] = None,
    feature_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    bundle = plan_bundle or _load_json(PLAN_BUNDLE_PATH)
    if not bundle:
        return {"ok": False, "reason": "plan_bundle_missing", "saved": 0}

    feature_snapshot = feature_snapshot or _load_json(FEATURE_SNAPSHOT_PATH)
    per_stock = (feature_snapshot.get("per_stock") or {}) if isinstance(feature_snapshot, dict) else {}
    recommendation = str(bundle.get("recommendation") or "")
    trade_date = _trade_date_from_bundle(bundle)
    rows = _pick_rows(bundle)

    saved = 0
    conn = _db()
    try:
        for candidate in rows:
            if not isinstance(candidate, dict):
                continue
            code = str(candidate.get("code") or "").strip()
            if not code:
                continue
            name = str(candidate.get("name") or code)
            score = candidate.get("composite_score")
            feature_row = per_stock.get(code) or {}
            feature_missing = not bool(feature_row)
            strategy_id = str(candidate.get("strategy_id") or candidate.get("strategy") or "screener_top")
            selector_ref = str(candidate.get("selector_ref") or "")
            blocked = 1 if recommendation == "BLOCKED" or feature_missing else 0
            blocked_reason = _candidate_block_reason(bundle, {
                **candidate,
                "feature_missing_reason": "research_features missing" if feature_missing else "",
            })
            payload = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
            close_price = candidate.get("price") or candidate.get("close") or feature_row.get("close")
            conn.execute(
                """
                INSERT INTO strategy_candidate_trials (
                    trade_date, code, name, score, strategy_id, selector_ref,
                    recommendation, blocked, blocked_reason, market_regime,
                    risk_level, cvar, feature_missing, close_price, source, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, code, strategy_id) DO UPDATE SET
                    name=excluded.name,
                    score=excluded.score,
                    selector_ref=excluded.selector_ref,
                    recommendation=excluded.recommendation,
                    blocked=excluded.blocked,
                    blocked_reason=excluded.blocked_reason,
                    market_regime=excluded.market_regime,
                    risk_level=excluded.risk_level,
                    cvar=excluded.cvar,
                    feature_missing=excluded.feature_missing,
                    close_price=COALESCE(strategy_candidate_trials.close_price, excluded.close_price),
                    source=excluded.source,
                    raw_payload=excluded.raw_payload
                """,
                (
                    trade_date,
                    code,
                    name,
                    float(score) if score is not None else None,
                    strategy_id,
                    selector_ref,
                    recommendation,
                    blocked,
                    blocked_reason,
                    str(((feature_snapshot.get("portfolio") or {}).get("market_regime") or {}).get("current_state") or ""),
                    str(feature_row.get("risk_level") or ""),
                    float(feature_row.get("cvar")) if feature_row.get("cvar") is not None else None,
                    1 if feature_missing else 0,
                    float(close_price) if close_price not in (None, "") else None,
                    "plan_bundle",
                    payload,
                ),
            )
            saved += 1
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "saved": saved, "trade_date": trade_date, "candidate_count": len(rows)}


def _bs_code(code: str) -> str:
    return f"sz.{code}" if code.startswith(("0", "3")) else f"sh.{code}"


def _fetch_day_bars(codes: List[str], trade_date: str) -> Dict[str, Dict[str, float]]:
    if not codes:
        return {}
    import baostock as bs

    out: Dict[str, Dict[str, float]] = {}
    bs.login()
    try:
        for code in codes:
            rs = bs.query_history_k_data_plus(
                _bs_code(code),
                "date,open,close",
                start_date=trade_date,
                end_date=trade_date,
                frequency="d",
                adjustflag="2",
            )
            while rs.next():
                row = rs.get_row_data()
                out[code] = {
                    "date": row[0],
                    "open": float(row[1]),
                    "close": float(row[2]),
                }
    finally:
        bs.logout()
    return out


def evaluate_previous_candidates(
    current_trade_date: Optional[str] = None,
) -> Dict[str, Any]:
    current_trade_date = current_trade_date or datetime.now().strftime("%Y-%m-%d")
    previous_trade_date = (datetime.strptime(current_trade_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM strategy_candidate_trials WHERE trade_date=? AND checked_at IS NULL",
        (previous_trade_date,),
    ).fetchall()
    if not rows:
        conn.close()
        return {
            "ok": True,
            "trade_date": previous_trade_date,
            "checked": 0,
            "summary": summarize_validation(conn=None, trade_date=previous_trade_date),
        }

    day_bars = _fetch_day_bars([str(r["code"]) for r in rows], current_trade_date)
    checked = 0
    for row in rows:
        bar = day_bars.get(str(row["code"]))
        if not bar:
            continue
        close_price = float(row["close_price"] or 0.0)
        if close_price <= 0:
            continue
        next_open = bar["open"]
        next_close = bar["close"]
        ret_open = (next_open - close_price) / close_price * 100.0
        ret_close = (next_close - close_price) / close_price * 100.0
        conn.execute(
            """
            UPDATE strategy_candidate_trials
               SET next_open=?, next_close=?, return_pct_open=?, return_pct_close=?,
                   win_open=?, win_close=?, checked_at=?
             WHERE id=?
            """,
            (
                round(next_open, 4),
                round(next_close, 4),
                round(ret_open, 2),
                round(ret_close, 2),
                1 if ret_open > 0 else 0,
                1 if ret_close > 0 else 0,
                datetime.now().isoformat(),
                row["id"],
            ),
        )
        checked += 1
    conn.commit()
    summary = summarize_validation(conn=conn, trade_date=previous_trade_date)
    conn.close()
    return {"ok": True, "trade_date": previous_trade_date, "checked": checked, "summary": summary}


def summarize_validation(conn: Optional[sqlite3.Connection] = None, trade_date: Optional[str] = None) -> Dict[str, Any]:
    owns_conn = conn is None
    conn = conn or _db()
    where = ""
    params: List[Any] = []
    if trade_date:
        where = "WHERE trade_date=?"
        params.append(trade_date)

    rows = conn.execute(
        f"SELECT * FROM strategy_candidate_trials {where} ORDER BY trade_date DESC, score DESC NULLS LAST",
        params,
    ).fetchall()
    total = len(rows)
    checked_rows = [r for r in rows if r["checked_at"]]
    measured = [r for r in checked_rows if r["return_pct_open"] is not None]
    positive = [r for r in measured if (r["return_pct_open"] or 0) > 0]
    blocked_positive = [r for r in positive if r["blocked"]]
    missing_feature = [r for r in rows if r["feature_missing"]]

    by_strategy: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        sid = row["strategy_id"] or "unknown"
        bucket = by_strategy.setdefault(sid, {"strategy_id": sid, "total": 0, "checked": 0, "wins": 0, "blocked_positive": 0})
        bucket["total"] += 1
        if row["checked_at"] and row["return_pct_open"] is not None:
            bucket["checked"] += 1
            if (row["return_pct_open"] or 0) > 0:
                bucket["wins"] += 1
                if row["blocked"]:
                    bucket["blocked_positive"] += 1

    summary = {
        "trade_date": trade_date,
        "candidate_count": total,
        "checked_count": len(checked_rows),
        "measured_count": len(measured),
        "hit_rate": round(len(positive) / len(measured), 4) if measured else None,
        "avg_return_pct_open": round(sum((r["return_pct_open"] or 0) for r in measured) / len(measured), 4) if measured else None,
        "avg_return_pct_close": round(sum((r["return_pct_close"] or 0) for r in measured) / len(measured), 4) if measured else None,
        "blocked_positive_count": len(blocked_positive),
        "blocked_positive_codes": [r["code"] for r in blocked_positive[:10]],
        "missing_feature_count": len(missing_feature),
        "missing_feature_codes": [r["code"] for r in missing_feature[:10]],
        "coverage_gap_ratio": round(len(missing_feature) / total, 4) if total else None,
        "by_strategy": list(by_strategy.values()),
    }
    if owns_conn:
        conn.close()
    return summary

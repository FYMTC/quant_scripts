#!/usr/local/bin/python3
"""策略注册、选股谓词、夜间评审基础实现。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
WIKI_STRATEGY_DIR = os.path.join("/config/quant-wiki", "strategies")
DATA_DIR = os.path.join(ROOT, "data")
REGISTRY_PATH = os.path.join(DATA_DIR, "strategy_registry.json")
NIGHT_REVIEW_PATH = os.path.join(DATA_DIR, "strategy_night_review.json")
NIGHT_OUTPUT_PATH = os.path.join(DATA_DIR, "night_output.json")


def _parse_frontmatter(text: str) -> Dict[str, Any]:
    if not text.startswith("---\n"):
        return {}
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        return {}
    fm = parts[1].strip().splitlines()
    data: Dict[str, Any] = {}
    for line in fm:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


@dataclass
class StrategyRecord:
    strategy_id: str
    title: str
    path: str
    status: str
    review_mode: str
    selector_ref: str
    symbols_scope: str
    holding_period: str
    risk_profile: str
    version: str
    type: str = "strategy"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "title": self.title,
            "path": self.path,
            "status": self.status,
            "review_mode": self.review_mode,
            "selector_ref": self.selector_ref,
            "symbols_scope": self.symbols_scope,
            "holding_period": self.holding_period,
            "risk_profile": self.risk_profile,
            "version": self.version,
            "type": self.type,
        }


REQUIRED_FIELDS = [
    "type",
    "strategy_id",
    "status",
    "review_mode",
    "selector_ref",
    "symbols_scope",
    "holding_period",
    "risk_profile",
    "version",
]


def scan_strategy_docs(strategy_dir: str = WIKI_STRATEGY_DIR) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(strategy_dir):
        return out
    for name in sorted(os.listdir(strategy_dir)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(strategy_dir, name)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        fm = _parse_frontmatter(text)
        if not fm:
            continue
        if str(fm.get("type", "")).strip() != "strategy":
            continue
        missing = [k for k in REQUIRED_FIELDS if not fm.get(k)]
        if missing:
            raise ValueError(f"strategy doc missing fields: {name}: {', '.join(missing)}")
        out.append(
            StrategyRecord(
                strategy_id=str(fm["strategy_id"]),
                title=str(fm.get("title") or fm["strategy_id"]),
                path=path,
                status=str(fm["status"]),
                review_mode=str(fm["review_mode"]),
                selector_ref=str(fm["selector_ref"]),
                symbols_scope=str(fm["symbols_scope"]),
                holding_period=str(fm["holding_period"]),
                risk_profile=str(fm["risk_profile"]),
                version=str(fm["version"]),
            ).to_dict()
        )
    return out


def build_registry(strategy_dir: str = WIKI_STRATEGY_DIR, registry_path: str = REGISTRY_PATH) -> Dict[str, Any]:
    strategies = scan_strategy_docs(strategy_dir)
    registry = {
        "generated_at": datetime.now().isoformat(),
        "strategy_count": len(strategies),
        "strategies": strategies,
    }
    os.makedirs(os.path.dirname(registry_path), exist_ok=True)
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    return registry


def load_registry(registry_path: str = REGISTRY_PATH) -> Dict[str, Any]:
    if os.path.isfile(registry_path):
        with open(registry_path, encoding="utf-8") as f:
            return json.load(f)
    return build_registry()


def selector_mean_reversion_backtest(symbol: str, snapshot: Optional[Dict[str, Any]] = None, features: Optional[Dict[str, Any]] = None, market_regime: Optional[Dict[str, Any]] = None, symbol_profile: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params = params or {}
    threshold_drop = float(params.get("drop", -3.0))
    threshold_close_pos = float(params.get("close_pos", 0.4))
    threshold_ma = bool(params.get("trend_filter", True))
    feats = features or {}
    price = float((snapshot or {}).get("current_price") or 0)
    if price <= 0:
        return {"passed": False, "score": 0.0, "reason": "missing_price", "evidence": {}}
    day_return = float(feats.get("day_return") or feats.get("drop_pct") or 0)
    close_pos = float(feats.get("close_pos") or 0)
    ma_ok = bool(feats.get("close_above_ma20", True)) if threshold_ma else True
    passed = day_return <= threshold_drop and close_pos < threshold_close_pos and ma_ok
    score = 0.0
    if passed:
        score = round(min(1.0, abs(day_return) / abs(threshold_drop) * 0.5 + (threshold_close_pos - close_pos) * 0.5), 3)
    return {
        "passed": passed,
        "score": score,
        "reason": "mean_reversion_entry" if passed else "criteria_not_met",
        "evidence": {
            "day_return": day_return,
            "close_pos": close_pos,
            "ma_ok": ma_ok,
            "threshold_drop": threshold_drop,
            "threshold_close_pos": threshold_close_pos,
        },
    }


SELECTOR_REGISTRY = {
    "selectors.mean_reversion_v1": selector_mean_reversion_backtest,
}


def route_strategy(strategy: Dict[str, Any], symbol: str, snapshot: Optional[Dict[str, Any]] = None, features: Optional[Dict[str, Any]] = None, market_regime: Optional[Dict[str, Any]] = None, symbol_profile: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    selector_ref = strategy.get("selector_ref") or ""
    selector = SELECTOR_REGISTRY.get(selector_ref)
    if selector is None:
        return {"passed": False, "score": 0.0, "reason": "selector_missing", "selector_ref": selector_ref}
    result = selector(symbol, snapshot=snapshot, features=features, market_regime=market_regime, symbol_profile=symbol_profile, params=params)
    result["selector_ref"] = selector_ref
    result["strategy_id"] = strategy.get("strategy_id")
    return result


def nightly_review(registry: Optional[Dict[str, Any]] = None, review_path: str = NIGHT_REVIEW_PATH, night_output_path: str = NIGHT_OUTPUT_PATH, signal_audit: Optional[Dict[str, Any]] = None, feature_snapshot: Optional[Dict[str, Any]] = None, trade_log: Optional[Dict[str, Any]] = None, stock_kb: Optional[Dict[str, Any]] = None, strategy_validation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    registry = registry or load_registry()
    active = [s for s in registry.get("strategies", []) if s.get("status") == "active"]
    reports: List[Dict[str, Any]] = []
    validation = strategy_validation or {}
    blocked_positive_count = int(validation.get("blocked_positive_count") or 0)
    missing_feature_count = int(validation.get("missing_feature_count") or 0)
    avg_return = validation.get("avg_return_pct_open")
    hit_rate = validation.get("hit_rate")
    for s in active:
        decision = "keep_observing"
        reason = "active_strategy_default"
        if feature_snapshot and not feature_snapshot.get("runtime_flags", {}).get("feature_fresh", True):
            decision = "pause"
            reason = "feature_snapshot_stale"
        elif missing_feature_count > 0:
            decision = "fix_feature_coverage"
            reason = "candidate_feature_gap"
        elif blocked_positive_count > 0:
            decision = "relax_gate_candidate"
            reason = "blocked_positive_opportunity"
        elif avg_return is not None and avg_return < 0:
            decision = "optimize"
            reason = "negative_next_day_return"
        elif signal_audit and signal_audit.get("entries_count", 0) > 0:
            decision = "optimize"
            reason = "has_runtime_feedback"
        reports.append({
            "strategy_id": s.get("strategy_id"),
            "title": s.get("title"),
            "decision": decision,
            "reason": reason,
            "validation_summary": {
                "hit_rate": hit_rate,
                "avg_return_pct_open": avg_return,
                "blocked_positive_count": blocked_positive_count,
                "missing_feature_count": missing_feature_count,
            },
            "next_day_plan": {
                "focus": "review selector and thresholds",
                "selector_ref": s.get("selector_ref"),
                "version": s.get("version"),
            },
        })
    out = {
        "generated_at": datetime.now().isoformat(),
        "active_strategy_count": len(active),
        "reports": reports,
        "inputs": {
            "signal_audit": bool(signal_audit),
            "feature_snapshot": bool(feature_snapshot),
            "trade_log": bool(trade_log),
            "stock_kb": bool(stock_kb),
        },
    }
    os.makedirs(os.path.dirname(review_path), exist_ok=True)
    with open(review_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    night_output = {
        "generated_at": out["generated_at"],
        "recommendation": "READY",
        "strategy_review": reports,
    }
    with open(night_output_path, "w", encoding="utf-8") as f:
        json.dump(night_output, f, ensure_ascii=False, indent=2)
    return out

#!/usr/local/bin/python3
"""Night pipeline strategy summary bridge."""

from __future__ import annotations

import json
import os
from typing import Any, Dict

import strategy_registry as sr
import strategy_validation as sv

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
NIGHT_OUTPUT_PATH = os.path.join(DATA, "night_output.json")
REVIEW_BUNDLE_PATH = os.path.join(DATA, "review_bundle.json")
FEATURE_SNAPSHOT_PATH = os.path.join(DATA, "feature_snapshot.json")
SIGNAL_AUDIT_PATH = os.path.join(ROOT, "signal_audit.jsonl")
TRADE_LOG_PATH = os.path.join(ROOT, "trade_log.db")
STRATEGY_REVIEW_PATH = os.path.join(DATA, "strategy_night_review.json")


def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_signal_audit(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                entries.append(obj)
    return {"entries_count": len(entries), "entries": entries[-200:]}


def build_strategy_night_output() -> Dict[str, Any]:
    registry = sr.load_registry()
    feature_snapshot = _load_json(FEATURE_SNAPSHOT_PATH)
    signal_audit = _load_signal_audit(SIGNAL_AUDIT_PATH)
    review_bundle = _load_json(REVIEW_BUNDLE_PATH)
    validation_eval = sv.evaluate_previous_candidates()
    validation_summary = validation_eval.get("summary") or sv.summarize_validation(trade_date=None)
    trade_log = {"path": TRADE_LOG_PATH, "exists": os.path.isfile(TRADE_LOG_PATH)}
    strategy_review = sr.nightly_review(
        registry=registry,
        review_path=STRATEGY_REVIEW_PATH,
        night_output_path=NIGHT_OUTPUT_PATH,
        signal_audit=signal_audit,
        feature_snapshot=feature_snapshot,
        trade_log=trade_log,
        stock_kb=review_bundle.get("account_runtime"),
        strategy_validation=validation_summary,
    )
    night_output = _load_json(NIGHT_OUTPUT_PATH)
    night_output["strategy_review"] = strategy_review.get("reports", [])
    night_output["strategy_review_generated_at"] = strategy_review.get("generated_at")
    night_output["strategy_validation"] = validation_summary
    night_output["strategy_validation_generated_at"] = strategy_review.get("generated_at")
    with open(NIGHT_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(night_output, f, ensure_ascii=False, indent=2)
    return night_output


if __name__ == "__main__":
    print(json.dumps(build_strategy_night_output(), ensure_ascii=False, indent=2))

#!/config/quant_env/bin/python3
"""
feature_snapshot.py — 统一 research/risk feature snapshot。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
SNAPSHOT_PATH = os.path.join(DATA, "feature_snapshot.json")
STALE_AFTER_SEC = 6 * 3600

sys.path.insert(0, ROOT)

import risk_monitor as rm  # noqa: E402
import market_regime as mr  # noqa: E402


def _build_market_regime() -> Dict[str, Any]:
    try:
        closes = mr.fetch_index_data(days=500)
        if closes is None or len(closes) <= 30:
            return {"ok": False, "error": "index_data_unavailable"}
        import numpy as np

        log_returns = np.diff(np.log(closes))
        result = mr.fit_hmm(log_returns) or {}
        if result.get("error"):
            return {"ok": False, "error": result["error"]}
        return {
            "ok": True,
            "current_state": result.get("current_state"),
            "current_probs": result.get("current_probs"),
            "state_distribution": result.get("state_distribution"),
            "n_obs": result.get("n_obs"),
            "aic": result.get("aic"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}


def build_feature_snapshot() -> Dict[str, Any]:
    raw = rm.run_full_scan(argparse.Namespace(json=True, code=None))
    portfolio = rm.load_portfolio_from_db()
    positions = portfolio.get("positions") or {}
    watchlist = (rm.load_guard_config().get("watch_list") or {})
    market_regime = _build_market_regime()

    per_stock: Dict[str, Any] = {}
    missing_codes: List[str] = []
    low_quality_codes: List[str] = []
    coverage_codes = set(positions.keys()) | set(watchlist.keys())

    for code in coverage_codes:
        source = (raw.get("positions") or {}).get(code) or (raw.get("watchlist") or {}).get(code)
        if not source:
            missing_codes.append(code)
            continue
        data_quality = source.get("data_quality", "unknown")
        if data_quality != "ok":
            low_quality_codes.append(code)
        per_stock[code] = {
            "code": code,
            "name": source.get("name", code),
            "scope": "position" if code in positions else "watchlist",
            "current_price": source.get("current_price"),
            "position_ratio": source.get("position_ratio"),
            "risk_level": source.get("risk_level", "unknown"),
            "risk_reasons": source.get("risk_reasons") or source.get("watchlist_flags") or [],
            "cvar": (source.get("cvar") or {}).get("value") if isinstance(source.get("cvar"), dict) else source.get("cvar"),
            "cvar_trend": (source.get("cvar") or {}).get("trend") if isinstance(source.get("cvar"), dict) else None,
            "momentum": source.get("momentum"),
            "momentum_analysis": source.get("momentum_analysis"),
            "max_drawdown": source.get("max_drawdown"),
            "garch": source.get("garch"),
            "data_quality": data_quality,
        }

    feature_fresh = len(missing_codes) == 0
    snapshot = {
        "generated_at": datetime.now().isoformat(),
        "as_of_date": datetime.now().strftime("%Y-%m-%d"),
        "stale_after_sec": STALE_AFTER_SEC,
        "source_modules": ["risk_monitor", "market_regime"],
        "portfolio": {
            "total_assets_estimate": raw.get("total_assets_estimate"),
            "available_cash": raw.get("available_cash"),
            "cash": portfolio.get("cash"),
            "positions_count": len(positions),
            "watchlist_count": len(watchlist),
            "flags": raw.get("flags") or [],
            "market_regime": market_regime,
            "event_risk": {"level": "NORMAL", "source": "not_wired_yet"},
        },
        "per_stock": per_stock,
        "runtime_flags": {
            "feature_fresh": feature_fresh,
            "missing_codes": missing_codes,
            "low_quality_codes": low_quality_codes,
            "data_quality_summary": {
                "ok": sum(1 for row in per_stock.values() if row.get("data_quality") == "ok"),
                "non_ok": sum(1 for row in per_stock.values() if row.get("data_quality") != "ok"),
            },
        },
    }
    os.makedirs(DATA, exist_ok=True)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description="build feature snapshot")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    out = build_feature_snapshot()
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(SNAPSHOT_PATH)


if __name__ == "__main__":
    main()

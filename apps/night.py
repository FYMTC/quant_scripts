#!/config/quant_env/bin/python3
"""
apps/night.py — 21:00 夜报摘要（代码管线）

在 night_preflight 跑完选股/审计等之后，由 night_app 调用；聚合当日 close JSON + 选股摘要，
供 Hermes 短 prompt 只读（详细 shell 步骤可逐步下线）。
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))
from datetime import datetime

import intraday_common as ic
from apps import morning as morning_app

RUNTIME_DATA_DIR = os.environ.get("QUANT_RUNTIME_DATA_DIR") or "/config/quant_scripts/data"
CLOSE_JSON = os.path.join(RUNTIME_DATA_DIR, "close_output.json")
NIGHT_QUANT_JSON = os.path.join(RUNTIME_DATA_DIR, "night_quant.json")
OUT_DEFAULT = os.path.join(RUNTIME_DATA_DIR, "night_output.json")


def _load_close_context():
    close_data = ic.load_json(CLOSE_JSON)
    holdings = close_data.get("holdings", []) if isinstance(close_data.get("holdings"), list) else []
    alerts = close_data.get("alerts", []) if isinstance(close_data.get("alerts"), list) else []
    recommendation = close_data.get("recommendation", "READY")
    constraints = close_data.get("constraints", []) if isinstance(close_data.get("constraints"), list) else []
    cash = float(close_data.get("cash") or 0.0)
    total_assets = float(close_data.get("total_assets") or 0.0)
    generated_at = close_data.get("generated_at")

    if holdings and total_assets > 0:
        return {
            "close_data": close_data,
            "holdings": holdings,
            "alerts": alerts,
            "recommendation": recommendation,
            "constraints": constraints,
            "cash": cash,
            "total_assets": total_assets,
            "generated_at": generated_at,
        }

    holdings, cash, total_assets = morning_app.load_holdings()
    fallback_constraints = []
    if total_assets <= 0:
        fallback_constraints.append(
            {
                "check": "total_assets",
                "pass": False,
                "message": "总资产无效",
            }
        )
    fallback_recommendation = recommendation
    if fallback_constraints:
        fallback_recommendation = "BLOCKED"
    elif recommendation == "BLOCKED":
        fallback_recommendation = "READY"
    fallback_close = dict(close_data) if isinstance(close_data, dict) else {}
    fallback_close.update(
        {
            "generated_at": generated_at or datetime.now().isoformat(),
            "holdings": holdings,
            "cash": round(cash, 2),
            "total_assets": round(total_assets, 2),
            "alerts": alerts,
            "constraints": fallback_constraints,
            "recommendation": fallback_recommendation,
        }
    )
    return {
        "close_data": fallback_close,
        "holdings": holdings,
        "alerts": alerts,
        "recommendation": fallback_recommendation,
        "constraints": fallback_constraints,
        "cash": cash,
        "total_assets": total_assets,
        "generated_at": fallback_close.get("generated_at"),
    }


def main():
    t0 = time.time()
    close_ctx = _load_close_context()
    close_data = close_ctx["close_data"]
    candidates = ic.load_candidates_top(8)
    screener = ic.load_candidates_top(15)
    holdings = close_ctx["holdings"]
    alerts = close_ctx["alerts"]

    blocked = False
    if close_ctx["constraints"]:
        blocked = any(not c.get("pass", True) for c in close_ctx["constraints"])

    rec_close = close_ctx["recommendation"]
    if blocked:
        recommendation = "BLOCKED"
    elif rec_close == "CAUTION":
        recommendation = "CAUTION"
    else:
        recommendation = "READY"

    pnl_summary = ic.pnl_summary_from_holdings(
        holdings,
        cash=close_ctx["cash"],
        total_assets=close_ctx["total_assets"],
    )
    night_quant = ic.load_json(NIGHT_QUANT_JSON)
    strategy_review = {}
    try:
        from strategy_night_bridge import build_strategy_night_output

        strategy_review = build_strategy_night_output()
    except Exception as e:
        strategy_review = {"error": str(e)[:200]}
    validation_summary = strategy_review.get("strategy_validation") or {}
    quant = {
        "close_quant_per_stock": close_data.get("quant_per_stock", {}),
        "preflight_modules": night_quant.get("modules") if night_quant else None,
        "night_quant_generated_at": night_quant.get("generated_at") if night_quant else None,
        "strategy_review": strategy_review.get("strategy_review") or [],
        "strategy_review_generated_at": strategy_review.get("strategy_review_generated_at"),
        "strategy_validation": validation_summary,
    }
    if not night_quant:
        quant["preflight_note"] = (
            "未找到 night_quant.json（night_preflight 未写出或路径不同）；"
            "协整/LSTM 等仅见该 cron 的 stderr。"
        )

    out = {
        "generated_at": datetime.now().isoformat(),
        "close_context_at": close_ctx["generated_at"],
        "close_recommendation": rec_close,
        "close_holdings_count": len(holdings),
        "close_alerts": alerts[:20],
        "close_constraints": close_ctx["constraints"],
        "candidates": candidates,
        "screener_path": ic.SCREENER_JSON if os.path.exists(ic.SCREENER_JSON) else None,
        "recommendation": recommendation,
        "note": "完整夜盘前置（选股/信号/风险JSON等）由 night_preflight.py 在 night_app 中先于本脚本执行。",
        "elapsed_sec": round(time.time() - t0, 1),
        "holdings": holdings,
        "screener": screener,
        "alerts": alerts,
        "pnl_summary": pnl_summary,
        "cash": round(close_ctx["cash"], 2),
        "total_assets": round(close_ctx["total_assets"], 2),
        "strategy_review": strategy_review.get("strategy_review") or [],
        "strategy_review_generated_at": strategy_review.get("strategy_review_generated_at"),
        "strategy_validation": validation_summary,
        "quant": quant,
    }

    if not close_data:
        out["warning"] = "close_output.json 缺失或为空；请先跑 15:05 close_app 或手动生成 close_output.json。"

    out = ic.apply_macro_risk(out, slot="night", scan_news=True)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if "--save" in sys.argv:
        idx = sys.argv.index("--save")
        path = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else OUT_DEFAULT
        ic.save_stdout_main(__file__, path)
    else:
        main()

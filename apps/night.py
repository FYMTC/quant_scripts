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

CLOSE_JSON = "/config/quant_scripts/data/close_output.json"
NIGHT_QUANT_JSON = "/config/quant_scripts/data/night_quant.json"
OUT_DEFAULT = "/config/quant_scripts/data/night_output.json"


def main():
    t0 = time.time()
    close_data = ic.load_json(CLOSE_JSON)
    candidates = ic.load_candidates_top(8)
    screener = ic.load_candidates_top(15)
    holdings = close_data.get("holdings", []) if isinstance(close_data.get("holdings"), list) else []
    alerts = close_data.get("alerts", []) if isinstance(close_data.get("alerts"), list) else []

    blocked = False
    if close_data.get("constraints"):
        blocked = any(not c.get("pass", True) for c in close_data["constraints"])

    rec_close = close_data.get("recommendation", "READY")
    if blocked:
        recommendation = "BLOCKED"
    elif rec_close == "CAUTION":
        recommendation = "CAUTION"
    else:
        recommendation = "READY"

    pnl_summary = ic.pnl_summary_from_holdings(holdings)
    night_quant = ic.load_json(NIGHT_QUANT_JSON)
    quant = {
        "close_quant_per_stock": close_data.get("quant_per_stock", {}),
        "preflight_modules": night_quant.get("modules") if night_quant else None,
        "night_quant_generated_at": night_quant.get("generated_at") if night_quant else None,
    }
    if not night_quant:
        quant["preflight_note"] = (
            "未找到 night_quant.json（night_preflight 未写出或路径不同）；"
            "协整/LSTM 等仅见该 cron 的 stderr。"
        )

    out = {
        "generated_at": datetime.now().isoformat(),
        "close_context_at": close_data.get("generated_at"),
        "close_recommendation": rec_close,
        "close_holdings_count": len(holdings),
        "close_alerts": alerts[:20],
        "close_constraints": close_data.get("constraints", []),
        "candidates": candidates,
        "screener_path": ic.SCREENER_JSON if os.path.exists(ic.SCREENER_JSON) else None,
        "recommendation": recommendation,
        "note": "完整夜盘前置（选股/信号/风险JSON等）由 night_preflight.py 在 night_app 中先于本脚本执行。",
        "elapsed_sec": round(time.time() - t0, 1),
        # 与 Hermes 夜报短 prompt 字段名对齐（来自当日 close + 选股落盘）
        "holdings": holdings,
        "screener": screener,
        "alerts": alerts,
        "pnl_summary": pnl_summary,
        "quant": quant,
    }

    if not close_data:
        out["warning"] = "close_output.json 缺失或为空；请先跑 15:05 close_app 或手动生成 close_output.json。"

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if "--save" in sys.argv:
        idx = sys.argv.index("--save")
        path = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else OUT_DEFAULT
        ic.save_stdout_main(__file__, path)
    else:
        main()

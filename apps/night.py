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
OUT_DEFAULT = "/config/quant_scripts/data/night_output.json"


def main():
    t0 = time.time()
    close_data = ic.load_json(CLOSE_JSON)
    candidates = ic.load_candidates_top(8)

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

    out = {
        "generated_at": datetime.now().isoformat(),
        "close_context_at": close_data.get("generated_at"),
        "close_recommendation": rec_close,
        "close_holdings_count": len(close_data.get("holdings", [])),
        "close_alerts": close_data.get("alerts", [])[:20],
        "close_constraints": close_data.get("constraints", []),
        "candidates": candidates,
        "screener_path": ic.SCREENER_JSON if os.path.exists(ic.SCREENER_JSON) else None,
        "recommendation": recommendation,
        "note": "完整夜盘前置（选股/信号/风险JSON等）由 night_preflight.py 在 night_app 中先于本脚本执行。",
        "elapsed_sec": round(time.time() - t0, 1),
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

#!/config/quant_env/bin/python3
"""
apps/afternoon.py — 14:00 下午速报（代码管线）

联动 flash / midday / noon（若已生成），硬约束与盘中告警与 midday 族一致。
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))
from datetime import datetime

import intraday_common as ic

OUT_DEFAULT = "/config/quant_scripts/data/afternoon_output.json"


def main():
    t0 = time.time()
    flash = ic.load_json(ic.FLASH_JSON)
    midday = ic.load_json(ic.MIDDAY_JSON)
    noon = ic.load_json(ic.NOON_JSON)

    holdings, cash, total = ic.load_holdings_and_quotes()
    holdings = ic.merge_flash_context(holdings, flash)
    holdings = ic.merge_prior_snapshot(holdings, midday, "midday")
    holdings = ic.merge_prior_snapshot(holdings, noon, "noon")

    alerts = ic.detect_alerts_intraday(holdings)
    quant = ic.run_quant_flat(holdings)
    constraints = ic.check_constraints_intraday(holdings, cash, total, quant, alerts, True)
    candidates = ic.load_candidates_top(5)
    recommendation = ic.recommend_from(constraints, alerts)
    pos_ratio = round((total - cash) / total * 100, 2) if total > 0 else None
    tier15 = ic.build_tier15_deploy_scan(holdings, cash, total)
    market_flow = {
        "cash": round(cash, 2),
        "total_assets": round(total, 2),
        "position_ratio_pct": pos_ratio,
        "note": "资金面向摘要；明细仍以 holdings 为准。",
    }

    print(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(),
                "flash_context_at": flash.get("generated_at"),
                "midday_context_at": midday.get("generated_at"),
                "noon_context_at": noon.get("generated_at"),
                "holdings": holdings,
                "cash": round(cash, 2),
                "total_assets": round(total, 2),
                "alerts": alerts,
                "constraints": constraints,
                "quant_per_stock": quant,
                "candidates": candidates,
                "tier15_deploy_scan": tier15,
                "market_flow": market_flow,
                "recommendation": recommendation,
                "elapsed_sec": round(time.time() - t0, 1),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    if "--save" in sys.argv:
        idx = sys.argv.index("--save")
        path = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else OUT_DEFAULT
        ic.save_stdout_main(__file__, path)
    else:
        main()

#!python3
"""
apps/noon.py — 11:30 午间总结（代码管线）

与 09:30 flash、10:00 midday 联动，输出 JSON 供 Hermes 只读。
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))
from datetime import datetime

import intraday_common as ic

OUT_DEFAULT = "/config/quant_scripts/data/noon_output.json"


def main():
    t0 = time.time()
    flash = ic.load_json(ic.FLASH_JSON)
    midday = ic.load_json(ic.MIDDAY_JSON)

    holdings, cash, total = ic.load_holdings_and_quotes()
    holdings = ic.merge_flash_context(holdings, flash)
    holdings = ic.merge_prior_snapshot(holdings, midday, "midday")

    alerts = ic.detect_alerts_intraday(holdings)
    for h in holdings:
        if h.get("vs_midday_pct") is not None and h["vs_midday_pct"] < -1.5 and h.get("vs_open_pct", 0) < 0:
            alerts.append(
                {
                    "code": h["code"],
                    "name": h["name"],
                    "type": "NOON_MIDDAY_FADE",
                    "severity": "MEDIUM",
                    "message": f"相对10:00走弱 {h['vs_midday_pct']:+.1f}%",
                }
            )

    quant = ic.run_quant_flat(holdings)
    constraints = ic.check_constraints_intraday(holdings, cash, total, quant, alerts, True)
    candidates = ic.load_candidates_top(5)
    recommendation = ic.recommend_from(constraints, alerts)

    out = {
        "generated_at": datetime.now().isoformat(),
        "flash_context_at": flash.get("generated_at"),
        "midday_context_at": midday.get("generated_at"),
        "holdings": holdings,
        "cash": round(cash, 2),
        "total_assets": round(total, 2),
        "alerts": alerts,
        "constraints": constraints,
        "quant_per_stock": quant,
        "candidates": candidates,
        "recommendation": recommendation,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    out = ic.apply_macro_risk(out, slot="noon", scan_news=True)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if "--save" in sys.argv:
        idx = sys.argv.index("--save")
        path = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else OUT_DEFAULT
        ic.save_stdout_main(__file__, path)
    else:
        main()

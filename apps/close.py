#!/config/quant_env/bin/python3
"""
apps/close.py — 15:05 收盘总结（代码管线）

在 signal_verify_report 等前置脚本之后由 close_app 调用；本文件只产出结构化 JSON。
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))
from datetime import datetime

import intraday_common as ic

OUT_DEFAULT = os.path.join(os.environ.get("QUANT_RUNTIME_DATA_DIR") or "/config/quant_scripts/data", "close_output.json")


def main():
    t0 = time.time()
    flash = ic.load_json(ic.FLASH_JSON)
    midday = ic.load_json(ic.MIDDAY_JSON)

    holdings, cash, total = ic.load_holdings_and_quotes()
    holdings = ic.merge_flash_context(holdings, flash)
    holdings = ic.merge_prior_snapshot(holdings, midday, "midday")

    alerts_intraday = ic.detect_alerts_intraday(holdings, pullback_alert=True)
    alerts_close = ic.detect_close_alerts(holdings)
    alerts = alerts_intraday + alerts_close

    quant = ic.run_quant_flat(holdings)
    constraints = ic.check_constraints_intraday(holdings, cash, total, quant, alerts, True)
    candidates = ic.load_candidates_top(5)
    recommendation = ic.recommend_from(
        constraints,
        alerts,
        caution_types={"REVERSAL_DOWN", "SHARP_DECLINE", "EOD_LARGE_DROP", "EOD_WIDE_RANGE"},
    )
    pnl_summary = ic.pnl_summary_from_holdings(holdings, cash=cash, total_assets=total)

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
        "pnl_summary": pnl_summary,
        "recommendation": recommendation,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    out = ic.apply_macro_risk(out, slot="close", scan_news=True)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if "--save" in sys.argv:
        idx = sys.argv.index("--save")
        path = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else OUT_DEFAULT
        ic.save_stdout_main(__file__, path)
    else:
        main()

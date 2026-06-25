#!python3
"""
strategy_optimizer.py — 策略优化报告
由夜报/周末 cron 自动调用，生成结构化优化建议供 Hermes agent 评估和执行。
"""
import json, os, sys
from datetime import datetime
from typing import Any, Dict, List, Optional
from system_config import cfg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
REPORT_PATH = os.path.join(DATA, "optimization_report.json")


def build_optimization_report() -> Dict[str, Any]:
    """Generate a structured optimization report for Hermes agent consumption."""

    # ── 1. Strategy validation ──
    from strategy_validation import summarize_validation
    vs = summarize_validation() or {}

    # ── 2. Current deployment config ──
    tiers_path = os.path.join(DATA, "deployment_tiers.json")
    tiers = {}
    if os.path.isfile(tiers_path):
        tiers = json.load(open(tiers_path, encoding="utf-8"))

    # ── 3. Feature snapshot (quant health) ──
    fs = {}
    fs_path = os.path.join(DATA, "feature_snapshot.json")
    if os.path.isfile(fs_path):
        with open(fs_path, encoding="utf-8") as f:
            fs = json.load(f)

    # ── 4. Current blocked candidates from morning plan ──
    blocked_details = []
    mp_path = os.path.join(DATA, "morning_output.json")
    if os.path.isfile(mp_path):
        with open(mp_path, encoding="utf-8") as f:
            mp = json.load(f)
        dp = mp.get("deployment_plan") or {}
        for b in dp.get("candidates_blocked") or []:
            blocked_details.append({
                "code": b.get("code", ""),
                "reasons": b.get("reasons", []),
                "score": b.get("score", 0),
            })

    # ── 5. Build actionable recommendations ──
    recommendations = []

    # R1: CVaR threshold — if blocked_positive > 0, suggest relaxing
    blocked_positive = vs.get("blocked_positive_count", 0)
    blocked_codes = vs.get("blocked_positive_codes", [])
    if blocked_positive > 0:
        recommendations.append({
            "id": "relax_cvar",
            "priority": "high",
            "reason": f"{blocked_positive} blocked candidates ({', '.join(blocked_codes)}) "
                      f"had positive next-day returns. Current CVaR threshold may be too strict.",
            "action": "modify deployment_tiers.json tiers.<LEVEL>.cvar_floor",
            "suggested_change": {
                "field": "tiers.<LEVEL>.cvar_floor",
                "from": (tiers.get("tiers", {}).get("HIGH", {}).get("cvar_floor", -10)),
                "to": "(relax by 2-3 points, e.g. -12 or -13)",
                "note": "Only relax for non-danger risk_level stocks",
            },
            "config_file": cfg.path.deployment_tiers,
        })

    # R2: Score threshold — if hit_rate < 50%, suggest raising floor
    hit_rate = vs.get("hit_rate", 0.5)
    if hit_rate and hit_rate < 0.45:
        recommendations.append({
            "id": "raise_score_floor",
            "priority": "medium",
            "reason": f"Candidate hit rate is {hit_rate*100:.0f}% (<45%). "
                      f"Consider raising score_floor to improve quality.",
            "action": "modify deployment_tiers.json tiers.<LEVEL>.score_floor",
            "config_file": cfg.path.deployment_tiers,
        })
    elif hit_rate and hit_rate > 0.55:
        recommendations.append({
            "id": "maintain_or_lower_score",
            "priority": "low",
            "reason": f"Candidate hit rate is {hit_rate*100:.0f}% (>55%). "
                      f"Score floor is working well — consider lowering to increase volume.",
            "action": "modify deployment_tiers.json tiers.<LEVEL>.score_floor",
            "config_file": cfg.path.deployment_tiers,
        })

    # R3: Quant engine gaps
    qe = fs.get("quant_engines") or {}
    garch_cov = qe.get("garch", {}).get("coverage", 0)
    if garch_cov and garch_cov < 3:
        recommendations.append({
            "id": "garch_coverage_low",
            "priority": "medium",
            "reason": f"GARCH coverage is {garch_cov} stocks (need >=60 daily bars). "
                      f"Most candidates lack volatility regime data.",
            "action": "Consider using ann_vol from screener as GARCH fallback.",
        })

    # R4: Position sizing — if blocked candidates all failed on score, suggest tier adjustment
    score_blocks = [b for b in blocked_details if any("score" in r for r in b.get("reasons", []))]
    if score_blocks and hit_rate and hit_rate > 0.5:
        recommendations.append({
            "id": "consider_lower_score_for_high_cvar",
            "priority": "medium",
            "reason": f"{len(score_blocks)} candidates blocked by score threshold, "
                      f"but overall hit rate is {hit_rate*100:.0f}%.",
            "action": "Relax score_floor for candidates with cvar > -5 or risk_level=safe/low.",
            "config_file": cfg.path.deployment_tiers,
        })

    # R5: Concentration — if any position > 20%, suggest de-risk
    positions = mp.get("holdings") or []
    over_20 = []
    total = mp.get("total_assets", 1)
    for h in positions:
        mv = h.get("market_value", h.get("price", 0) * h.get("shares", 0))
        if mv / total > 0.20:
            over_20.append(h.get("code"))
    if over_20:
        recommendations.append({
            "id": "concentration_alert",
            "priority": "high",
            "reason": f"Position concentration >20% for: {', '.join(over_20)}. "
                      f"Consider reducing position or adjusting max_single in deployment tier.",
            "action": "If continuing to hold: raise max_single in deployment tier. "
                      "If reducing: generate SELL via agent_desk de_risk plan.",
            "config_file": cfg.path.deployment_tiers,
        })

    report = {
        "generated_at": datetime.now().isoformat(),
        "phase": "optimization_report",
        "instructions": (
            "Hermes agent: read this report and evaluate the recommendations. "
            "To apply a recommendation, modify the config_file (JSON) using bash/python. "
            "Example: python3 -c \"import json; d=json.load(open(...)); "
            "d['tiers']['HIGH']['cvar_floor']=-12; json.dump(d, open(...,'w'))\" "
            "Then confirm the change in your response."
        ),
        "summary": {
            "candidate_count": vs.get("candidate_count", 0),
            "measured_count": vs.get("measured_count", 0),
            "hit_rate": hit_rate,
            "avg_return_pct": vs.get("avg_return_pct_close", 0),
            "blocked_positive_count": blocked_positive,
            "blocked_positive_codes": blocked_codes,
        },
        "current_config": {
            "deployment_tiers": tiers,
            "quant_coverage": {
                "cvar_stocks": qe.get("cvar", {}).get("coverage", 0),
                "garch_stocks": garch_cov,
                "market_regime": qe.get("market_regime", {}).get("current_state", "unknown"),
            },
        },
        "blocked_candidates_today": blocked_details,
        "recommendations": recommendations,
    }

    os.makedirs(DATA, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


def main():
    report = build_optimization_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/config/quant_env/bin/python3
"""
signal_dashboard.py - Signal quality dashboard for cron integration
Usage: python signal_dashboard.py [--days 30]
"""
import sys, json, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from trade_db import SignalLog

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30)
    args = p.parse_args()

    slog = SignalLog()
    stats = slog.stats_by_source(days=args.days)
    pending = len(slog.get_pending())
    active = len(slog.get_active())

    # Get recent passed/failed details
    recent = slog.query(status="passed", limit=5) + slog.query(status="failed", limit=5)
    recent = sorted(recent, key=lambda x: x.get("verified_at", ""), reverse=True)[:10]

    lines = []

    if not stats:
        lines.append("No verified signals yet (need 5+ simulation days)")
    else:
        lines.append("Source | Signals | Accuracy | Avg PnL | Avg Score")
        lines.append("-" * 55)
        for source, st in sorted(stats.items()):
            lines.append(
                f"{source:15s} | {st['total']:4d}     | "
                f"{st['accuracy']:5.1f}%   | "
                f"{st['avg_pnl_pct']:+6.1f}% | "
                f"{st['avg_score']:5.0f}"
            )

    lines.append("")
    lines.append(f"Pending: {pending} | Active: {active}")
    total_verified = sum(s["total"] for s in stats.values())
    total_passed = sum(s["passed"] for s in stats.values())
    if total_verified > 0:
        lines.append(f"Overall: {total_passed}/{total_verified} passed "
                     f"({total_passed/total_verified*100:.0f}%)")

    if recent:
        lines.append("")
        lines.append("Recent verifications:")
        for r in recent[:5]:
            st_icon = "PASS" if r["status"] == "passed" else "FAIL"
            lines.append(
                f"  [{st_icon}] {r['name']}({r['code']}) from {r['source']} "
                f"score={r['score']:.0f} PnL={r.get('sim_pnl_pct',0):+.1f}%"
            )

    print("\n".join(lines))

if __name__ == "__main__":
    main()

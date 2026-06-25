#!python3
"""
signal_executor.py - Signal Engine + Simulation Lifecycle Management
===================================================================
Phase 1 Core: Convert analysis signals to trackable simulated trades.

Usage:
  python signal_executor.py generate --source qlib
  python signal_executor.py activate
  python signal_executor.py verify
  python signal_executor.py report
  python signal_executor.py full
"""

import sys, json, argparse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from trade_db import SignalLog, MarketSnapshot
from system_config import cfg

SCREENING_PATH = "/config/qlib_data/screening/screening_result.json"

def from_qlib_screening():
    if not Path(SCREENING_PATH).exists():
        print("[SKIP] screening_result.json not found", file=sys.stderr)
        return []
    screening = json.load(open(SCREENING_PATH))
    slog = SignalLog()
    signals = []
    for stock in screening:
        code = stock["code"]
        name = stock.get("name", code)
        score = stock.get("composite_score", 0)
        grade = stock.get("grade", "")
        close = stock.get("close", 0)
        factors = stock.get("factors", {})
        if grade == "值得操作" and score >= 75:
            confidence = min(0.95, score / 100)
            target = close * 1.10
            stop = close * 0.93
            key_factors = {k: v for k, v in factors.items()
                          if k in ["ret_5d","ret_20d","volatility_20d",
                                    "trend_score","ma_dev_5","ma_dev_20",
                                    "vol_ratio_20","price_pos_20"]}
            sid = slog.create(
                source="qlib", code=code, name=name,
                signal_type="BUY", score=score, confidence=confidence,
                target_price=round(target, 2), stop_loss=round(stop, 2),
                reason=f"Qlib score {score:.0f}, {grade}",
                detail={"factors": key_factors}
            )
            signals.append({"id": sid, "code": code, "name": name, "type": "BUY", "score": score})
        elif grade == "建议剔除" and 0 < score < 55:
            confidence = min(0.90, (55 - score) / 55)
            sid = slog.create(
                source="qlib", code=code, name=name,
                signal_type="SELL", score=score, confidence=confidence,
                target_price=round(close, 2),
                reason=f"Qlib score {score:.0f}, {grade}",
                detail={}
            )
            signals.append({"id": sid, "code": code, "name": name, "type": "SELL", "score": score})
    print(f"[OK] Qlib generated {len(signals)} signals", file=sys.stderr)
    return signals

def activate_signals():
    snap = MarketSnapshot()
    slog = SignalLog()
    pending = slog.get_pending()
    activated = 0
    for sig in pending:
        q = snap.get(sig["code"])
        if not q:
            continue
        price = q.get("p", 0)
        if price <= 0:
            continue
        if sig["signal_type"] == "BUY":
            if sig.get("target_price") and price >= sig["target_price"]:
                continue
        slog.update_status(
            sig["id"], "active",
            sim_entry_price=price,
            sim_entry_date=datetime.now().strftime("%Y-%m-%d")
        )
        activated += 1
    print(f"[OK] Activated {activated}/{len(pending)} signals", file=sys.stderr)
    return activated

def verify_signals(min_days=5):
    """
    除了现有的简单验证，还能自动调用高级回测管线 (signal_backtester.py)
    """
    import subprocess
    print("[INFO] 启动信号状态转换及简单验证...", file=sys.stderr)
    snap = MarketSnapshot()
    slog = SignalLog()
    active = slog.get_active()
    verified = 0
    for sig in active:
        q = snap.get(sig["code"])
        if not q:
            continue
        entry_price = sig.get("sim_entry_price", 0)
        current_price = q.get("p", 0)
        if entry_price <= 0 or current_price <= 0:
            continue
        entry_date = sig.get("sim_entry_date", "")
        days_held = 0
        if entry_date:
            days_held = (datetime.now() - datetime.strptime(entry_date, "%Y-%m-%d")).days
            if days_held < min_days:
                continue
        if sig["signal_type"] == "BUY":
            pnl_pct = (current_price - entry_price) / entry_price * 100
        else:
            pnl_pct = (entry_price - current_price) / entry_price * 100
        result = "passed" if pnl_pct > 0 else "failed"
        bt = {
            "entry_price": entry_price, "exit_price": current_price,
            "pnl_pct": round(pnl_pct, 2), "days_held": days_held,
            "verified_at": datetime.now().isoformat()
        }
        slog.update_status(
            sig["id"], result,
            sim_exit_price=current_price,
            sim_exit_date=datetime.now().strftime("%Y-%m-%d"),
            sim_pnl=round(pnl_pct, 2),
            sim_pnl_pct=round(pnl_pct, 2),
            backtest_result=bt,
            verified_at=datetime.now().isoformat()
        )
        verified += 1
    print(f"[OK] Verified {verified}/{len(active)} active signals", file=sys.stderr)
    
    print("[INFO] 调用系统完整级回测管线处理 pending 信号...", file=sys.stderr)
    subprocess.run([sys.executable, cfg.root + "/signal_backtester.py"])

    return verified

def generate_report():
    slog = SignalLog()
    stats = slog.stats_by_source(days=30)
    pending = len(slog.get_pending())
    active = len(slog.get_active())
    lines = []
    if not stats:
        lines.append("No backtest data yet (need 5+ days)")
        lines.append(f"Pending: {pending} | Active: {active}")
        return "\\n".join(lines)
    for source, st in sorted(stats.items()):
        e = "OK" if st["accuracy"] >= 60 else ("WARN" if st["accuracy"] >= 40 else "BAD")
        lines.append(f"[{e}] {source}: {st['total']} signals | "
                     f"accuracy {st['accuracy']}% | avg PnL {st['avg_pnl_pct']:+.1f}% | "
                     f"avg score {st['avg_score']:.0f}")
    lines.append(f"Pending: {pending} | Active: {active}")
    return "\\n".join(lines)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["generate","activate","verify","report","expire","full"])
    p.add_argument("--min-days", type=int, default=5)
    args = p.parse_args()
    if args.action == "generate":
        sigs = from_qlib_screening()
        for s in sigs:
            print(f"  {s['type']} {s['name']}({s['code']}) score={s['score']:.0f}")
    elif args.action == "activate":
        print(f"Activated {activate_signals()} signals")
    elif args.action == "verify":
        print(f"Verified {verify_signals(args.min_days)} signals")
    elif args.action == "report":
        print(generate_report())
    elif args.action == "expire":
        SignalLog().expire_old_pending(14)
        print("Expired old signals cleaned")
    elif args.action == "full":
        from_qlib_screening()
        activate_signals()
        verify_signals(args.min_days)
        print(generate_report())

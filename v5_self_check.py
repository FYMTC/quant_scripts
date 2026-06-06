#!/config/quant_env/bin/python3
"""
v5 系统自检 — 单元测试 + 静态契约检查（每晚 Review / 组件审计可调用）

用法:
  /config/quant_env/bin/python3 /config/quant_scripts/v5_self_check.py
  /config/quant_env/bin/python3 /config/quant_scripts/v5_self_check.py --json

退出码: 0 全通过，1 存在失败
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from typing import Any, Dict, List

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_PY = "/config/quant_env/bin/python3"
DATA = os.path.join(ROOT, "data")
REPORT_PATH = os.path.join(DATA, "v5_self_check_last.json")
SELF_CHECK_NOTIFY_MODE = "record-only"
NOISE_TAIL_LIMIT = 4000


def _capture_check_output(fn, *args, **kwargs) -> Dict[str, Any]:
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        result = fn(*args, **kwargs)
    if not isinstance(result, dict):
        result = {"ok": False, "error": "check did not return a dict"}
    stdout_text = stdout_buf.getvalue()
    stderr_text = stderr_buf.getvalue()
    if stdout_text:
        result["captured_stdout"] = stdout_text[-NOISE_TAIL_LIMIT:]
    if stderr_text:
        result["captured_stderr"] = stderr_text[-NOISE_TAIL_LIMIT:]
    return result


def _run_cycle_suite(suite: str) -> Dict[str, Any]:
    runner_path = os.path.join(ROOT, "test_cycle_runner.py")
    env = os.environ.copy()
    env["QUANT_NOTIFY_MODE"] = SELF_CHECK_NOTIFY_MODE
    proc = subprocess.run(
        [VENV_PY, runner_path, "--suite", suite],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
    )
    return {
        "ok": proc.returncode == 0,
        "suite": suite,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-4000:],
        "stderr": (proc.stderr or "")[-4000:],
        "notify_mode": SELF_CHECK_NOTIFY_MODE,
    }


def _run_unittest_suite() -> Dict[str, Any]:
    state_path = os.path.join(DATA, "trade_accounts_state.json")
    original_state_text = None
    if os.path.isfile(state_path):
        with open(state_path, encoding="utf-8") as f:
            original_state_text = f.read()
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for mod in (
        "tests.test_constraints",
        "tests.test_agent_queue",
        "tests.test_trade_outbox",
        "tests.test_trade_accounts",
        "tests.test_guard_bind_hotload",
        "tests.test_stock_kb_account",
        "tests.test_v5_contracts",
        "tests.test_decision_gate",
        "tests.test_signal_loop",
        "tests.test_agent_desk",
        "tests.test_agent_desk_poll",
        "tests.test_data_refresh_alert",
        "tests.test_digest",
        # stock_kb portfolio tests may rewrite guard exports during setup
        "tests.test_stock_kb_portfolio",
        "tests.test_signal_lineage",
        "tests.test_event_calendar",
        "tests.test_smart_guard_v3",
    ):
        try:
            suite.addTests(loader.loadTestsFromName(mod))
        except Exception as e:
            return {"ok": False, "error": f"load {mod}: {e}"}
    try:
        runner = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w"))
        result = runner.run(suite)
    finally:
        if original_state_text is not None:
            with open(state_path, "w", encoding="utf-8") as f:
                f.write(original_state_text)
    return {
        "ok": result.wasSuccessful(),
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "failure_names": [str(f[0]) for f in result.failures + result.errors],
    }


def _extract_json_object(raw: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[idx:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return {}


def _check_paths() -> Dict[str, Any]:
    required = [
        os.path.join(ROOT, "core", "constraints.py"),
        os.path.join(ROOT, "agent_queue.py"),
        os.path.join(ROOT, "agent_desk.py"),
        os.path.join(ROOT, "trade_outbox.py"),
        os.path.join(ROOT, "signal_loop.py"),
        os.path.join(ROOT, "trade_log.db"),
        os.path.join(DATA, "quant_registry.yaml"),
        os.path.join(ROOT, "core", "engines", "event_calendar.py"),
        os.path.join(ROOT, "core", "engines", "signal_lineage.py"),
        os.path.join(DATA, "event_risk_keywords.yaml"),
        os.path.join(ROOT, "..", ".hermes", "scripts", "agent_desk_app.py"),
        os.path.join(ROOT, "..", ".hermes", "scripts", "agent_desk_poll_app.py"),
        os.path.join(ROOT, "agent_desk_config.py"),
        os.path.join(ROOT, "trade_accounts.py"),
        os.path.join(ROOT, "trade_execution.py"),
        os.path.join(ROOT, "trade_notify.py"),
        os.path.join(DATA, "trade_accounts.yaml"),
        os.path.join(DATA, "trade_accounts_state.json"),
        os.path.join(ROOT, "trade_account_context.py"),
        os.path.join(ROOT, "guard_account_bind.py"),
        os.path.join(ROOT, "..", ".hermes", "scripts", "morning_plan_app.py"),
        os.path.join(ROOT, "..", ".hermes", "scripts", "review_app.py"),
    ]
    missing = [p for p in required if not os.path.isfile(p)]
    return {"ok": len(missing) == 0, "missing": missing}


def _check_jobs_deliver() -> Dict[str, Any]:
    jobs_path = os.path.join(ROOT, "..", ".hermes", "cron", "jobs.json")
    if not os.path.isfile(jobs_path):
        return {"ok": True, "skipped": "jobs.json not found"}
    try:
        with open(jobs_path, encoding="utf-8") as f:
            jobs = json.load(f).get("jobs", [])
    except Exception as e:
        return {"ok": False, "error": str(e)}
    weixin_trading = []
    local_refresh_ids = {
        "38a1c0401a1d", "718bad2ea1fe", "81c08b8f2cbe",
        "6907661c0a15", "1af47883139e",
    }
    for j in jobs:
        jid = j.get("id", "")
        name = j.get("name", "")
        deliver = j.get("deliver", "")
        if jid in local_refresh_ids and deliver != "local":
            weixin_trading.append(jid)
        if "08:30" in name or jid == "5a69c039950e":
            if deliver.startswith("weixin"):
                weixin_trading.append(jid + "(morning)")
    desk_poll = next((j for j in jobs if j.get("id") == "76ef0dd15954"), None)
    desk_llm = next((j for j in jobs if j.get("id") == "a7f3e81d9llm"), None)
    desk_ok = (
        desk_poll is not None
        and desk_poll.get("no_agent") is True
        and desk_poll.get("skills") == []
        and desk_llm is not None
        and desk_llm.get("skills") == ["trading-decision-gate"]
    )
    refresh_bad = []
    for j in jobs:
        jid = j.get("id", "")
        if jid in local_refresh_ids:
            if j.get("no_agent") is not True or j.get("skills"):
                refresh_bad.append(jid)
    refresh_ok = len(refresh_bad) == 0
    return {
        "ok": len(weixin_trading) == 0 and desk_ok and refresh_ok,
        "weixin_on_silent_jobs": weixin_trading,
        "desk_dual_job": desk_ok,
        "refresh_no_agent": refresh_ok,
        "refresh_not_no_agent": refresh_bad,
    }


def _smoke_agent_desk_empty_queue() -> Dict[str, Any]:
    try:
        state_path = os.path.join(DATA, "agent_state.json")
        original_text = None
        if os.path.isfile(state_path):
            with open(state_path, encoding="utf-8") as f:
                original_text = f.read()
        sys.path.insert(0, ROOT)
        from agent_desk import process_pending

        out = process_pending(max_events=1)
        return {"ok": True, "needs_hermes": out.get("needs_hermes")}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    finally:
        if original_text is not None:
            with open(state_path, "w", encoding="utf-8") as f:
                f.write(original_text)


def _check_guard_runtime_contract() -> Dict[str, Any]:
    guard_path = os.path.join(ROOT, "guard_config.json")
    state_path = os.path.join(ROOT, "guard_state.json")
    heartbeat_path = os.path.join(ROOT, "guard_heartbeat.txt")

    if not os.path.isfile(guard_path):
        return {"ok": False, "error": "guard_config.json missing"}

    with open(guard_path, encoding="utf-8") as f:
        cfg = json.load(f)

    monitored = cfg.get("monitored_codes") or {}
    watch_list = cfg.get("watch_list") or {}
    signals = cfg.get("signals") or []
    runtime_hollow = not monitored and not watch_list
    degraded = bool(monitored) and not bool(cfg.get("watch_list"))

    state_data = {}
    if os.path.isfile(state_path):
        try:
            with open(state_path, encoding="utf-8") as f:
                state_data = json.load(f)
        except Exception as e:
            return {"ok": False, "error": f"guard_state unreadable: {e}"}

    blindness = state_data.get("runtime_blindness") or {}
    price_history = state_data.get("price_history") or {}
    heartbeat_status = None
    heartbeat_marked_sleep = None
    heartbeat_age_sec = None
    if os.path.isfile(heartbeat_path):
        heartbeat_age_sec = round(datetime.now().timestamp() - os.path.getmtime(heartbeat_path), 1)
        try:
            with open(heartbeat_path, encoding="utf-8") as f:
                heartbeat_text = f.read().strip()
            parts = heartbeat_text.split("|") if heartbeat_text else []
            heartbeat_status = parts[1] if len(parts) > 1 else None
            heartbeat_marked_sleep = any(p.startswith("sleep=") for p in parts)
        except Exception:
            heartbeat_status = None
            heartbeat_marked_sleep = None

    reasons = []
    if runtime_hollow:
        reasons.append("guard_config 缺少 monitored_codes/watch_list")
    if degraded:
        reasons.append("watch_list 缺失，仅剩 monitored_codes 导出视图")
    if not signals and not os.environ.get("STOCK_KB_GUARD_CONFIG_PATH"):
        reasons.append("signals 为空")
    if not price_history:
        reasons.append("price_history 为空")
    if blindness.get("status") == "blind":
        critical_reasons = blindness.get("critical_reasons") or []
        if critical_reasons:
            reasons.append("state.runtime_blindness 标记为 blind")
    if heartbeat_age_sec is not None and heartbeat_age_sec > 600:
        allow_idle_sleep = heartbeat_status == "idle" and heartbeat_marked_sleep
        if allow_idle_sleep:
            pass
        elif (blindness.get("status") == "healthy" and (blindness.get("warning_reasons") or [])) or any(
            "heartbeat 超过 600s 未更新" in r for r in (blindness.get("reasons") or [])
        ):
            pass
        else:
            reasons.append(f"heartbeat 超过 600s 未更新 ({heartbeat_age_sec}s)")

    return {
        "ok": len(reasons) == 0,
        "reasons": reasons,
        "monitored_codes": len(monitored),
        "watch_list": len(watch_list),
        "signals": len(signals),
        "price_history_codes": len(price_history),
        "heartbeat_age_sec": heartbeat_age_sec,
        "heartbeat_status": heartbeat_status,
        "runtime_blindness": blindness,
    }


def _check_feature_snapshot_contract() -> Dict[str, Any]:
    path = os.path.join(DATA, "feature_snapshot.json")
    if not os.path.isfile(path):
        return {"ok": False, "error": "feature_snapshot.json missing"}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    runtime_flags = data.get("runtime_flags") or {}
    portfolio = data.get("portfolio") or {}
    per_stock = data.get("per_stock") or {}
    reasons = []
    if not data.get("generated_at"):
        reasons.append("generated_at missing")
    if not isinstance(per_stock, dict):
        reasons.append("per_stock not dict")
    if not isinstance(portfolio, dict):
        reasons.append("portfolio not dict")
    if "feature_fresh" not in runtime_flags:
        reasons.append("runtime_flags.feature_fresh missing")
    return {"ok": len(reasons) == 0, "reasons": reasons, "per_stock_count": len(per_stock)}


def _check_runtime_research_consumption() -> Dict[str, Any]:
    plan_path = os.path.join(DATA, "plan_bundle.json")
    guard_path = os.path.join(ROOT, "guard_config.json")
    reasons = []
    if not os.path.isfile(plan_path):
        reasons.append("plan_bundle.json missing")
        return {"ok": False, "reasons": reasons}
    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)
    if "feature_snapshot" not in plan:
        reasons.append("plan_bundle 缺少 feature_snapshot")
    signal_auto_generate = plan.get("signal_auto_generate") or {}
    if not signal_auto_generate.get("feature_snapshot_used"):
        reasons.append("signal_auto_generate 未声明使用 feature_snapshot")
    strategy_validation_record = plan.get("strategy_validation_record") or {}
    if not strategy_validation_record.get("ok"):
        reasons.append("plan_bundle 缺少 strategy_validation_record.ok")
    if os.path.isfile(guard_path):
        with open(guard_path, encoding="utf-8") as f:
            cfg = json.load(f)
        auto = [s for s in (cfg.get("signals") or []) if s.get("auto_generated")]
        if auto and not any(isinstance(s.get("evidence"), dict) for s in auto):
            reasons.append("auto_generated signals 缺少 evidence")
    return {"ok": len(reasons) == 0, "reasons": reasons}


def _check_omnidata_health() -> Dict[str, Any]:
    import urllib.request, urllib.error
    try:
        req = urllib.request.Request("http://localhost:8380/health")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        if data.get("success") and data.get("data", {}).get("status") == "healthy":
            return {"ok": True, "reasons": [], "uptime_seconds": data["data"].get("uptime_seconds")}
        return {"ok": False, "reasons": [f"omnidata unhealthy: {data.get('message','?')}"]}
    except Exception as e:
        return {"ok": False, "reasons": [f"omnidata unreachable: {str(e)[:200]}"]}


def _check_stock_kb_hygiene() -> Dict[str, Any]:
    """Cross-check stock_kb against live portfolio truth.

    Reports drift in both directions:
      - stock_kb 持有 N 股但 portfolio 中没有（或更少）→ 旧 leftover
      - portfolio 持有 N 股但 stock_kb 没有/更少 → 漏记
    仅看 stock_kb.current_shares>0 会把活持仓误报为 leftover。
    """
    import sqlite3
    reasons: List[str] = []
    db_path = os.path.join(ROOT, "trade_log.db")
    try:
        sys.path.insert(0, ROOT)
        # 部分 tests.test_* 在模块顶部用 sys.modules.setdefault('yaml', ...)
        # stub 掉 yaml.safe_load。unittest 跑完后该 stub 仍在污染 sys.modules，
        # 会让 load_registry() 拿到空字典、get_account() 抛 unknown account_id。
        # 显式清除 stub + 重新绑定 trade_accounts.yaml（其 import 早已固化引用），
        # 确保 load_portfolio_truth() 走真实 yaml。
        if "yaml" in sys.modules and not hasattr(sys.modules["yaml"], "SafeLoader"):
            del sys.modules["yaml"]
        import yaml as _yaml
        import trade_accounts as _ta
        _ta.yaml = _yaml
        from trade_account_context import load_portfolio_truth
        # debug
        import sys as _sys
        _sys.stderr.write(f"DEBUG load_registry result: {_ta.load_registry()}\n")
        _sys.stderr.write(f"DEBUG load_registry __name__: {_ta.load_registry.__name__}\n")
        try:
            _sys.stderr.write(f"DEBUG ta.get_account('paper_easyths') = {_ta.get_account('paper_easyths')}\n")
        except Exception as e:
            _sys.stderr.write(f"DEBUG ta.get_account RAISED: {e}\n")

        portfolio = load_portfolio_truth() or {}
        live_positions = portfolio.get("positions") or {}
        live_by_code: Dict[str, Dict[str, Any]] = {}
        for code, info in live_positions.items():
            if not isinstance(info, dict):
                continue
            shares = int(info.get("shares") or 0)
            if shares <= 0:
                continue
            live_by_code[str(code)] = {
                "shares": shares,
                "cost": float(info.get("cost") or 0.0),
                "name": str(info.get("name") or ""),
            }

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        kb_rows = conn.execute(
            "SELECT code, name, current_shares, avg_cost FROM stock_kb"
        ).fetchall()
        conn.close()
        kb_by_code: Dict[str, Dict[str, Any]] = {}
        for r in kb_rows:
            kb_by_code[str(r["code"])] = {
                "shares": int(r["current_shares"] or 0),
                "cost": float(r["avg_cost"] or 0.0),
                "name": str(r["name"] or ""),
            }

        for code, kb in kb_by_code.items():
            if kb["shares"] <= 0:
                continue
            live = live_by_code.get(code)
            if not live:
                reasons.append(
                    f"{code}({kb['name'] or '?'}): stock_kb={kb['shares']}股 但 portfolio 中已无持仓"
                )
                continue
            if kb["shares"] > live["shares"]:
                reasons.append(
                    f"{code}({kb['name'] or live['name']}): stock_kb={kb['shares']}股 > portfolio={live['shares']}股，多记 {kb['shares'] - live['shares']}股"
                )

        for code, live in live_by_code.items():
            kb = kb_by_code.get(code)
            kb_shares = kb["shares"] if kb else 0
            if kb_shares < live["shares"]:
                reasons.append(
                    f"{code}({live['name']}): portfolio={live['shares']}股 但 stock_kb={kb_shares}股，少记 {live['shares'] - kb_shares}股"
                )

        if reasons:
            return {"ok": False, "reasons": reasons}
        return {"ok": True, "reasons": []}
    except Exception as e:
        return {"ok": False, "reasons": [f"stock_kb check failed: {str(e)[:200]}"]}


def _check_quant_engine_coverage() -> Dict[str, Any]:
    """Check that feature_snapshot has quant_engines section with minimum coverage."""
    snapshot_path = os.path.join(DATA, "feature_snapshot.json")
    reasons = []
    try:
        if not os.path.isfile(snapshot_path):
            reasons.append("feature_snapshot.json missing")
            return {"ok": False, "reasons": reasons}
        with open(snapshot_path, encoding="utf-8") as f:
            fs = json.load(f)
        qe = fs.get("quant_engines") or {}
        modules = fs.get("source_modules") or []
        cvar = qe.get("cvar") or {}
        garch = qe.get("garch") or {}
        mr = qe.get("market_regime") or {}

        if len(modules) < 6:
            reasons.append(f"only {len(modules)} source_modules (need >=6)")
        if not cvar.get("coverage"):
            reasons.append("cvar: zero coverage")
        if not mr.get("ok"):
            reasons.append("market_regime: not ok")
        if not fs.get("factor_library"):
            reasons.append("factor_library: not present (RD-Agent not run?)")
        if not reasons:
            return {
                "ok": True, "reasons": [],
                "modules": len(modules),
                "cvar_coverage": cvar.get("coverage", 0),
                "garch_coverage": garch.get("coverage", 0),
                "factor_count": len((fs.get("factor_library") or {}).get("factors", [])),
            }
        return {"ok": False, "reasons": reasons}
    except Exception as e:
        return {"ok": False, "reasons": [str(e)[:200]]}


def _check_primary_account_runtime() -> Dict[str, Any]:
    state_path = os.path.join(DATA, "trade_accounts_state.json")
    try:
        expected_state = None
        if os.path.isfile(state_path):
            with open(state_path, encoding="utf-8") as f:
                expected_state = json.load(f)
        sys.path.insert(0, ROOT)
        from trade_accounts import status_report

        report = status_report(active_only=True)
    except ImportError as e:
        return {"ok": False, "reasons": [f"import error: {e}"], "error": str(e)[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}

    primary = report.get("primary_account") or {}
    reasons = []
    if report.get("runtime_mode") != "single_account_mode":
        reasons.append(f"runtime_mode={report.get('runtime_mode')}")
    if report.get("hermes_trading_active_count") != 1:
        reasons.append(f"active_count={report.get('hermes_trading_active_count')}")
    if not report.get("desk_primary_account"):
        reasons.append("desk_primary_account missing")
    if not primary:
        reasons.append("primary_account missing")
    elif primary.get("account_id") != report.get("desk_primary_account"):
        reasons.append("primary_account mismatch")
    if expected_state:
        expected_active = list(expected_state.get("hermes_trading_active") or [])
        expected_primary = expected_state.get("desk_primary_account")
        if report.get("hermes_trading_active") != expected_active:
            reasons.append(f"state_drift.active={report.get('hermes_trading_active')} expected={expected_active}")
        if report.get("desk_primary_account") != expected_primary:
            reasons.append(
                f"state_drift.primary={report.get('desk_primary_account')} expected={expected_primary}"
            )

    return {
        "ok": len(reasons) == 0,
        "reasons": reasons,
        "runtime_mode": report.get("runtime_mode"),
        "desk_primary_account": report.get("desk_primary_account"),
        "hermes_trading_active_count": report.get("hermes_trading_active_count"),
        "primary_account": primary,
        "hermes_trading_active": report.get("hermes_trading_active") or [],
        "expected_state": expected_state or {},
    }


def run_all(*, include_cycle: bool = False) -> Dict[str, Any]:
    report = {
        "generated_at": datetime.now().isoformat(),
        "phase": "v5_self_check",
        "include_cycle": include_cycle,
        "checks": {},
    }
    report["checks"]["primary_account_runtime"] = _capture_check_output(_check_primary_account_runtime)
    report["checks"]["unittest"] = _capture_check_output(_run_unittest_suite)
    report["checks"]["three_layer_fast"] = _capture_check_output(_run_cycle_suite, "fast")
    if include_cycle:
        report["checks"]["three_layer_cycle"] = _capture_check_output(_run_cycle_suite, "cycle")
    report["checks"]["paths"] = _capture_check_output(_check_paths)
    report["checks"]["jobs_deliver"] = _capture_check_output(_check_jobs_deliver)
    report["checks"]["agent_desk_smoke"] = _capture_check_output(_smoke_agent_desk_empty_queue)
    report["checks"]["guard_runtime_contract"] = _capture_check_output(_check_guard_runtime_contract)
    report["checks"]["feature_snapshot_contract"] = _capture_check_output(_check_feature_snapshot_contract)
    report["checks"]["runtime_research_consumption"] = _capture_check_output(_check_runtime_research_consumption)
    report["checks"]["omnidata_health"] = _capture_check_output(_check_omnidata_health)
    report["checks"]["stock_kb_hygiene"] = _capture_check_output(_check_stock_kb_hygiene)
    report["checks"]["quant_engine_coverage"] = _capture_check_output(_check_quant_engine_coverage)
    report["ok"] = all(c.get("ok") for c in report["checks"].values())
    os.makedirs(DATA, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    report["report_path"] = REPORT_PATH
    return report


def main():
    as_json = "--json" in sys.argv
    include_cycle = "--cycle" in sys.argv
    report = run_all(include_cycle=include_cycle)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        status = "PASS" if report["ok"] else "FAIL"
        print(f"v5_self_check: {status}")
        print(f"  include_cycle: {include_cycle}")
        for name, c in report["checks"].items():
            mark = "ok" if c.get("ok") else "FAIL"
            print(f"  [{mark}] {name}")
            if not c.get("ok") and c.get("missing"):
                print(f"       missing: {c['missing'][:3]}")
            if not c.get("ok") and c.get("failure_names"):
                print(f"       tests: {c['failure_names'][:5]}")
            if not c.get("ok") and c.get("stderr"):
                tail = [line for line in c["stderr"].splitlines() if line.strip()][-1:]
                if tail:
                    print(f"       stderr: {tail[0]}")
        print(f"  report: {REPORT_PATH}")
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()

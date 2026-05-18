#!/config/quant_env/bin/python3
"""
v5 系统自检 — 单元测试 + 静态契约检查（每晚 Review / 组件审计可调用）

用法:
  /config/quant_env/bin/python3 /config/quant_scripts/v5_self_check.py
  /config/quant_env/bin/python3 /config/quant_scripts/v5_self_check.py --json

退出码: 0 全通过，1 存在失败
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import datetime
from typing import Any, Dict, List

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_PY = "/config/quant_env/bin/python3"
DATA = os.path.join(ROOT, "data")
REPORT_PATH = os.path.join(DATA, "v5_self_check_last.json")


def _run_unittest_suite() -> Dict[str, Any]:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for mod in (
        "tests.test_constraints",
        "tests.test_agent_queue",
        "tests.test_trade_outbox",
        "tests.test_v5_contracts",
        "tests.test_decision_gate",
        "tests.test_signal_loop",
        "tests.test_agent_desk",
        "tests.test_agent_desk_poll",
        "tests.test_digest",
        "tests.test_stock_kb_portfolio",
        "tests.test_signal_lineage",
        "tests.test_event_calendar",
    ):
        try:
            suite.addTests(loader.loadTestsFromName(mod))
        except Exception as e:
            return {"ok": False, "error": f"load {mod}: {e}"}
    runner = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w"))
    result = runner.run(suite)
    return {
        "ok": result.wasSuccessful(),
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "failure_names": [str(f[0]) for f in result.failures + result.errors],
    }


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
    return {
        "ok": len(weixin_trading) == 0 and desk_ok,
        "weixin_on_silent_jobs": weixin_trading,
        "desk_dual_job": desk_ok,
    }


def _smoke_agent_desk_empty_queue() -> Dict[str, Any]:
    try:
        sys.path.insert(0, ROOT)
        from agent_desk import process_pending
        out = process_pending(max_events=1)
        return {"ok": True, "needs_hermes": out.get("needs_hermes")}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def run_all() -> Dict[str, Any]:
    report = {
        "generated_at": datetime.now().isoformat(),
        "phase": "v5_self_check",
        "checks": {},
    }
    report["checks"]["unittest"] = _run_unittest_suite()
    report["checks"]["paths"] = _check_paths()
    report["checks"]["jobs_deliver"] = _check_jobs_deliver()
    report["checks"]["agent_desk_smoke"] = _smoke_agent_desk_empty_queue()
    report["ok"] = all(c.get("ok") for c in report["checks"].values())
    os.makedirs(DATA, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    report["report_path"] = REPORT_PATH
    return report


def main():
    as_json = "--json" in sys.argv
    report = run_all()
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        status = "PASS" if report["ok"] else "FAIL"
        print(f"v5_self_check: {status}")
        for name, c in report["checks"].items():
            mark = "ok" if c.get("ok") else "FAIL"
            print(f"  [{mark}] {name}")
            if not c.get("ok") and c.get("missing"):
                print(f"       missing: {c['missing'][:3]}")
            if not c.get("ok") and c.get("failure_names"):
                print(f"       tests: {c['failure_names'][:5]}")
        print(f"  report: {REPORT_PATH}")
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()

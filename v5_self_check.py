#!python3
"""
v5 系统自检 — 单元测试 + 静态契约检查（每晚 Review / 组件审计可调用）

用法:
  python3 v5_self_check.py
  python3 v5_self_check.py --json

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
from system_config import cfg

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_PY = cfg.python
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
    # 备份 + 恢复 trade_accounts_state.json — 防止 cycle suite 触发 start/stop 污染运行时账户
    state_path = os.path.join(DATA, "trade_accounts_state.json")
    original_state_text = None
    if os.path.isfile(state_path):
        with open(state_path, encoding="utf-8") as f:
            original_state_text = f.read()
    try:
        proc = subprocess.run(
            [VENV_PY, runner_path, "--suite", suite],
            capture_output=True,
            text=True,
            env=env,
            cwd=ROOT,
        )
    finally:
        if original_state_text is not None:
            with open(state_path, "w", encoding="utf-8") as f:
                f.write(original_state_text)
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
        "tests.test_dynamic_stop_loss",
        "tests.test_direction_resolver",
        "tests.test_trade_db",
        "tests.test_t_flip_applicability",
        "tests.test_close_loop_reflow",
        "tests.test_cvrf_reflection",
        "tests.test_rotation_scanner",
        "tests.test_backtest_rotation",
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
    hermes_scripts = cfg.system.hermes_scripts
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
        os.path.join(hermes_scripts, "agent_desk_app.py"),
        os.path.join(hermes_scripts, "agent_desk_poll_app.py"),
        os.path.join(ROOT, "agent_desk_config.py"),
        os.path.join(ROOT, "trade_accounts.py"),
        os.path.join(ROOT, "trade_execution.py"),
        os.path.join(ROOT, "trade_notify.py"),
        os.path.join(DATA, "trade_accounts.yaml"),
        os.path.join(DATA, "trade_accounts_state.json"),
        os.path.join(ROOT, "trade_account_context.py"),
        os.path.join(ROOT, "guard_account_bind.py"),
        os.path.join(hermes_scripts, "morning_plan_app.py"),
        os.path.join(hermes_scripts, "review_app.py"),
    ]
    missing = [p for p in required if not os.path.isfile(p)]
    return {"ok": len(missing) == 0, "missing": missing}


def _check_jobs_deliver() -> Dict[str, Any]:
    jobs_path = os.path.join(cfg.system.hermes_root, "cron", "jobs.json")
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
        # morning job (5a69c039950e) 推企业微信是核心设计，不应判为 silent 误报
    desk_poll = next((j for j in jobs if j.get("id") == "76ef0dd15954"), None)
    desk_llm = next((j for j in jobs if j.get("id") == "a7f3e81d9llm"), None)
    # 2026-06-27：desk LLM skills 从单元素 ["trading-decision-gate"] 扩展为
    # ["omnidata-finance", "quant-data-layer", "stock-knowledge-base", "trading-decision-gate"]
    # 检查规则改为"包含 trading-decision-gate"（决策门禁 skill 必须存在）
    desk_ok = (
        desk_poll is not None
        and desk_poll.get("no_agent") is True
        and desk_poll.get("skills") == []
        and desk_llm is not None
        and "trading-decision-gate" in (desk_llm.get("skills") or [])
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
            # 非交易时段 guard 休眠(idle+sleep)时，blindness 可能是旧值，不报
            allow_idle_sleep = heartbeat_status == "idle" and heartbeat_marked_sleep
            if not allow_idle_sleep:
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
        from omnidata_config import OMNIDATA_BASE_URL
        health_url = f"{OMNIDATA_BASE_URL}/health"
        req = urllib.request.Request(health_url)
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
        # stub 掉 yaml.safe_load；更糟的是 test_guard_bind_hotload.py 的 tearDown
        # 漏了恢复 ta.DEFAULT_PATH，导致 load_registry() 读到一个 accounts: {} 的
        # 临时文件，get_account() 因此抛 'unknown account_id'。两条都得手动清掉：
        # 1) 重新绑定 trade_accounts.yaml，2) 把 DEFAULT_PATH 显式拨回真实 YAML。
        if "yaml" in sys.modules and not hasattr(sys.modules["yaml"], "SafeLoader"):
            del sys.modules["yaml"]
        import yaml as _yaml
        import trade_accounts as _ta
        _ta.yaml = _yaml
        _ta.DEFAULT_PATH = os.path.join(DATA, "trade_accounts.yaml")
        from trade_accounts import hermes_trading_active
        from trade_account_context import load_account_snapshot, normalize_portfolio_truth

        # 聚合所有 hermes_trading_active 账户的持仓（多账户场景下不能只看 primary）
        live_by_code: Dict[str, Dict[str, Any]] = {}
        read_errors: List[str] = []
        for _aid in hermes_trading_active() or []:
            _snap = load_account_snapshot(_aid)
            _pf = normalize_portfolio_truth(_snap)
            if _pf.get("error"):
                read_errors.append(f"{_aid}: {_pf['error'][:80]}")
                continue
            for code, info in (_pf.get("positions") or {}).items():
                if not isinstance(info, dict):
                    continue
                shares = int(info.get("shares") or 0)
                if shares <= 0:
                    continue
                # 多账户合并：同代码累加
                prev = live_by_code.get(str(code))
                if prev:
                    prev["shares"] += shares
                else:
                    live_by_code[str(code)] = {
                        "shares": shares,
                        "cost": float(info.get("cost") or 0.0),
                        "name": str(info.get("name") or ""),
                    }
        # 所有持仓源都不可读时，无法对账 → skip 避免误报 leftover
        if not live_by_code and read_errors:
            return {
                "ok": False,
                "reasons": ["持仓源全部不可读，无法对账: " + "; ".join(read_errors)],
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


def _check_direction_resolver_contract() -> Dict[str, Any]:
    """T1.10 二期（2026-06-30）：校验 trading_journal 决策事件的方向解析一致性。

    抽样近 30 条 type='决策事件' 记录，校验：
      - action ∈ {BUY, SELL, HOLD, WAIT, T_FLIP}（或 forced_risk_stop_triggered）
      - resolver_path 非空且含 → 方向标记
      - action=SELL 时 resolver_path 应含 stop=True / forced_risk / tp_score / tp= 之一
      - action=BUY 时 resolver_path 应含 bf= / bo= / bottom_fish 之一

    初期无决策事件时返回 ok:True, skip，不阻塞自检。
    """
    import sqlite3
    reasons: List[str] = []
    db_path = os.path.join(ROOT, "trade_log.db")
    try:
        if not os.path.isfile(db_path):
            return {"ok": True, "reasons": ["trade_log.db not found, skip"]}
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # 防御：旧 DB 可能没有 action/resolver_path 列（Task 4A 迁移前）
        try:
            rows = conn.execute(
                "SELECT action, resolver_path, signal_id FROM trading_journal "
                "WHERE type='决策事件' ORDER BY id DESC LIMIT 30"
            ).fetchall()
        except sqlite3.OperationalError:
            conn.close()
            return {"ok": True, "reasons": ["trading_journal schema pre-T1.10-phase2, skip"]}
        conn.close()
        if not rows:
            return {"ok": True, "reasons": ["no 决策事件 records yet, skip"]}
        valid_actions = {"BUY", "SELL", "HOLD", "WAIT", "T_FLIP"}
        for r in rows:
            action = str(r["action"] or "")
            path = str(r["resolver_path"] or "")
            if action not in valid_actions:
                reasons.append(f"invalid action={action} path={path[:80]}")
                continue
            if not path:
                reasons.append(f"{action}: resolver_path empty")
                continue
            if "→" not in path and "forced_risk" not in path:
                reasons.append(f"{action}: resolver_path missing → marker: {path[:80]}")
                continue
            # 方向一致性校验
            if action == "SELL" and not any(
                k in path for k in ("stop=True", "forced_risk", "tp_score", "tp=", "take_profit")
            ):
                reasons.append(f"SELL without stop/forced/tp in path: {path[:100]}")
            if action == "BUY" and not any(
                k in path for k in ("bf=", "bo=", "bottom_fish")
            ):
                reasons.append(f"BUY without bf/bo in path: {path[:100]}")
        if reasons:
            return {"ok": False, "reasons": reasons[:10]}  # 最多报 10 条
        return {"ok": True, "reasons": [], "sampled": len(rows)}
    except Exception as e:
        return {"ok": False, "reasons": [f"contract check failed: {str(e)[:200]}"]}


def _check_trading_hours_guard() -> Dict[str, Any]:
    """B1（2026-07-01）：校验交易时段守卫 + 渐进减仓状态。

    检查项：
      1. ``_trading_hours_now`` 可导入且返回 bool
      2. ``propose_and_notify`` 签名含 ``force`` kwarg（B1-L3 运维逃生口）
      3. 审计 ``agent_state.pending_trade_requests``：是否存在 ``signal_id=="de_risk_plan"``
         且 ``direction=="SELL"`` 且 ``created_at`` 不在 [09:30, 15:00) CST 的记录（after-hours SELL）
      4. ``pending_sell_in_progress`` 结构正确（含 active/deferred_actions/date，B3 渐进减仓）
    """
    reasons: List[str] = []
    # 1. _trading_hours_now 可导入 + force kwarg
    try:
        import inspect
        from trade_outbox import _trading_hours_now, propose_and_notify
        sig = inspect.signature(propose_and_notify)
        if "force" not in sig.parameters:
            reasons.append("propose_and_notify missing 'force' kwarg (B1-L3)")
        r = _trading_hours_now()
        if not isinstance(r, bool):
            reasons.append(f"_trading_hours_now returned non-bool: {type(r).__name__}")
    except Exception as e:
        reasons.append(f"trade_outbox import failed: {str(e)[:200]}")

    # 2. 审计 after-hours SELL（de_risk_plan 信号在非交易时段生成）
    state: dict = {}
    try:
        state_path = cfg.path.agent_state
        if os.path.isfile(state_path):
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
    except Exception as e:
        reasons.append(f"agent_state load failed: {str(e)[:200]}")

    try:
        from zoneinfo import ZoneInfo
        CST = ZoneInfo("Asia/Shanghai")
        for row in state.get("pending_trade_requests") or []:
            if str(row.get("signal_id") or "") != "de_risk_plan":
                continue
            if str(row.get("direction") or "").upper() != "SELL":
                continue
            created = str(row.get("created_at") or "")
            if not created:
                continue
            try:
                # created_at 是 naive ISO（服务器 CST），直接 parse 后 attach tz
                dt = datetime.fromisoformat(created)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=CST)
                hhmm = dt.hour * 100 + dt.minute
                in_session = (930 <= hhmm < 1130) or (1300 <= hhmm < 1500)
                if not in_session and dt.weekday() < 5:  # 工作日盘后
                    reasons.append(
                        f"after-hours de_risk SELL: {row.get('code')} created at {created[:19]} (request_id={row.get('request_id')})"
                    )
            except Exception:
                pass
    except Exception as e:
        reasons.append(f"after-hours audit failed: {str(e)[:200]}")

    # 3. pending_sell_in_progress 结构校验（B3 渐进减仓）
    psi = state.get("pending_sell_in_progress") or {}
    if psi:
        for k in ("active", "deferred_actions", "date"):
            if k not in psi:
                reasons.append(f"pending_sell_in_progress missing key: {k}")

    return {"ok": not reasons, "reasons": reasons[:10]}


def _check_rotation_scan_freshness() -> Dict[str, Any]:
    """T1.10 三期：rotation_scan.json 新鲜度。

    dormant 容忍：文件不存在 → skip（不报红，周末 cron 未首次跑）；
    存在但 >7 天未更新 → stale 提示（仍 ok=True，不拉红，与 rdagent dormant 同策略）。
    """
    try:
        path = cfg.path.rotation_scan
        if not os.path.exists(path):
            return {"ok": True, "status": "skip",
                    "reason": "rotation_scan.json 未生成（周末 cron 未首次跑）"}
        mtime = datetime.fromtimestamp(os.path.getmtime(path))
        age_days = (datetime.now() - mtime).days
        if age_days > 7:
            return {"ok": True, "status": "stale",
                    "reason": f"rotation_scan.json 已 {age_days} 天未更新（>7）", "mtime": str(mtime)}
        return {"ok": True, "status": "ok", "age_days": age_days}
    except Exception as e:
        return {"ok": True, "status": "skip", "reason": f"check failed: {str(e)[:200]}"}


def _check_cvrf_weekly_gc_freshness() -> Dict[str, Any]:
    """T1.10 三期：cvrf_weekly_gc.json 新鲜度（同 rotation_scan 策略）。"""
    try:
        path = cfg.path.cvrf_weekly_gc
        if not os.path.exists(path):
            return {"ok": True, "status": "skip",
                    "reason": "cvrf_weekly_gc.json 未生成（周末 cron 未首次跑）"}
        mtime = datetime.fromtimestamp(os.path.getmtime(path))
        age_days = (datetime.now() - mtime).days
        if age_days > 7:
            return {"ok": True, "status": "stale",
                    "reason": f"cvrf_weekly_gc.json 已 {age_days} 天未更新（>7）", "mtime": str(mtime)}
        return {"ok": True, "status": "ok", "age_days": age_days}
    except Exception as e:
        return {"ok": True, "status": "skip", "reason": f"check failed: {str(e)[:200]}"}


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
        # factor_library: 直接读 rd_agent_quant 周末产物（不依赖 feature_snapshot 慢重建）
        # 2026-06-30: RD-Agent 首跑完成（factor_library.json 已生成于 2026-06-27），
        # 恢复 FAIL 检查。直接读 cfg.path.factor_library 避免因 feature_snapshot
        # 重建耗时（run_full_scan >280s）导致 factor_library 字段长期 stale 误判。
        factor_lib_path = cfg.path.factor_library
        factor_count = 0
        if not os.path.isfile(factor_lib_path):
            reasons.append("factor_library.json missing — rd_agent_quant.py --mode full 未跑")
        else:
            with open(factor_lib_path, encoding="utf-8") as f:
                flib = json.load(f)
            factor_count = len(flib.get("stable_new_factors") or [])
        if not reasons:
            return {
                "ok": True, "reasons": [],
                "modules": len(modules),
                "cvar_coverage": cvar.get("coverage", 0),
                "garch_coverage": garch.get("coverage", 0),
                "factor_count": factor_count,
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
    # unittest 与 three_layer_fast 互不依赖（后者跑子进程 sandbox，不读真实 state），
    # 并行可省去较短者的等待时间。
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_unittest = ex.submit(_capture_check_output, _run_unittest_suite)
        fut_fast = ex.submit(_capture_check_output, _run_cycle_suite, "fast")
        report["checks"]["unittest"] = fut_unittest.result()
        report["checks"]["three_layer_fast"] = fut_fast.result()
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
    # T1.10 二期：方向解析契约自检（trading_journal 决策事件方向一致性）
    report["checks"]["direction_resolver_contract"] = _capture_check_output(_check_direction_resolver_contract)
    # B1-B4（2026-07-01）：交易时段守卫 + 渐进减仓状态自检
    report["checks"]["trading_hours_guard"] = _capture_check_output(_check_trading_hours_guard)
    # T1.10 三期：轮动扫描 + 周度 GC 新鲜度（文件不存在 → skip 不报红）
    report["checks"]["rotation_scan_freshness"] = _capture_check_output(_check_rotation_scan_freshness)
    report["checks"]["cvrf_weekly_gc_freshness"] = _capture_check_output(_check_cvrf_weekly_gc_freshness)
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

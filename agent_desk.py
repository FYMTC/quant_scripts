#!python3
"""
agent_desk.py — v5 Agent Desk：消费 agent_queue，跑 signal_loop 硬过滤 + 量化上下文。

stdout JSON 供 Hermes 短 prompt 使用：
  needs_hermes: false → 无待分析事件或全部 SKIP，Cron 应静默
  needs_hermes: true  → 含 analyze_tasks，由 Hermes 跑 TradingAgents + decision_gate
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(__file__))

from agent_queue import ack, list_pending, pending_count
from system_config import cfg

RUNTIME_DATA_DIR = cfg.data_dir
PLAYBOOK_DIR = os.path.join(RUNTIME_DATA_DIR, "playbooks")
STATE_PATH = os.path.join(RUNTIME_DATA_DIR, "agent_state.json")
MORNING_OUTPUT_PATH = os.path.join(RUNTIME_DATA_DIR, "morning_output.json")


def _load_json(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_agent_state(patch: dict) -> None:
    state = _load_json(STATE_PATH)
    state["updated_at"] = datetime.now().isoformat()
    state.update(patch)
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def _load_playbook(code: str) -> List[dict]:
    path = os.path.join(PLAYBOOK_DIR, f"{code}.yaml")
    if not os.path.isfile(path):
        return []
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, list) else data.get("patterns", []) if isinstance(data, dict) else []
    except Exception:
        return []


def _run_registry_plugins(code: str, triggers: tuple = ("decide",)) -> List[dict]:
    """P5：对 experimental/production 插件执行 run(ctx)。"""
    reg_path = cfg.path.quant_registry
    if not os.path.isfile(reg_path):
        return []
    try:
        import yaml
        with open(reg_path, encoding="utf-8") as f:
            reg = yaml.safe_load(f) or {}
    except Exception:
        return []
    plugins = reg.get("plugins") or []
    results = []
    ctx_base = {"code": code}
    try:
        from data_converter import fetch_kline_baostock
        from datetime import date
        end = date.today().strftime("%Y%m%d")
        start = date.today().replace(year=date.today().year - 1).strftime("%Y%m%d")
        rec = fetch_kline_baostock(code, start, end)
        ctx_base["closes"] = [float(r["收盘"]) for r in rec] if rec else []
    except Exception:
        ctx_base["closes"] = []

    for pl in plugins:
        if pl.get("status") not in ("production", "experimental"):
            continue
        tr = pl.get("triggers") or []
        if not any(t in tr for t in triggers):
            continue
        mod = pl.get("module", "")
        if ":" not in mod:
            continue
        mod_path, func_name = mod.split(":", 1)
        try:
            import importlib
            m = importlib.import_module(mod_path.replace("/", "."))
            fn = getattr(m, func_name)
            out = fn(ctx_base)
            results.append({"plugin_id": pl.get("id"), "result": out})
        except Exception as e:
            results.append({"plugin_id": pl.get("id"), "error": str(e)[:200]})
    return results


def _fetch_quant_context(code: str) -> dict:
    try:
        from tradingagents_runner import fetch_quant_context
        ctx = fetch_quant_context(code)
        if isinstance(ctx, dict):
            return ctx
        import re
        m = re.search(r"价格[:：]\s*¥?(\d+\.?\d*)", ctx or "")
        return {"note": str(ctx)[:500], "price": float(m.group(1)) if m else 0.0}
    except Exception as e:
        return {"error": str(e)[:200]}


def _get_analyst_report(code: str, price: float) -> dict:
    """获取 TradingAgents 多分析师报告，优先复用 4h 缓存，未命中则实时调用 analyze。

    闭环：check_cache → 命中返回缓存评分 / 未命中 subprocess 调 analyze(180s超时)
         → analyze 内部自动 _save_analysis_cache 写入 → 返回新评分

    Returns: {verdict, composite_score, summary, source} 或 {} (无缓存且 analyze 失败/超时)
    """
    try:
        from stock_kb import StockKB
        kb = StockKB()
        # 1. 先查缓存（4h 内价差<3% 复用）
        cached = kb.check_cache(code, price, max_age_hours=4, max_price_change_pct=3.0)
        if cached.get("hit") and cached.get("report"):
            r = cached["report"]
            return {
                "verdict": r.get("verdict"),
                "composite_score": r.get("composite_score"),
                "summary": (r.get("summary") or "")[:300],
                "source": "cache",
            }
        # 2. 缓存未命中 → subprocess 调 analyze（180s 超时，避免阻塞 agent_desk）
        import subprocess
        r = subprocess.run(
            [cfg.python, "-c",
             f"from tradingagents_runner import analyze; print(analyze('{code}'))"],
            capture_output=True, text=True, timeout=180, cwd=cfg.script_root,
        )
        if r.returncode != 0:
            return {"source": "none", "error": r.stderr[:200]}
        import json
        rec = json.loads(r.stdout.strip().split("\n")[-1])
        return {
            "verdict": (rec.get("signal") or "HOLD").upper(),
            "composite_score": rec.get("confidence"),
            "summary": (rec.get("rationale") or "")[:300],
            "source": "fresh",
        }
    except subprocess.TimeoutExpired:
        return {"source": "none", "error": "analyze timeout(180s)"}
    except Exception as e:
        return {"source": "none", "error": str(e)[:200]}


def _stock_insights(code: str, limit: int = 8) -> List[dict]:
    try:
        from stock_kb import StockKB
        rows = StockKB().get_insights(code, limit=limit)
        return [
            {
                "date": r.get("insight_date"),
                "category": r.get("category"),
                "content": (r.get("content") or "")[:300],
                "confidence": r.get("confidence"),
            }
            for r in rows
        ]
    except Exception as e:
        return [{"error": str(e)[:120]}]


def _latest_apps_snapshot() -> dict:
    """合并最近一档盘中 JSON 路径（供 Hermes 只读）。"""
    paths = [
        "afternoon_output.json",
        "noon_output.json",
        "midday_output.json",
        "flash_output.json",
        "morning_output.json",
    ]
    base = cfg.data_dir
    out = {}
    for name in paths:
        p = os.path.join(base, name)
        if os.path.isfile(p):
            out[name.replace("_output.json", "")] = _load_json(p)
    return out


def _position_from_snapshot(account_snapshot: dict, code: str) -> Optional[dict]:
    for row in account_snapshot.get("positions") or []:
        if str(row.get("code") or "").strip() == code:
            return row
    return None


FORCED_SELL_KEYWORDS = ("连跌", "累计", "止损", "急跌", "大跌", "跌破")


def _extract_research_features(code: str, quant_context: dict) -> dict:
    if not isinstance(quant_context, dict):
        return {}

    feature = quant_context.get("feature_snapshot")
    if isinstance(feature, dict) and any(k in feature for k in ("feature_fresh", "risk_level", "market_regime", "cvar")):
        return feature

    per_stock = quant_context.get("per_stock") or {}
    row = per_stock.get(code) or per_stock.get(str(code)) or {}
    portfolio = quant_context.get("portfolio") or {}
    market_regime = (portfolio.get("market_regime") or {}).get("current_state") or quant_context.get("market_regime")
    feature_fresh = quant_context.get("feature_fresh")
    if feature_fresh is None:
        runtime_flags = quant_context.get("runtime_flags") or {}
        feature_fresh = runtime_flags.get("feature_fresh")

    out = {
        "feature_fresh": bool(feature_fresh) if feature_fresh is not None else False,
        "risk_level": row.get("risk_level"),
        "market_regime": market_regime,
        "cvar": row.get("cvar"),
        "risk_reasons": row.get("risk_reasons") or [],
    }
    return out if any(v is not None and v != [] for v in out.values()) else {}


def _resolve_signal_direction(*, event: dict, account_snapshot: dict,
                              quant_context: dict) -> tuple:
    """T1.10（2026-06-29）信号方向解析器包装层。

    组装「持仓态 × 止损态 × 抄底分 × 大盘态」三元组，调
    direction_resolver.resolve_from_event() 输出 BUY/SELL/HOLD/WAIT/T_FLIP。

    替换旧逻辑：rapid_drop/price_below/rolling_decline 无条件 → SELL。
    新逻辑：持仓+未触发止损 → HOLD（忍）；持仓+触发止损 → SELL；
            空仓+抄底分达标 → BUY；空仓+不达标 → WAIT。

    失败默认 (HOLD, resolver_error) —— 对下跌信号最安全（不卖也不买）。

    Returns:
        (direction, resolver_path)
    """
    try:
        from direction_resolver import resolve_from_event
        from dynamic_stop_loss import is_stop_loss_triggered
        from risk_check import load_price_history

        code = str(event.get("code") or "")
        signal_id = str(event.get("signal_id") or "")
        current_price = float(event.get("price") or 0)

        # 持仓态
        position = _position_from_snapshot(account_snapshot or {}, code)
        holding = position is not None

        # 止损态（仅持仓时计算，照搬 decision_gate._gate_dynamic_stop_loss 模式）
        stop_triggered = False
        if holding:
            avg_cost = float(position.get("cost") or 0)
            if avg_cost > 0 and current_price > 0:
                try:
                    price_history = load_price_history()
                    hist_prices = price_history.get(code, [])
                    daily_returns = []
                    if len(hist_prices) >= 3:
                        for i in range(1, len(hist_prices)):
                            if hist_prices[i - 1] > 0:
                                daily_returns.append(
                                    (hist_prices[i] - hist_prices[i - 1]) / hist_prices[i - 1]
                                )
                    trig, _details = is_stop_loss_triggered(code, avg_cost, current_price, daily_returns)
                    stop_triggered = bool(trig)
                except Exception:
                    stop_triggered = False

        # 大盘态
        feats = _extract_research_features(code, quant_context or {})
        regime = feats.get("market_regime")
        risk_level = feats.get("risk_level")

        # 抄底分（仅空仓时计算，避免持仓态不必要的网络调用）
        bottom_fish_score = None
        if not holding and current_price > 0:
            try:
                from bottom_fish_score import compute as _bf_compute
                bf = _bf_compute(code, current_price, regime, risk_level)
                bottom_fish_score = bf.get("score")
            except Exception:
                bottom_fish_score = None

        # 做T检测（仅持仓+下跌+未触发止损+非弱市时才有意义，单次 fetch_quote）
        open_price = pre_close = None
        if holding and not stop_triggered:
            # T1.10 二期（2026-06-30）：做T适用性 gating，不适用则跳过 fetch_quote 省网络
            try:
                from t_flip_applicability import is_applicable as _t_flip_applicable
                t_flip_ok, _gap, _t_reason = _t_flip_applicable(code)
            except Exception:
                t_flip_ok = True  # 失败保守放行（不阻塞主链路）
            if t_flip_ok:
                try:
                    from market_data import fetch_quote
                    q = fetch_quote(code)
                    if q:
                        open_price = q.get("open")
                        pre_close = q.get("pre_close")
                except Exception:
                    pass

        direction, path = resolve_from_event(
            signal_id=signal_id,
            holding=holding,
            stop_triggered=stop_triggered,
            bottom_fish_score=bottom_fish_score,
            regime=regime,
            risk_level=risk_level,
            open_price=open_price,
            pre_close=pre_close,
            current_price=current_price,
        )
        return direction, path
    except Exception as exc:
        return "HOLD", f"resolver_error: {str(exc)[:160]}"


def _run_decision_gate_for_event(*, event: dict, quant_context: dict,
                                 account_snapshot: dict = None) -> dict:
    try:
        from decision_gate import DecisionGate

        scores = (quant_context or {}).get("analyst_scores") or {}
        # T1.10（2026-06-29）：方向由「持仓态×止损态×抄底分×大盘态」决策树决定，
        # 不再由信号类型写死。rapid_drop 在持仓+未触发止损时为 HOLD，空仓+抄底分达标时为 BUY。
        # HOLD/WAIT/T_FLIP 在门禁前短路（trade_outbox 硬卡 BUY/SELL，position_sizer 把非 BUY 误判 SELL）。
        direction, resolver_path = _resolve_signal_direction(
            event=event, account_snapshot=account_snapshot or {}, quant_context=quant_context,
        )
        if direction not in ("BUY", "SELL"):
            # 短路：不调真实门禁，返回合成 verdict，下游 _build_trade_request_from_decision
            # 因 verdict != APPROVE 返回 None（不创建 trade_request），task dict 仍记录理由供审计。
            try:
                from decision_explainer import build_counterfactual_from_gate
                cf = build_counterfactual_from_gate({"verdict": direction, "direction": direction})
            except Exception as exc:
                cf = {"summary": f"counterfactual unavailable: {str(exc)[:120]}"}
            return {
                "verdict": direction,
                "direction": direction,
                "reasons": [f"direction_resolver: {resolver_path}"],
                "gates": [],
                "suggested_shares": 0,
                "counterfactual": cf,
                "resolver_path": resolver_path,
            }

        result = DecisionGate().check(
            ticker=event.get("code", ""),
            direction=direction,
            analyst_scores=scores,
            current_price=float(event.get("price") or 0),
            research_features=_extract_research_features(event.get("code", ""), quant_context),
        )
        # T1.10 二期（2026-06-30）：修复 resolver_path 不对称——
        # BUY/SELL 路径也注入 resolver_path，与 HOLD/WAIT/T_FLIP 短路分支对齐，
        # 供 trading_journal 审计 + direction_resolver_contract 自检。
        result["resolver_path"] = resolver_path
        try:
            from decision_explainer import build_counterfactual_from_gate

            result["counterfactual"] = build_counterfactual_from_gate(result)
        except Exception as exc:
            result["counterfactual"] = {"summary": f"counterfactual unavailable: {str(exc)[:120]}"}
        return result
    except Exception as exc:
        return {"verdict": "ERROR", "reasons": [str(exc)[:200]], "error": str(exc)[:200]}


def _build_trade_request_from_decision(
    *,
    event: dict,
    handle_result: dict,
    trading_account: str,
    decision_gate_result: dict,
) -> Optional[dict]:
    if not isinstance(decision_gate_result, dict):
        return None
    if decision_gate_result.get("verdict") != "APPROVE":
        return None

    direction = str(decision_gate_result.get("direction") or "").upper()
    if direction not in ("BUY", "SELL"):
        return None

    suggested_shares = int(decision_gate_result.get("suggested_shares") or 0)
    price = float(event.get("price") or 0) or None
    counterfactual = (decision_gate_result.get("counterfactual") or {}).get("summary") or ""
    mapped_action = decision_gate_result.get("mapped_action") or direction
    reasons = decision_gate_result.get("reasons") or []
    summary_parts = [
        f"门禁通过: {mapped_action}",
        reasons[0] if reasons else "",
        counterfactual,
    ]
    gate_summary = "；".join([p for p in summary_parts if p])[:500]

    try:
        import trade_outbox

        proposal = trade_outbox.propose_and_notify(
            event.get("code", ""),
            direction,
            name=event.get("name", event.get("code", "")),
            price=price,
            shares=suggested_shares or None,
            gate_verdict="APPROVE",
            gate_summary=gate_summary,
            event_id=event.get("event_id"),
            signal_id=event.get("signal_id"),
            lineage_id=handle_result.get("lineage_id"),
            account_id=trading_account,
            decision_gate=decision_gate_result,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300], "direction": direction}

    if not proposal.get("ok"):
        return proposal

    # T1.10 二期（2026-06-30）：写 trading_journal 决策事件，供审计 + direction_resolver_contract 自检
    try:
        from trade_db import TradeDB
        TradeDB().log_trade_event(
            code=event.get("code", ""),
            name=event.get("name", event.get("code", "")),
            action=direction,
            event_id=event.get("event_id"),
            signal_id=event.get("signal_id"),
            request_id=proposal.get("request_id"),
            resolver_path=decision_gate_result.get("resolver_path") or "",
            decision_gate=decision_gate_result,
            rationale="; ".join(decision_gate_result.get("reasons") or [])[:300],
        )
    except Exception:
        pass  # 审计日志失败不阻塞交易链路

    return {
        "ok": True,
        "direction": direction,
        "request_id": proposal.get("request_id"),
        "wechat_template": proposal.get("wechat_template"),
        "wechat_notify": proposal.get("wechat_notify"),
        "wechat_sent": proposal.get("wechat_sent"),
        "shares": suggested_shares or None,
    }


def _build_trade_request_from_plan(*, proposal: dict, trading_account: str) -> Optional[dict]:
    if not isinstance(proposal, dict):
        return None
    code = str(proposal.get("code") or "").strip()
    direction = str(proposal.get("direction") or "BUY").upper()
    shares = int(proposal.get("shares") or 0)
    if not code or direction not in ("BUY", "SELL") or shares <= 0:
        return None

    rationale = str(proposal.get("rationale") or "")[:500]
    try:
        import trade_outbox

        out = trade_outbox.propose_and_notify(
            code,
            direction,
            name=proposal.get("name") or code,
            price=float(proposal.get("price") or 0) or None,
            shares=shares,
            gate_verdict="APPROVE",
            gate_summary=rationale or f"morning_plan {direction}",
            signal_id="morning_plan",
            lineage_id=proposal.get("lineage_id"),
            account_id=trading_account,
            decision_gate={
                "verdict": "APPROVE",
                "direction": direction,
                "mapped_action": direction,
                "reasons": [rationale] if rationale else [],
                "suggested_shares": shares,
                "source": "morning_plan",
                "proposal_generated_at": proposal.get("proposal_generated_at"),
            },
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300], "direction": direction, "code": code}

    if not out.get("ok"):
        return out

    return {
        "ok": True,
        "source": "morning_plan",
        "code": code,
        "direction": direction,
        "shares": shares,
        "request_id": out.get("request_id"),
        "wechat_template": out.get("wechat_template"),
        "wechat_notify": out.get("wechat_notify"),
        "wechat_sent": out.get("wechat_sent"),
    }


def _expire_stale_pending_requests(*, now: Optional[datetime] = None) -> int:
    state = _load_json(STATE_PATH)
    rows = state.get("pending_trade_requests") or []
    if not isinstance(rows, list) or not rows:
        return 0

    current = now or datetime.now()
    changed = 0
    for row in rows:
        if str(row.get("status") or "") != "pending":
            continue
        expires_at = str(row.get("expires_at") or "")
        if not expires_at:
            continue
        try:
            expired = datetime.fromisoformat(expires_at) < current
        except ValueError:
            continue
        if not expired:
            continue
        row["status"] = "expired"
        row["resolved_at"] = current.isoformat()
        row.setdefault("note", "")
        note = str(row.get("note") or "").strip()
        row["note"] = "auto-expired by agent_desk" if not note else note
        changed += 1

    if changed:
        state["pending_trade_requests"] = rows
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
        try:
            import trade_outbox

            trade_outbox._save_state(state)
        except Exception:
            pass
    return changed


def _emit_morning_plan_requests(*, trading_account: Optional[str], account_snapshot: dict) -> List[dict]:
    if not trading_account:
        return []
    data = _load_json(MORNING_OUTPUT_PATH)
    proposals = data.get("buy_proposals") or []
    if not proposals:
        return []

    # ── once-per-day guard ──
    today = datetime.now().strftime("%Y-%m-%d")
    state = _load_json(STATE_PATH)
    if str(state.get("last_morning_plan_date") or "") == today:
        return []
    state["last_morning_plan_date"] = today
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)

    # ── position concentration cap (20% single-stock max) ──
    total_assets = 0.0
    positions = account_snapshot.get("positions") or []
    if isinstance(positions, dict):
        positions = list(positions.values())
    for pos in positions or []:
        shares = int(pos.get("shares") or 0)
        price = float(pos.get("last_price") or pos.get("current_price") or pos.get("price") or 0)
        total_assets += shares * price
    cash = float(account_snapshot.get("cash") or 0)
    total_assets += cash
    if total_assets <= 0:
        total_assets = 100000.0
    max_single_value = total_assets * 0.20  # 20% hard cap

    current_holdings = {}
    for pos in positions or []:
        code = str(pos.get("code") or "").strip()
        if code:
            shares = int(pos.get("shares") or 0)
            price = float(pos.get("last_price") or pos.get("current_price") or pos.get("price") or 0)
            current_holdings[code] = shares * price

    proposal_generated_at = str(data.get("generated_at") or "")
    existing_codes = set()
    now = datetime.now()
    for row in state.get("pending_trade_requests") or []:
        if row.get("signal_id") != "morning_plan":
            continue
        if str(row.get("account_id") or "") != str(trading_account):
            continue
        row_generated_at = str(row.get("proposal_generated_at") or "")
        if proposal_generated_at and row_generated_at and row_generated_at != proposal_generated_at:
            continue
        status = str(row.get("status") or "")
        if status == "pending":
            expires_at = str(row.get("expires_at") or "")
            if expires_at:
                try:
                    if datetime.fromisoformat(expires_at) < now:
                        continue
                except ValueError:
                    pass
            existing_codes.add(str(row.get("code") or ""))
            continue
        if status == "resolved":
            existing_codes.add(str(row.get("code") or ""))

    emitted = []
    for proposal in proposals:
        code = str(proposal.get("code") or "")
        if not code or code in existing_codes:
            continue
        buy_value = float(proposal.get("buy_value") or 0)
        if buy_value <= 0:
            price = float(proposal.get("price") or 0)
            shares = int(proposal.get("shares") or 0)
            buy_value = price * shares
        existing_value = current_holdings.get(code, 0.0)
        if existing_value + buy_value > max_single_value and existing_value > 0:
            # Scale down to fit within 20% cap, minimum 1 lot
            available = max(0.0, max_single_value - existing_value)
            if available <= 0:
                continue
            price = float(proposal.get("price") or 0)
            if price <= 0:
                continue
            capped_shares = max(100, int(available / price / 100) * 100)
            proposal["shares"] = capped_shares
            proposal["buy_value"] = capped_shares * price
            proposal["rationale"] = (proposal.get("rationale") or "") + f" (capped to {capped_shares}sh from concentration limit)"
        elif existing_value + buy_value > max_single_value:
            # New position but would exceed 20% — cap to 20%
            price = float(proposal.get("price") or 0)
            if price <= 0:
                continue
            capped_shares = max(100, int(max_single_value / price / 100) * 100)
            if capped_shares < 100:
                continue
            proposal["shares"] = capped_shares
            proposal["buy_value"] = capped_shares * price
            proposal["rationale"] = (proposal.get("rationale") or "") + f" (capped to {capped_shares}sh from concentration limit)"

        proposal_payload = dict(proposal)
        if proposal_generated_at and not proposal_payload.get("proposal_generated_at"):
            proposal_payload["proposal_generated_at"] = proposal_generated_at
        result = _build_trade_request_from_plan(proposal=proposal_payload, trading_account=trading_account)
        if result:
            emitted.append(result)
            existing_codes.add(code)
    return emitted


def _emit_de_risk_requests(account_snapshot: dict, trading_account: str) -> List[dict]:
    """Read de_risk_plan from morning_output.json and auto-create SELL requests.

    T1.7 修复（2026-06-26）：盘中用 snapshot 实时价格重验"长线盈利股豁免"。
    早报 plan 一次性算豁免写入 morning_output.json，盘中价格下跌导致浮盈跌破
    10% 阈值时，原豁免本应失效但 plan 不再重算 → HIGH 风险事件被豁免吞没。
    现对 skipped_long_term 中"长线盈利股豁免"的标的用实时价格重算浮盈，
    跌破阈值即追加减仓 action（仅扩大不缩小，新仓保护期豁免不重验）。
    """
    if not trading_account or not account_snapshot:
        return []
    data = _load_json(MORNING_OUTPUT_PATH)
    de_risk = data.get("de_risk_plan") or {}
    actions = list(de_risk.get("actions") or [])
    skipped = de_risk.get("skipped_long_term") or []

    # T1.7: 盘中重验"长线盈利股豁免"
    LONG_TERM_PROFIT_PCT = 0.10
    revalidated = []
    for item in skipped:
        reason = str(item.get("reason") or "")
        if "长线盈利股豁免" not in reason:
            continue  # 新仓保护期豁免不重验（开仓日期盘中不变）
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        position = _position_from_snapshot(account_snapshot, code)
        if not position:
            continue
        cost = float(position.get("cost") or 0)
        price = float(position.get("last_price") or position.get("price") or 0)
        if cost <= 0 or price <= 0:
            continue
        profit_pct = (price - cost) / cost
        if profit_pct >= LONG_TERM_PROFIT_PCT:
            continue  # 浮盈仍达标，豁免继续生效
        held_shares = int(position.get("shares") or 0)
        if held_shares < 100:
            continue
        # 豁免失效 → 追加 1 手减仓（保守最小动作）
        revalidated.append({
            "code": code,
            "name": item.get("name") or position.get("name") or code,
            "direction": "SELL",
            "shares": 100,
            "price": round(price, 2),
            "reason": f"T1.7 盘中重验：长线盈利豁免失效（浮盈 {profit_pct:.1%} < {int(LONG_TERM_PROFIT_PCT*100)}%），追加减仓",
            "lineage_id": de_risk.get("lineage_id") or "",
        })

    all_actions = actions + revalidated
    if not all_actions:
        return []

    emitted = []
    for action in all_actions:
        code = str(action.get("code") or "").strip()
        action_dir = str(action.get("direction") or "SELL").upper()
        shares = int(action.get("shares") or 0)
        if not code or action_dir != "SELL" or shares <= 0:
            continue
        position = _position_from_snapshot(account_snapshot, code)
        if not position:
            continue
        held_shares = int(position.get("shares") or 0)
        if held_shares <= 0:
            continue
        lot_shares = max(100, min(shares, held_shares) // 100 * 100)
        if lot_shares <= 0:
            continue
        reason = action.get("reason", "de_risk_plan")[:200]
        try:
            import trade_outbox
            proposal = trade_outbox.propose_and_notify(
                code,
                "SELL",
                name=action.get("name") or position.get("name") or code,
                price=float(action.get("price") or 0) or None,
                shares=lot_shares,
                gate_verdict="APPROVE",
                gate_summary=f"de_risk_plan: {reason[:120]}",
                signal_id="de_risk_plan",
                account_id=trading_account,
                decision_gate={
                    "verdict": "APPROVE",
                    "direction": "SELL",
                    "mapped_action": "SELL",
                    "reasons": [reason],
                    "suggested_shares": lot_shares,
                    "source": "de_risk_plan",
                },
            )
        except Exception as exc:
            proposal = {"ok": False, "error": str(exc)[:300]}
        if proposal.get("ok"):
            emitted.append({
                "ok": True,
                "de_risk": True,
                "direction": "SELL",
                "code": code,
                "shares": lot_shares,
                "request_id": proposal.get("request_id"),
                "wechat_template": proposal.get("wechat_template"),
                "wechat_sent": proposal.get("wechat_sent"),
                "reason": reason,
            })
    return emitted


def _build_forced_risk_request(
    *,
    event: dict,
    handle_result: dict,
    account_snapshot: dict,
    trading_account: str,
    decision_gate_result: dict,
) -> Optional[dict]:
    code = event.get("code", "")
    position = _position_from_snapshot(account_snapshot, code)
    if not position:
        return None

    reason = event.get("reason", "")
    signal_id = event.get("signal_id", "")
    if not any(k in reason for k in FORCED_SELL_KEYWORDS) and not any(
        k in signal_id for k in ("rolling_decline", "rapid_drop", "price_below")
    ):
        return None

    # T1.10（2026-06-29）forced_risk 止损护栏：未触发动态止损不强制减仓。
    # 旧逻辑：持仓股急跌（rapid_drop/rolling_decline/price_below）→ 无条件半仓 SELL，
    # 导致紫光国微 -3.23% 反弹前被强制卖出。新逻辑：只有止损真正触发才强制减险。
    # 用户/Agent 仍可通过正常 SELL 请示门禁主动减仓（不经过本函数）。
    avg_cost = float(position.get("cost") or 0)
    current_price = float(event.get("price") or 0)
    if avg_cost > 0 and current_price > 0:
        try:
            from dynamic_stop_loss import is_stop_loss_triggered
            from risk_check import load_price_history
            price_history = load_price_history()
            hist_prices = price_history.get(code, [])
            daily_returns = []
            if len(hist_prices) >= 3:
                for i in range(1, len(hist_prices)):
                    if hist_prices[i - 1] > 0:
                        daily_returns.append(
                            (hist_prices[i] - hist_prices[i - 1]) / hist_prices[i - 1]
                        )
            trig, _details = is_stop_loss_triggered(code, avg_cost, current_price, daily_returns)
            if not trig:
                return None  # 未触发止损 → 不强制卖（T1.10 核心）
        except Exception:
            # 止损态判定失败时保守放行（保留旧 forced_risk 行为），避免护栏异常导致风险事件静默
            pass

    shares = int(position.get("shares") or 0)
    if shares <= 0:
        return None

    lot_shares = shares if shares < 100 else max(100, (shares // 2 // 100) * 100)
    if lot_shares <= 0:
        lot_shares = shares

    gate_summary = f"风险事件强制减仓请示: {reason[:120]}"

    try:
        import trade_outbox

        proposal = trade_outbox.propose_and_notify(
            code,
            "SELL",
            name=event.get("name", code),
            price=float(event.get("price") or 0) or None,
            shares=lot_shares,
            gate_verdict="APPROVE",
            gate_summary=gate_summary,
            event_id=event.get("event_id"),
            signal_id=signal_id,
            lineage_id=handle_result.get("lineage_id"),
            lineage_stages=[
                {
                    "stage": "DESK_FORCED_RISK",
                    "source": "agent_desk",
                    "payload": {
                        "summary": gate_summary,
                        "reason": reason[:200],
                        "position_shares": shares,
                        "suggested_shares": lot_shares,
                    },
                }
            ],
            account_id=trading_account,
            decision_gate=decision_gate_result if isinstance(decision_gate_result, dict) else None,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc)[:300],
            "reason": reason,
            "forced_risk": True,
        }

    if proposal.get("ok"):
        # T1.10 二期（2026-06-30）：forced_risk SELL 也写 trading_journal
        try:
            from trade_db import TradeDB
            TradeDB().log_trade_event(
                code=code,
                name=event.get("name", code),
                action="SELL",
                event_id=event.get("event_id"),
                signal_id=signal_id,
                request_id=proposal.get("request_id"),
                resolver_path="forced_risk_stop_triggered",
                decision_gate=decision_gate_result if isinstance(decision_gate_result, dict) else {},
                rationale=f"forced_risk: {reason[:200]}",
            )
        except Exception:
            pass
        return {
            "ok": True,
            "forced_risk": True,
            "direction": "SELL",
            "shares": lot_shares,
            "request_id": proposal.get("request_id"),
            "wechat_template": proposal.get("wechat_template"),
            "wechat_notify": proposal.get("wechat_notify"),
            "wechat_sent": proposal.get("wechat_sent"),
            "reason": reason,
        }
    return {"forced_risk": True, **proposal, "reason": reason}


def process_pending(*, max_events: int = 5, trading_account_id: str = None) -> Dict[str, Any]:
    from signal_loop import handle_trigger

    expired_count = _expire_stale_pending_requests()

    trading_account = None
    account_snapshot = {}
    desk_account_error = None
    try:
        from trade_accounts import HermesTradingError, resolve_trading_account
        from trade_account_context import load_account_snapshot

        trading_account = resolve_trading_account(trading_account_id)
        account_snapshot = load_account_snapshot(trading_account)
        # 持仓源不可读时（easyths 配置缺失等）必须视作 account error，
        # 否则 position_reconciliation 会用空持仓对比旧基准 → 误报 manual_sell 漂移 → 偷跑 LLM
        if account_snapshot.get("error"):
            desk_account_error = str(account_snapshot["error"])[:500]
    except Exception as exc:
        desk_account_error = str(exc)[:500]

    pending = list_pending(limit=max_events)
    skipped: List[dict] = []
    analyze_tasks: List[dict] = []
    forced_trade_requests: List[dict] = []
    planned_trade_requests = _emit_morning_plan_requests(
        trading_account=trading_account,
        account_snapshot=account_snapshot,
    ) if not desk_account_error else []

    de_risk_requests = _emit_de_risk_requests(
        account_snapshot=account_snapshot,
        trading_account=trading_account,
    ) if not desk_account_error else []

    # ── dedup: skip requests already presented to desk LLM ──
    state = _load_json(STATE_PATH)
    presented_ids = set(str(rid) for rid in (state.get("presented_request_ids") or []))
    planned_trade_requests = [r for r in planned_trade_requests if str(r.get("request_id") or "") not in presented_ids]
    de_risk_requests = [r for r in de_risk_requests if str(r.get("request_id") or "") not in presented_ids]
    new_presented = {str(r.get("request_id") or "") for r in (planned_trade_requests + de_risk_requests) if r.get("request_id")}
    if new_presented:
        presented_ids.update(new_presented)
        state["presented_request_ids"] = list(presented_ids)[-200:]
        _save_agent_state({"presented_request_ids": state["presented_request_ids"]})

    # ── post-sell re-scan: after de_risk sells execute, re-assess if cash freed up ──
    post_sell_buy_requests: List[dict] = []
    if not desk_account_error:
        try:
            import post_execution_rescan as psr
            psr.reset_daily()
            should, reason = psr.should_rescan(account_snapshot)
            if should:
                excluded = psr.get_executed_sell_codes_today()
                proposals = psr.run_buy_allocation(account_snapshot, excluded)
                new_depth = psr.increment_depth()
                for proposal in proposals:
                    code = str(proposal.get("code") or "")
                    if not code:
                        continue
                    proposal["rationale"] = (proposal.get("rationale") or "") + f" [post-sell rescan d={new_depth}]"
                    result = _build_trade_request_from_plan(proposal=proposal, trading_account=trading_account)
                    if result and result.get("ok"):
                        post_sell_buy_requests.append(result)
        except Exception:
            pass

    # ── position drift detection ──
    position_drift = None
    if not desk_account_error:
        try:
            import position_reconciliation as pr
            position_drift = pr.detect_drift(account_snapshot)
        except Exception:
            pass

    for ev in pending:
        eid = ev.get("event_id", "")
        if desk_account_error:
            ack(eid, result={"action": "SKIP", "reason": "hermes_trading_stopped"})
            skipped.append({"event_id": eid, "reason": "hermes_trading_stopped", "error": desk_account_error})
            continue
        if not ev.get("parse_ok", True):
            ack(eid, result={"action": "SKIP", "reason": "parse_failed"})
            skipped.append({"event_id": eid, "reason": "parse_failed"})
            continue

        code = ev.get("code", "")
        sid = ev.get("signal_id", "")
        price = float(ev.get("price") or 0)
        pct = float(ev.get("change_pct") or 0)
        vol = float(ev.get("volume") or 0)

        hr = handle_trigger(sid, code, price, pct, vol)
        action = hr.get("action", "SKIP")

        # T1.9 Bug A 修复（2026-06-26）：forced_risk 必须尊重 signal_loop 的拒绝。
        # signal_loop 已 FILTER_REJECT（T+1 锁定/quota 拒绝等）返回 SKIP 时，
        # 不得再走 forced_risk propose，否则 signal_loop 的所有过滤形同虚设。
        forced_request = None
        if action != "SKIP":
            # 强制减仓请求：基于信号关键词快速判定，但仍留 decision_gate 审计 + counterfactual
            forced_gate = _run_decision_gate_for_event(event=ev, quant_context={}, account_snapshot=account_snapshot)
            forced_request = _build_forced_risk_request(
                event=ev,
                handle_result=hr,
                account_snapshot=account_snapshot,
                trading_account=trading_account,
                decision_gate_result=forced_gate,
            )
        else:
            forced_gate = None
        if forced_request:
            ack_result = {**hr, "forced_trade_request": forced_request, "decision_gate": forced_gate}
            ack(eid, result=ack_result)
            forced_trade_requests.append(
                {
                    "event_id": eid,
                    "code": code,
                    "name": ev.get("name", code),
                    **forced_request,
                }
            )
            continue

        if action != "ANALYZE":
            ack(eid, result=hr)
            skipped.append({"event_id": eid, "code": code, **hr})
            continue

        # 仅对 ANALYZE 事件执行重型操作（量化上下文/HMM/GARCH/决策门禁）
        quant_context = _fetch_quant_context(code)
        # TradingAgents 多分析师报告（4h缓存优先，未命中实时调 analyze）
        # —— 量化引擎完整参与买卖信号生产链路：分析报告 verdict/confidence 注入决策门禁
        analyst_report = _get_analyst_report(code, float(quant_context.get("price") or 0))
        if analyst_report and analyst_report.get("verdict"):
            quant_context["ta_verdict"] = analyst_report.get("verdict")
            quant_context["ta_confidence"] = analyst_report.get("composite_score")
            quant_context["ta_summary"] = analyst_report.get("summary")
            quant_context["ta_source"] = analyst_report.get("source")
        decision_gate_result = _run_decision_gate_for_event(event=ev, quant_context=quant_context, account_snapshot=account_snapshot)

        lineage_id = hr.get("lineage_id") or ""
        try:
            from core.engines import signal_lineage as sl

            if not lineage_id:
                lineage_id = sl.new_lineage_id("desk")
            sl.append(
                "DESK_ENQUEUE",
                "agent_desk",
                code=code,
                lineage_id=lineage_id,
                payload={
                    "summary": ev.get("reason", "")[:200],
                    "event_id": eid,
                    "signal_id": sid,
                    "action": action,
                },
            )
        except Exception:
            pass

        normal_request = _build_trade_request_from_decision(
            event=ev,
            handle_result=hr,
            trading_account=trading_account,
            decision_gate_result=decision_gate_result,
        )

        task = {
            "event_id": eid,
            "signal_id": sid,
            "lineage_id": lineage_id,
            "trading_account_id": trading_account,
            "account_snapshot": account_snapshot,
            "code": code,
            "name": ev.get("name", code),
            "reason": ev.get("reason", ""),
            "price": price,
            "change_pct": pct,
            "handle_trigger": hr,
            "decision_gate": decision_gate_result,
            "counterfactual": (decision_gate_result or {}).get("counterfactual") or {},
            "trade_request": normal_request,
            "quant_context": quant_context,
            "stock_insights": _stock_insights(code),
            "playbook_patterns": _load_playbook(code),
            "registry_plugins": _run_registry_plugins(code),
        }
        analyze_tasks.append(task)

    needs = (
        len(analyze_tasks) > 0
        or len(forced_trade_requests) > 0
        or len(planned_trade_requests) > 0
        or len(de_risk_requests) > 0
        or len(post_sell_buy_requests) > 0
        or bool(position_drift and position_drift.get("has_drift"))
    ) and trading_account is not None and not desk_account_error

    result = {
        "generated_at": datetime.now().isoformat(),
        "trading_account_id": trading_account,
        "account_snapshot": account_snapshot if needs else {},
        "desk_account_error": desk_account_error,
        "pending_in": pending_count(),
        "processed": len(pending),
        "expired_pending_requests": expired_count,
        "skipped": skipped,
        "forced_trade_requests": forced_trade_requests if needs else [],
        "de_risk_requests": de_risk_requests if needs else [],
        "planned_trade_requests": planned_trade_requests if needs else [],
        "post_sell_buy_requests": post_sell_buy_requests if needs else [],
        "position_drift": position_drift if needs else None,
        "analyze_tasks": analyze_tasks if needs else [],
        "needs_hermes": needs,
        "apps_snapshot_keys": list(_latest_apps_snapshot().keys()),
        "apps_snapshot": _latest_apps_snapshot() if analyze_tasks else {},
        "agent_state_path": STATE_PATH,
        "instruction": (
            "若 needs_hermes=false：完全静默，不输出。"
            "若 desk_account_error：一行说明并停止，禁止跨账户用 guard/实盘持仓替代表账户。"
            "若存在 forced_trade_requests 或 de_risk_requests：优先逐条输出请示，不得静默吞掉。"
            "de_risk_requests 来自 morning de_risk_plan，是代码直接生成的强制减仓请求，须逐条汇报，不得静默跳过。"
            "若 analyze_tasks 非空：仅依据本任务 trading_account_id 与 account_snapshot 评估仓位/T+1；"
            "若已有 trade_request：优先引用已有 request_id/微信请示，不得重复创建；"
            "propose 必须带该 account_id；BUY/SELL 走 trade_outbox；WAIT 则 close。"
        ),
    }

    _save_agent_state(
        {
            "last_desk_run": result["generated_at"],
            "last_pending": pending_count(),
            "last_analyze_count": len(analyze_tasks),
        }
    )
    return result


def main():
    import argparse

    p = argparse.ArgumentParser(description="Agent Desk v5")
    p.add_argument("--json", action="store_true", help="stdout JSON only")
    p.add_argument("--max", type=int, default=5)
    p.add_argument("--ack-all-skipped", action="store_true", help="dev: ack remaining pending")
    args = p.parse_args()

    if args.ack_all_skipped:
        for ev in list_pending():
            ack(ev.get("event_id", ""), result={"action": "SKIP", "reason": "runtime_clear"})
        print(json.dumps({"cleared": True}, ensure_ascii=False))
        return

    out = process_pending(max_events=args.max)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

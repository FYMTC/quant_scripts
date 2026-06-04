#!/config/quant_env/bin/python3
"""
apps/morning.py — 盘前简报（v4.0 代码管线）

替代 08:30 cron 长 prompt。纯代码：取数据→跑量化→硬约束→输出JSON。
LLM 仅读取输出的 JSON 做最终判断，不取数不计算。

输出契约:
  stdout: JSON {
    holdings: [{code, name, shares, cost, price, pnl, ...}],
    candidates: [{code, name, composite_score, ...}],
    constraints: [{check, pass, message}],
    quant_summary: {cvar, garch, hmm, momentum, ...},
    recommendation: "READY" | "CAUTION" | "BLOCKED"
  }

用法:
  python apps/morning.py              # stdout JSON
  python apps/morning.py --save FILE  # 落盘JSON
"""

import sys, os, json, time
from datetime import datetime
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from data_converter import fetch_kline_baostock
from trade_account_context import load_portfolio_truth
import warnings
warnings.filterwarnings('ignore')
from risk_metrics import calc_cvar, calc_multi_momentum, calc_garch_vol, calc_max_drawdown, calc_gbm_cvar
from position_sizer import PositionSizer, SizerInput

RUNTIME_DATA_DIR = os.environ.get("QUANT_RUNTIME_DATA_DIR") or "/config/quant_scripts/data"
FEATURE_SNAPSHOT_PATH = os.path.join(RUNTIME_DATA_DIR, "feature_snapshot.json")


def load_holdings() -> list:
    """从 EasyTHS 账户快照加载持仓+行情"""
    pf = load_portfolio_truth()
    positions = pf.get("positions", {})
    cash = pf.get("cash", 0.0)

    holdings = []
    for code, info in positions.items():
        shares = info.get("shares", 0)
        cost = float(info.get("cost") or 0.0)
        fallback_price = float(info.get("current_price") or 0.0)
        fallback_market_value = float(info.get("market_value") or 0.0)
        try:
            import io, contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                records = fetch_kline_baostock(code, "20260101", datetime.now().strftime("%Y%m%d"))
            if records and len(records) >= 2:
                closes = [float(r['收盘']) for r in records]
                price = closes[-1]
                prev = closes[-2]
                pct = (price - prev) / prev * 100 if prev else 0.0
                n_days = len(records)
            elif fallback_price > 0:
                price = fallback_price
                pct = 0.0
                n_days = 0
            else:
                continue
            pnl = (price - cost) * shares
            pnl_pct = (price - cost) / cost * 100 if cost > 0 else 0.0
            holdings.append({
                "code": code,
                "name": info.get("name") or code,
                "shares": shares,
                "cost": round(cost, 2),
                "price": round(price, 2),
                "change_pct": round(pct, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "market_value": round(price * shares, 2),
                "n_days": n_days,
            })
        except Exception:
            price = fallback_price
            market_value = fallback_market_value or price * shares
            pnl = (price - cost) * shares if price > 0 else 0.0
            pnl_pct = (price - cost) / cost * 100 if cost > 0 and price > 0 else 0.0
            holdings.append({
                "code": code,
                "name": info.get("name") or code,
                "shares": shares,
                "cost": round(cost, 2),
                "price": round(price, 2),
                "change_pct": 0.0,
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "market_value": round(market_value, 2),
                "error": "行情不可用",
                "n_days": 0,
            })

    total_market = sum(h.get("market_value", h.get("price", 0) * h.get("shares", 0)) for h in holdings)
    total_assets = float(pf.get("total_assets") or 0.0)
    if total_assets <= 0:
        total_assets = total_market + cash

    return holdings, cash, total_assets


def load_candidates() -> list:
    """加载昨夜选股引擎结果"""
    path = os.path.join(RUNTIME_DATA_DIR, "screener_top15.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("results", [])[:10]
    except Exception:
        return []


def load_feature_snapshot() -> dict:
    try:
        with open(FEATURE_SNAPSHOT_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _feature_fallback_row(candidate: dict) -> dict:
    cvar = candidate.get("cvar")
    try:
        cvar_value = float(cvar) if cvar is not None else None
    except (TypeError, ValueError):
        cvar_value = None
    risk_reasons = []
    if cvar_value is not None and cvar_value <= -5.0:
        risk_reasons.append(f"candidate_cvar={cvar_value:.2f}%")
    return {
        "code": str(candidate.get("code") or ""),
        "name": candidate.get("name") or str(candidate.get("code") or ""),
        "scope": "candidate_fallback",
        "current_price": candidate.get("price"),
        "position_ratio": None,
        "risk_level": "unknown",
        "risk_reasons": risk_reasons,
        "cvar": cvar_value,
        "cvar_trend": None,
        "momentum": {
            "composite": candidate.get("composite_score"),
            "consistency": candidate.get("consistency"),
        },
        "momentum_analysis": None,
        "max_drawdown": candidate.get("max_drawdown"),
        "garch": {
            "ann_vol": candidate.get("garch_vol") or candidate.get("ann_vol"),
        },
        "data_quality": "fallback_candidate",
    }


def augment_feature_snapshot_for_candidates(feature_snapshot: dict, candidates: list) -> dict:
    snapshot = dict(feature_snapshot or {})
    per_stock = dict((snapshot.get("per_stock") or {}))
    runtime_flags = dict((snapshot.get("runtime_flags") or {}))
    missing_codes = list(runtime_flags.get("missing_codes") or [])
    supplemented_codes = []

    for candidate in candidates or []:
        code = str(candidate.get("code") or "")
        if not code or code in per_stock:
            continue
        per_stock[code] = _feature_fallback_row(candidate)
        supplemented_codes.append(code)
        if code in missing_codes:
            missing_codes.remove(code)

    runtime_flags["missing_codes"] = missing_codes
    runtime_flags["supplemented_candidate_codes"] = supplemented_codes
    snapshot["per_stock"] = per_stock
    snapshot["runtime_flags"] = runtime_flags
    return snapshot


def allocate_buy_candidates(holdings: list, cash: float, total_assets: float, candidates: list, feature_snapshot: dict, event_risk: dict | None = None) -> list:
    if total_assets <= 0 or cash <= 0 or not candidates:
        return []

    playbook = ((event_risk or {}).get("playbook") or {})
    buy_score_threshold = float(playbook.get("buy_score_threshold") or 1.0)
    max_gross_exposure = float(playbook.get("max_gross_exposure") or 0.8)
    allow_new_buy = bool(playbook.get("allow_new_buy", True))
    risk_level = str(playbook.get("level") or "").upper()
    probe_mode = (not allow_new_buy) and risk_level == "HIGH"
    if not allow_new_buy and not probe_mode:
        return []

    def _probe_budget_floor(price: float) -> float:
        if not probe_mode or price <= 0:
            return 0.0
        return price * 100

    current_gross = (total_assets - cash) / total_assets if total_assets > 0 else 0.0
    target_gross = min(max_gross_exposure, 0.95)
    gross_room_value = max(0.0, (target_gross - current_gross) * total_assets)
    reserve_cash = max(total_assets * 0.10, cash * 0.10)
    deployable_cash = max(0.0, min(cash - reserve_cash, gross_room_value if gross_room_value > 0 else cash - reserve_cash))
    if probe_mode:
        deployable_cash = min(deployable_cash, total_assets * 0.03, cash * 0.08)
    if deployable_cash <= 0:
        return []

    per_stock_features = (feature_snapshot.get("per_stock") or {}) if isinstance(feature_snapshot, dict) else {}
    current_position_ratio = {
        str(h.get("code") or ""): ((h.get("market_value") or (h.get("price", 0) * h.get("shares", 0))) / total_assets if total_assets > 0 else 0.0)
        for h in holdings
    }

    eligible = []
    for candidate in candidates:
        code = str(candidate.get("code") or "")
        if not code:
            continue
        score = float(candidate.get("composite_score") or 0.0)
        risk_row = per_stock_features.get(code) or {}
        score_floor = max(buy_score_threshold, 1.35) if probe_mode else buy_score_threshold
        if not risk_row and probe_mode:
            score_floor = max(score_floor, 1.45)
        if score < score_floor:
            continue
        stock_risk_level = str(risk_row.get("risk_level") or "").lower()
        cvar = risk_row.get("cvar", candidate.get("cvar"))
        try:
            cvar_value = float(cvar) if cvar is not None else None
        except (TypeError, ValueError):
            cvar_value = None
        if stock_risk_level == "danger":
            continue
        if cvar_value is not None and cvar_value < -8.0:
            continue
        if probe_mode and cvar_value is not None and cvar_value < -8.0:
            continue
        price = float(candidate.get("price") or 0.0)
        if price <= 0:
            continue
        eligible.append({
            "candidate": candidate,
            "score": score,
            "price": price,
            "risk_level": stock_risk_level or "unknown",
            "cvar": cvar_value,
        })

    eligible.sort(key=lambda row: row["score"], reverse=True)
    selected = eligible[:1] if probe_mode else eligible[:3]
    if not selected:
        return []

    total_score = sum(max(row["score"], 0.0) for row in selected) or float(len(selected))
    sizer = PositionSizer(total_assets=total_assets, available_cash=cash)
    proposals = []
    cash_remaining = cash
    deploy_remaining = deployable_cash

    for row in selected:
        candidate = row["candidate"]
        code = str(candidate.get("code") or "")
        score = max(row["score"], 0.0)
        budget_share = score / total_score if total_score > 0 else (1.0 / len(selected))
        target_budget = min(deploy_remaining, deployable_cash * budget_share)
        min_probe_budget = _probe_budget_floor(row["price"])
        if probe_mode and min_probe_budget > 0:
            target_budget = max(target_budget, min_probe_budget)
        if target_budget <= 0:
            continue
        current_ratio = current_position_ratio.get(code, 0.0)
        confidence = min(0.95, max(0.35, score / 2.0))
        if probe_mode:
            confidence = min(0.65, max(confidence, 0.6))
        annual_vol = candidate.get("garch_vol") or candidate.get("ann_vol") or 30.0
        try:
            annual_vol = float(annual_vol) / 100.0
        except (TypeError, ValueError):
            annual_vol = 0.30
        per_name_cash = min(cash_remaining, max(target_budget, row["price"] * 100))
        if probe_mode:
            per_name_cash = min(cash_remaining, max(target_budget, row["price"] * 100 + total_assets * 0.10))
        sizing = PositionSizer(total_assets=total_assets, available_cash=per_name_cash).calculate(
            SizerInput(
                code=code,
                name=str(candidate.get("name") or code),
                direction="BUY",
                confidence=confidence,
                current_shares=0,
                current_price=row["price"],
                avg_cost=0.0,
                total_assets=total_assets,
                annual_volatility=annual_vol,
            )
        )
        shares = int(sizing.suggested_shares or 0)
        if shares <= 0:
            continue
        buy_value = shares * row["price"]
        effective_deploy_limit = deploy_remaining
        if probe_mode:
            effective_deploy_limit = max(effective_deploy_limit, _probe_budget_floor(row["price"]))
        if buy_value > effective_deploy_limit + 1e-6 or buy_value > cash_remaining + 1e-6:
            continue
        deploy_remaining = max(0.0, effective_deploy_limit - buy_value)
        cash_remaining -= buy_value
        rationale = f"score={score:.2f}; budget={target_budget:.0f}; {sizing.reasoning}"
        if probe_mode:
            rationale = f"macro_probe=HIGH; {rationale}"
        proposals.append({
            "account_id": "paper_easyths",
            "code": code,
            "name": candidate.get("name") or code,
            "price": round(row["price"], 2),
            "shares": shares,
            "budget": round(target_budget, 2),
            "buy_value": round(buy_value, 2),
            "weight_pct": round(buy_value / total_assets * 100, 2) if total_assets > 0 else 0.0,
            "score": round(score, 4),
            "confidence": round(confidence, 3),
            "risk_level": row["risk_level"],
            "cvar": row["cvar"],
            "source": "morning_plan",
            "rationale": rationale,
        })

    return proposals



def run_quant(holdings: list) -> dict:
    """对所有持仓运行量化引擎"""
    quant = {'per_stock': {}, 'summary': {}}
    cvars = []
    garchs = []

    for h in holdings:
        code = h['code']
        if h.get('error') or h.get('n_days', 0) < 20:
            continue
        try:
            import io, contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                import io as _io, contextlib as _cl
            with _cl.redirect_stdout(_io.StringIO()):
                import io as _io2, contextlib as _cl2
            with _cl2.redirect_stdout(_io2.StringIO()):
                records = fetch_kline_baostock(code, "20260101", datetime.now().strftime("%Y%m%d"))
            if not records:
                continue
            closes = [float(r['收盘']) for r in records]

            cvar = calc_cvar(closes)
            mom = calc_multi_momentum(closes)
            garch = calc_garch_vol(closes)
            mdd = calc_max_drawdown(closes)

            quant['per_stock'][code] = {
                'cvar': round(cvar * 100, 2) if cvar is not None else None,
                'momentum_20d': mom.get('20d') if mom else None,
                'momentum_5d': mom.get('5d') if mom else None,
                'consistency': mom.get('consistency') if mom else None,
                'garch_ann_vol': round(garch['ann_vol'] * 100, 1) if garch and garch.get('converged') else None,
                'vol_regime': garch.get('vol_regime') if garch and garch.get('converged') else None,
                'max_drawdown': mdd,
            }
            if cvar is not None:
                cvars.append(cvar * 100)
            if garch and garch.get('converged'):
                garchs.append(garch['ann_vol'] * 100)
        except Exception:
            pass

    if cvars:
        quant['summary']['avg_cvar'] = round(sum(cvars) / len(cvars), 2)
        quant['summary']['worst_cvar'] = round(min(cvars), 2)
    if garchs:
        quant['summary']['avg_garch_vol'] = round(sum(garchs) / len(garchs), 1)

    return quant


def check_constraints(holdings: list, cash: float, total_assets: float, quant: dict) -> list:
    """硬约束检查"""
    try:
        from core.constraints import check_all
        feature_snapshot = load_feature_snapshot()
        results = check_all(holdings, cash, total_assets, quant, feature_snapshot=feature_snapshot)
        return [{'check': r[0], 'pass': r[1], 'message': r[2]} for r in results]
    except ImportError:
        # Fallback: basic checks if constraints.py not available
        results = []
        for h in holdings:
            code = h['code']
            ratio = (h['price'] * h['shares']) / total_assets if total_assets > 0 else 0
            if ratio > 0.30:
                results.append({'check': 'position_limit', 'pass': False,
                              'message': f"{code}仓位{ratio:.0%}>30%"})
        if cash / total_assets < 0.05:
            results.append({'check': 'cash_buffer', 'pass': False,
                          'message': f"现金不足5%"})
        return results


def main():
    t0 = time.time()

    # Step 1: 持仓
    holdings, cash, total_assets = load_holdings()

    # Step 2: 候选
    candidates = load_candidates()
    new_candidates = [c for c in candidates if c['code'] not in [h['code'] for h in holdings]]

    feature_snapshot = load_feature_snapshot()
    feature_snapshot = augment_feature_snapshot_for_candidates(feature_snapshot, new_candidates)
    quant = run_quant(holdings)

    # Step 4: 硬约束
    constraints = check_constraints(holdings, cash, total_assets, quant)

    # Step 5: 建议级别
    blocked = any(not c['pass'] for c in constraints)
    cvars = [q.get('cvar') for q in quant['per_stock'].values() if q.get('cvar') is not None]
    cvar_warning = any(c is not None and c < -5 for c in cvars)

    if blocked:
        recommendation = "BLOCKED"
    elif cvar_warning:
        recommendation = "CAUTION"
    else:
        recommendation = "READY"

    output = {
        'generated_at': datetime.now().isoformat(),
        'holdings': holdings,
        'cash': round(cash, 2),
        'total_assets': round(total_assets, 2),
        'position_ratio_pct': round((total_assets - cash) / total_assets * 100, 1) if total_assets > 0 else 0,
        'candidates': new_candidates[:5],
        'missing_feature_candidates': [c['code'] for c in new_candidates if c.get('code') not in (feature_snapshot.get('per_stock') or {})],
        'constraints': constraints,
        'quant_summary': quant['summary'],
        'quant_per_stock': quant['per_stock'],
        'recommendation': recommendation,
        'elapsed_sec': round(time.time() - t0, 1),
    }

    try:
        from apps.intraday_common import apply_macro_risk
        output = apply_macro_risk(output, slot="morning", scan_news=True)
    except Exception:
        pass

    output['buy_proposals'] = allocate_buy_candidates(
        holdings,
        cash,
        total_assets,
        new_candidates,
        feature_snapshot,
        output.get('event_risk'),
    )
    try:
        from strategy_validation import record_plan_candidates
        output['strategy_validation_record'] = record_plan_candidates(output, feature_snapshot)
    except Exception as exc:
        output['strategy_validation_record'] = {'ok': False, 'error': str(exc)[:200]}
    output['portfolio_buy_plan'] = {
        'account_id': 'paper_easyths',
        'proposal_count': len(output['buy_proposals']),
        'planned_buy_value': round(sum(p.get('buy_value', 0.0) for p in output['buy_proposals']), 2),
        'tickers': [p.get('code') for p in output['buy_proposals']],
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    # --save 支持
    if "--save" in sys.argv:
        idx = sys.argv.index("--save")
        path = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "/tmp/morning_output.json"
        import subprocess
        r = subprocess.run([sys.executable, __file__], capture_output=True, text=True)
        with open(path, 'w') as f:
            f.write(r.stdout)
        print(f"Saved to {path}", file=sys.stderr)
    else:
        main()

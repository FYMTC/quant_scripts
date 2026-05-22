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

FEATURE_SNAPSHOT_PATH = "/config/quant_scripts/data/feature_snapshot.json"


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
    path = "/config/quant_scripts/data/screener_top15.json"
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

    # Step 3: 量化
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

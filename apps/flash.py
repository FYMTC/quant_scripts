#!/config/quant_env/bin/python3
"""
apps/flash.py — 开盘闪电战（v4.0 代码管线）

替代 09:30 cron 长 prompt。聚焦开盘30分钟：
  开盘缺口检测 · 冲高回落识别 · 假突破预警 · 硬约束 · 量化上下文。

输出契约（与 morning.py 对称）:
  stdout: JSON {
    holdings: [{code, name, price, open, gap_pct, change_pct, pnl, ...}],
    alerts: [{code, type, message}],
    constraints: [{check, pass, message}],
    quant: {cvar, garch, momentum},
    candidates: [...],
    recommendation: "READY" | "CAUTION" | "BLOCKED"
  }

用法:
  python apps/flash.py
  python apps/flash.py --save FILE
"""

import sys, os, json, io, contextlib, time
from datetime import datetime
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from stock_kb import StockKB
from data_converter import fetch_kline_baostock
from risk_metrics import calc_cvar, calc_multi_momentum, calc_garch_vol, calc_max_drawdown


def fetch_live_quotes(codes: list) -> dict:
    """从 market_data 取实时行情"""
    try:
        from market_data import fetch_quotes_batch
        return fetch_quotes_batch(codes)
    except Exception:
        return {}


def load_holdings() -> tuple:
    """DB 持仓 + 实时行情"""
    kb = StockKB()
    pf = kb.read_portfolio_truth()
    positions = pf.get("positions", {})
    cash = pf.get("cash", 0)

    codes = list(positions.keys())
    quotes = fetch_live_quotes(codes) if codes else {}

    holdings = []
    for code, info in positions.items():
        q = quotes.get(code, {})
        price = q.get('price', 0)
        open_price = q.get('open', 0)
        pre_close = q.get('pre_close', 0)
        high = q.get('high', 0)
        low = q.get('low', 0)
        vol = q.get('vol', 0)

        # 开盘缺口
        gap_pct = ((open_price - pre_close) / pre_close * 100) if pre_close > 0 and open_price > 0 else 0

        # 日内振幅
        intraday_range = ((high - low) / open_price * 100) if open_price > 0 and high > 0 else 0

        # 冲高回落检测
        pullback = ((high - price) / high * 100) if high > 0 and price > 0 and high > price else 0

        pnl = (price - info['cost']) * info['shares'] if price > 0 else 0
        pnl_pct = (price - info['cost']) / info['cost'] * 100 if info['cost'] > 0 and price > 0 else 0

        holdings.append({
            'code': code,
            'name': info['name'],
            'shares': info['shares'],
            'cost': round(info['cost'], 2),
            'price': round(price, 2),
            'open': round(open_price, 2),
            'pre_close': round(pre_close, 2),
            'high': round(high, 2),
            'low': round(low, 2),
            'gap_pct': round(gap_pct, 2),
            'change_pct': round((price - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0,
            'intraday_range_pct': round(intraday_range, 2),
            'pullback_from_high_pct': round(pullback, 2),
            'volume': vol,
            'pnl': round(pnl, 2) if pnl else 0,
            'pnl_pct': round(pnl_pct, 2) if pnl_pct else 0,
        })

    total_market = sum(h['price'] * h['shares'] for h in holdings if h['price'] > 0)
    total_assets = total_market + cash

    return holdings, cash, total_assets


def detect_alerts(holdings: list, quant_per_stock: dict) -> list:
    """开盘异动告警"""
    alerts = []

    for h in holdings:
        code = h['code']
        name = h['name']
        q = quant_per_stock.get(code, {})

        # 高开冲高回落 = 假突破风险 (这就是今天中兴的模式)
        if h['gap_pct'] > 1.5 and h['pullback_from_high_pct'] > 1.0:
            alerts.append({
                'code': code,
                'name': name,
                'type': 'FAKE_BREAKOUT',
                'severity': 'HIGH',
                'message': (f"⚠️ {name} 高开{h['gap_pct']:+.1f}%后冲高回落{h['pullback_from_high_pct']:.1f}%"
                           f"——假突破概率大，谨慎追高"),
                'gap_pct': h['gap_pct'],
                'pullback_pct': h['pullback_from_high_pct'],
                'price': h['price'],
                'high': h['high'],
            })

        # 大幅低开
        if h['gap_pct'] < -2:
            alerts.append({
                'code': code,
                'name': name,
                'type': 'GAP_DOWN',
                'severity': 'HIGH',
                'message': f"🚨 {name} 大幅低开{h['gap_pct']:+.1f}%——检查是否有突发利空",
                'gap_pct': h['gap_pct'],
            })

        # 接近前高 + GARCH高波动
        garch_vol = q.get('garch_ann_vol')
        if garch_vol and garch_vol > 50 and h['gap_pct'] > 0:
            alerts.append({
                'code': code,
                'name': name,
                'type': 'HIGH_VOL_BREAKOUT',
                'severity': 'MEDIUM',
                'message': (f"⚠️ {name} 高波动环境(GARCH={garch_vol:.0f}%)下追涨，"
                           f"建议等待确认或缩小仓位"),
            })

        # CVaR 超阈值
        cvar = q.get('cvar')
        if cvar is not None and cvar < -5:
            alerts.append({
                'code': code,
                'name': name,
                'type': 'CVAR_WARNING',
                'severity': 'MEDIUM',
                'message': f"⚠️ {name} CVaR={cvar:+.1f}%超过-5%告警线",
            })

    return alerts


def run_quant(holdings: list) -> dict:
    """量化引擎（与 morning.py 对称）"""
    quant = {}
    for h in holdings:
        code = h['code']
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                records = fetch_kline_baostock(code, "20260101", datetime.now().strftime("%Y%m%d"))
            if not records or len(records) < 20:
                continue
            closes = [float(r['收盘']) for r in records]

            cvar = calc_cvar(closes)
            mom = calc_multi_momentum(closes)
            garch = calc_garch_vol(closes)
            mdd = calc_max_drawdown(closes)

            # 前高阻力位
            high_60d = max(closes[-60:]) if len(closes) >= 60 else closes[-1]
            from_high = (closes[-1] - high_60d) / high_60d * 100

            quant[code] = {
                'cvar': round(cvar * 100, 2) if cvar is not None else None,
                'momentum_5d': mom.get('5d') if mom else None,
                'momentum_20d': mom.get('20d') if mom else None,
                'consistency': mom.get('consistency') if mom else None,
                'garch_ann_vol': round(garch['ann_vol'] * 100, 1) if garch and garch.get('converged') else None,
                'vol_regime': garch.get('vol_regime') if garch and garch.get('converged') else None,
                'max_drawdown': mdd,
                'high_60d': round(high_60d, 2),
                'distance_from_high_pct': round(from_high, 2),
            }
        except Exception:
            pass
    return quant


def check_constraints(holdings: list, cash: float, total_assets: float, quant: dict, alerts: list) -> list:
    """硬约束 + 假突破特殊规则"""
    results = []

    # Hard constraints from core/constraints（quant 须为 {"per_stock": dict}）
    try:
        from core.constraints import check_all
        quant_wrapped = quant if isinstance(quant, dict) and "per_stock" in quant else {"per_stock": quant}
        results = check_all(holdings, cash, total_assets, quant_wrapped)
        results = [{'check': r[0], 'pass': r[1], 'message': r[2]} for r in results]
    except ImportError:
        for h in holdings:
            ratio = (h['price'] * h['shares']) / total_assets if total_assets > 0 else 0
            if ratio > 0.30:
                results.append({'check': 'position_limit', 'pass': False,
                              'message': f"{h['code']}仓位{ratio:.0%}>30%"})

    # Flash-specific: FAKE_BREAKOUT blocks BUY for that ticker
    for alert in alerts:
        if alert['type'] == 'FAKE_BREAKOUT':
            results.append({
                'check': f"fake_breakout_{alert['code']}",
                'pass': False,
                'message': f"假突破拦截: {alert['code']} 高开冲高回落，禁止追涨"
            })

    return results


def load_candidates() -> list:
    """昨夜选股结果"""
    path = "/config/quant_scripts/data/screener_top15.json"
    if not os.path.exists(path):
        return []
    with open(path) as f:
        data = json.load(f)
    return data.get("results", [])[:5]


def main():
    t0 = time.time()

    holdings, cash, total_assets = load_holdings()
    quant = run_quant(holdings)
    alerts = detect_alerts(holdings, quant)
    constraints = check_constraints(holdings, cash, total_assets, quant, alerts)
    candidates = load_candidates()

    blocked = any(not c['pass'] for c in constraints)
    has_fake_breakout = any(a['type'] == 'FAKE_BREAKOUT' for a in alerts)
    has_cvar_warning = any(a['type'] == 'CVAR_WARNING' for a in alerts)

    if blocked:
        recommendation = "BLOCKED"
    elif has_fake_breakout or has_cvar_warning:
        recommendation = "CAUTION"
    else:
        recommendation = "READY"

    print(json.dumps({
        'generated_at': datetime.now().isoformat(),
        'holdings': holdings,
        'cash': round(cash, 2),
        'total_assets': round(total_assets, 2),
        'alerts': alerts,
        'constraints': constraints,
        'quant_per_stock': quant,
        'candidates': candidates,
        'recommendation': recommendation,
        'elapsed_sec': round(time.time() - t0, 1),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if "--save" in sys.argv:
        idx = sys.argv.index("--save")
        path = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "/tmp/flash_output.json"
        import subprocess
        r = subprocess.run([sys.executable, __file__], capture_output=True, text=True)
        with open(path, 'w') as f:
            f.write(r.stdout)
        # Echo back for cron log
        with open(path) as f:
            print(f.read(), end='')
    else:
        main()

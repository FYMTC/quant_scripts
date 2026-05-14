#!/config/quant_env/bin/python3
"""
apps/midday.py — 盘中快照 10:00（v4.0 代码管线）

替代 10:00 cron 长 prompt。开盘30分钟后快照：
  持仓变化追踪 · 方向偏离检测 · 量化刷新 · 硬约束 · 机会扫描。

与 09:30 flash 联动：对比 flash_output.json 检测趋势变化。

输出契约（与 morning/flash 对称）:
  stdout: JSON {
    holdings: [{code, name, price, vs_open, vs_flash, direction, ...}],
    alerts: [{code, type, message}],
    constraints: [{check, pass, message}],
    quant: {code: {cvar, garch, momentum, ...}},
    candidates: [...],
    recommendation: "READY" | "CAUTION" | "BLOCKED"
  }

用法:
  python apps/midday.py
  python apps/midday.py --save FILE
"""

import sys, os, json, io, contextlib, time
from datetime import datetime
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from stock_kb import StockKB
from data_converter import fetch_kline_baostock
import warnings
warnings.filterwarnings('ignore')
from risk_metrics import calc_cvar, calc_multi_momentum, calc_garch_vol, calc_max_drawdown


FLASH_JSON = "/config/quant_scripts/data/flash_output.json"
SCREENER_JSON = "/config/quant_scripts/data/screener_top15.json"


def fetch_live(codes: list) -> dict:
    """实时行情（腾讯API优先）"""
    try:
        from market_data import fetch_quotes_batch
        return fetch_quotes_batch(codes)
    except Exception:
        return {}


def load_flash() -> dict:
    """加载 09:30 闪电战上下文"""
    if not os.path.exists(FLASH_JSON):
        return {}
    try:
        with open(FLASH_JSON) as f:
            return json.load(f)
    except Exception:
        return {}


def load_holdings_and_quotes():
    """持仓 + 实时行情合并"""
    kb = StockKB()
    pf = kb.read_portfolio_truth()
    positions = pf.get("positions", {})
    cash = pf.get("cash", 0)

    codes = list(positions.keys())
    quotes = fetch_live(codes) if codes else {}

    holdings = []
    for code, info in positions.items():
        q = quotes.get(code, {})
        price = q.get('price', 0)
        open_p = q.get('open', 0)
        pre_close = q.get('pre_close', 0)
        high = q.get('high', 0)
        low = q.get('low', 0)

        if price <= 0:
            holdings.append({'code': code, 'name': info['name'], 'shares': info['shares'],
                           'cost': info['cost'], 'price': 0, 'error': '实时行情不可用'})
            continue

        pnl = (price - info['cost']) * info['shares']
        pnl_pct = (price - info['cost']) / info['cost'] * 100 if info['cost'] > 0 else 0

        holdings.append({
            'code': code,
            'name': info['name'],
            'shares': info['shares'],
            'cost': round(info['cost'], 2),
            'price': round(price, 2),
            'open': round(open_p, 2),
            'pre_close': round(pre_close, 2),
            'high': round(high, 2),
            'low': round(low, 2),
            'change_pct': round((price - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0,
            'vs_open_pct': round((price - open_p) / open_p * 100, 2) if open_p > 0 else 0,
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl_pct, 2) if pnl_pct else 0,
        })

    total_market = sum(h['price'] * h['shares'] for h in holdings if h['price'] > 0)
    total_assets = total_market + cash

    return holdings, cash, total_assets


def merge_flash_context(holdings: list, flash: dict) -> list:
    """对比 09:30 上下文，附加上午趋势"""
    if not flash:
        return holdings

    flash_holdings = {h['code']: h for h in flash.get('holdings', [])}

    for h in holdings:
        code = h['code']
        fh = flash_holdings.get(code, {})
        if not fh or h['price'] <= 0:
            continue

        open_at_flash = fh.get('open', 0)
        price_at_flash = fh.get('price', 0)

        # 从开盘以来的累计变化
        h['vs_flash_price'] = round(price_at_flash, 2) if price_at_flash else None
        h['vs_flash_pct'] = round((h['price'] - price_at_flash) / price_at_flash * 100, 2) if price_at_flash > 0 else None

        # 方向判断
        gap_at_flash = fh.get('gap_pct', 0)
        vs_open = h['vs_open_pct']

        if gap_at_flash > 1 and vs_open < -0.5:
            h['direction'] = 'REVERSAL_DOWN'  # 高开后逆转下跌
        elif gap_at_flash < -1 and vs_open > 0.5:
            h['direction'] = 'REVERSAL_UP'    # 低开后逆转上涨
        elif vs_open > 1:
            h['direction'] = 'CONTINUE_UP'
        elif vs_open < -1:
            h['direction'] = 'CONTINUE_DOWN'
        else:
            h['direction'] = 'FLAT'

        # 振幅扩大检测
        range_then = fh.get('intraday_range_pct', 0)
        range_now = (h['high'] - h['low']) / h['open'] * 100 if h['open'] > 0 else 0
        if range_now > range_then * 1.5 and range_now > 3:
            h['volatility_surge'] = True
            h['range_then'] = round(range_then, 1)
            h['range_now'] = round(range_now, 1)

    return holdings


def detect_midday_alerts(holdings: list) -> list:
    """盘中异动告警"""
    alerts = []

    for h in holdings:
        code = h['code']
        name = h['name']
        if h['price'] <= 0:
            continue

        # 方向逆转
        if h.get('direction') == 'REVERSAL_DOWN':
            alerts.append({
                'code': code, 'name': name, 'type': 'REVERSAL_DOWN',
                'severity': 'HIGH',
                'message': f"🚨 {name} 高开后逆转下跌 {h['vs_open_pct']:+.1f}%——上午趋势恶化",
            })
        elif h.get('direction') == 'REVERSAL_UP':
            alerts.append({
                'code': code, 'name': name, 'type': 'REVERSAL_UP',
                'severity': 'MEDIUM',
                'message': f"📈 {name} 低开后逆转上涨 {h['vs_open_pct']:+.1f}%——上午趋势改善",
            })

        # 振幅暴增
        if h.get('volatility_surge'):
            alerts.append({
                'code': code, 'name': name, 'type': 'VOLATILITY_SURGE',
                'severity': 'HIGH',
                'message': f"⚠️ {name} 振幅暴增({h['range_then']}%→{h['range_now']}%)——波动异常",
            })

        # 持续大幅下跌
        if h['change_pct'] < -4:
            alerts.append({
                'code': code, 'name': name, 'type': 'SHARP_DECLINE',
                'severity': 'HIGH',
                'message': f"🔴 {name} 盘中大跌 {h['change_pct']:+.1f}%，检查是否需要止损",
            })

        # 冲高回落（盘中新出现的）
        if h['high'] > 0 and h['price'] > 0:
            pullback = (h['high'] - h['price']) / h['high'] * 100
            if h['change_pct'] > 1 and pullback > 3:
                alerts.append({
                    'code': code, 'name': name, 'type': 'MIDDAY_PULLBACK',
                    'severity': 'MEDIUM',
                    'message': f"📉 {name} 盘中冲高回落{pullback:.1f}%",
                })

    return alerts


def run_quant(holdings: list) -> dict:
    """量化指标刷新"""
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

            quant[code] = {
                'cvar': round(cvar * 100, 2) if cvar is not None else None,
                'momentum_5d': mom.get('5d') if mom else None,
                'momentum_20d': mom.get('20d') if mom else None,
                'garch_ann_vol': round(garch['ann_vol'] * 100, 1) if garch and garch.get('converged') else None,
                'vol_regime': garch.get('vol_regime') if garch and garch.get('converged') else None,
            }
        except Exception:
            pass
    return quant


def check_constraints(holdings: list, cash: float, total_assets: float, quant: dict, alerts: list) -> list:
    """硬约束"""
    results = []
    try:
        from core.constraints import check_all
        quant_wrapped = quant if isinstance(quant, dict) and "per_stock" in quant else {"per_stock": quant}
        results = check_all(holdings, cash, total_assets, quant_wrapped)
        results = [{'check': r[0], 'pass': r[1], 'message': r[2]} for r in results]
    except ImportError:
        pass

    # Midday-specific: reversal blocks new BUY
    for alert in alerts:
        if alert['type'] == 'REVERSAL_DOWN':
            results.append({
                'check': f"reversal_{alert['code']}",
                'pass': False,
                'message': f"上午逆转拦截: {alert['code']} 高开后转跌，禁止买入"
            })

    return results


def load_candidates() -> list:
    if not os.path.exists(SCREENER_JSON):
        return []
    with open(SCREENER_JSON) as f:
        data = json.load(f)
    return data.get("results", [])[:5]


def main():
    t0 = time.time()

    holdings, cash, total_assets = load_holdings_and_quotes()
    flash = load_flash()
    holdings = merge_flash_context(holdings, flash)

    alerts = detect_midday_alerts(holdings)
    quant = run_quant(holdings)
    constraints = check_constraints(holdings, cash, total_assets, quant, alerts)
    candidates = load_candidates()

    blocked = any(not c['pass'] for c in constraints)
    has_reversal = any(a['type'] in ('REVERSAL_DOWN', 'SHARP_DECLINE') for a in alerts)

    if blocked:
        recommendation = "BLOCKED"
    elif has_reversal:
        recommendation = "CAUTION"
    else:
        recommendation = "READY"

    flash_time = flash.get('generated_at', 'N/A') if flash else 'N/A'

    print(json.dumps({
        'generated_at': datetime.now().isoformat(),
        'flash_context_at': flash_time,
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
        path = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "/tmp/midday_output.json"
        import subprocess
        r = subprocess.run([sys.executable, __file__], capture_output=True, text=True)
        with open(path, 'w') as f:
            f.write(r.stdout)
        with open(path) as f:
            print(f.read(), end='')
    else:
        main()

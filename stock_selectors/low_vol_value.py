#!/config/quant_env/bin/python3
"""
selectors/low_vol_value.py — 低波价值防守型策略
适合 HIGH/WATCH 级别：低波动、合理估值、正动量。
"""
import numpy as np
from datetime import datetime, timedelta
from typing import Optional, Dict

_MIN_DAYS = 60  # need more data for vol regime


def score(code: str) -> Optional[Dict]:
    from data_converter import fetch_kline_baostock
    from risk_metrics import calc_cvar, calc_multi_momentum, calc_max_drawdown, calc_garch_vol

    try:
        records = fetch_kline_baostock(
            code,
            (datetime.now() - timedelta(days=365)).strftime('%Y%m%d'),
            datetime.now().strftime('%Y%m%d'),
        )
        if not records or len(records) < _MIN_DAYS:
            return None

        closes = np.array([float(r['收盘']) for r in records])
        latest = closes[-1]

        # ── Volatility (lower = better) ──
        daily_rets = np.diff(np.log(closes))
        ann_vol = float(np.std(daily_rets) * np.sqrt(252) * 100)
        # Prefer stocks with ann_vol < 40%
        vol_score = max(0.0, 1.0 - ann_vol / 60.0) if ann_vol < 60 else 0.0

        # GARCH regime
        garch = calc_garch_vol(list(closes))
        vol_regime = garch.get('vol_regime', 'normal') if garch and garch.get('converged') else 'unknown'
        garch_vol = garch['ann_vol'] if garch and garch.get('converged') else ann_vol / 100.0

        # ── Momentum (moderate positive = best) ──
        mom_20d = (latest / closes[-21] - 1) * 100 if len(closes) >= 21 else 0
        # Prefer 5-20% in 20 days (not too hot, not cold)
        mom_score = 1.0 - abs(mom_20d - 10.0) / 15.0 if -5 <= mom_20d <= 25 else 0.0
        mom_score = max(0.0, mom_score)

        # ── Drawdown stability ──
        mdd = calc_max_drawdown(list(closes)) or 0.0
        mdd_score = max(0.0, 1.0 - abs(mdd) / 25.0)  # Prefer max drawdown < 25%

        # ── CVaR ──
        cvar = calc_cvar(list(closes))
        cvar_val = cvar * 100 if cvar is not None else -10.0
        cvar_score = max(0.0, 1.0 + cvar_val / 10.0)  # CVaR -10%→0, 0%→1

        # ── Momentum consistency ──
        mom = calc_multi_momentum(list(closes))
        consistency = mom.get('consistency', 0) if mom else 0

        # ── Composite: weight toward safety ──
        composite = (
            0.30 * vol_score +
            0.20 * mom_score +
            0.20 * mdd_score +
            0.15 * cvar_score +
            0.15 * (0.5 + consistency * 0.5)
        )
        # Bonus for low-vol regime
        if vol_regime == 'low':
            composite *= 1.1
        elif vol_regime == 'high':
            composite *= 0.8

        name = records[0].get('名称', '') or records[0].get('name', code) if records else code

        return {
            'code': code, 'name': name, 'price': round(latest, 2),
            'composite_score': round(composite, 4),
            'ann_vol': round(ann_vol, 1), 'vol_regime': vol_regime,
            'garch_vol': round(garch_vol * 100, 1),
            'mom_20d': round(mom_20d, 2), 'cvar': round(cvar_val, 2),
            'max_drawdown': round(mdd, 2), 'consistency': round(consistency, 2),
            'n_days': len(records),
        }
    except Exception:
        return None

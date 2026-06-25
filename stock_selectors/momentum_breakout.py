#!python3
"""
selectors/momentum_breakout.py — 动量突破策略
基于动量、波动率、量比、均线排列的综合评分。从 stock_screener.score_single() 中提取。
"""
import numpy as np
from datetime import datetime, timedelta
from typing import Optional, Dict

_MIN_DAYS = 20

FACTOR_WEIGHTS = {
    'momentum_20d': 0.30,
    'momentum_5d': 0.10,
    'volatility_adj': 0.15,
    'volume_ratio': 0.15,
    'ma_alignment': 0.15,
    'cvar_score': 0.15,
}


def score(code: str) -> Optional[Dict]:
    from data_converter import fetch_kline_baostock
    from risk_metrics import calc_cvar, calc_multi_momentum, calc_max_drawdown, calc_garch_vol

    try:
        records = fetch_kline_baostock(
            code,
            (datetime.now() - timedelta(days=180)).strftime('%Y%m%d'),
            datetime.now().strftime('%Y%m%d'),
        )
        if not records or len(records) < _MIN_DAYS:
            return None

        closes = np.array([float(r['收盘']) for r in records])
        volumes = np.array([float(r['成交量(手)']) for r in records])

        latest = closes[-1]

        mom_20d = (latest / closes[-21] - 1) * 100 if len(closes) >= 21 else 0
        mom_5d = (latest / closes[-6] - 1) * 100 if len(closes) >= 6 else 0

        daily_rets = np.diff(np.log(closes))
        ann_vol = float(np.std(daily_rets) * np.sqrt(252) * 100)
        vol_score = max(0.0, 1.0 - ann_vol / 80.0)

        garch = calc_garch_vol(list(closes))
        garch_vol = garch['ann_vol'] if garch and garch.get('converged') else ann_vol / 100

        vol_5d = float(np.mean(volumes[-5:])) if len(volumes) >= 5 else 0
        vol_20d = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 1
        vol_ratio = vol_5d / max(vol_20d, 1)
        vol_ratio_score = min(max((vol_ratio - 0.5) / 2, 0), 1)

        ma5 = float(np.mean(closes[-5:]))
        ma20 = float(np.mean(closes[-20:])) if len(closes) >= 20 else ma5
        ma_score = 1.0 if latest > ma5 > ma20 else (0.5 if latest > ma20 else 0.0)

        cvar = calc_cvar(list(closes))
        cvar_val = cvar * 100 if cvar is not None else -10.0
        cvar_score = max(0.0, 1.0 + cvar_val / 15.0)

        mdd = calc_max_drawdown(list(closes)) or 0.0
        mdd_penalty = min(abs(mdd) / 40.0, 1.0)

        mom = calc_multi_momentum(list(closes))
        consistency = mom.get('consistency', 0) if mom else 0

        composite = (
            FACTOR_WEIGHTS['momentum_20d'] * (mom_20d / 20 if mom_20d > 0 else mom_20d / 10) +
            FACTOR_WEIGHTS['momentum_5d'] * (mom_5d / 10 if mom_5d > 0 else mom_5d / 5) +
            FACTOR_WEIGHTS['volatility_adj'] * vol_score +
            FACTOR_WEIGHTS['volume_ratio'] * vol_ratio_score +
            FACTOR_WEIGHTS['ma_alignment'] * ma_score +
            FACTOR_WEIGHTS['cvar_score'] * cvar_score
        ) * (1 - mdd_penalty * 0.5)
        composite *= (0.8 + consistency * 0.4)

        name = records[0].get('名称', '') or records[0].get('name', code) if records else code

        return {
            'code': code, 'name': name, 'price': round(latest, 2),
            'composite_score': round(composite, 4),
            'mom_20d': round(mom_20d, 2), 'mom_5d': round(mom_5d, 2),
            'ann_vol': round(ann_vol, 1), 'garch_vol': round(garch_vol * 100, 1),
            'vol_ratio': round(vol_ratio, 2),
            'ma_alignment': 'bullish' if ma_score >= 1 else 'mixed' if ma_score >= 0.5 else 'bearish',
            'cvar': round(cvar_val, 2), 'max_drawdown': round(mdd, 2),
            'consistency': round(consistency, 2), 'n_days': len(records),
        }
    except Exception:
        return None

#!python3
"""
selectors/rdagent_factors.py — RD-Agent 因子策略
消费 RD-Agent 周末生成的 factor_library.json，用 IC 加权的稳定因子评分。
"""
import json
import os
import numpy as np
from datetime import datetime, timedelta
from typing import Optional, Dict, List

_MIN_DAYS = 20
FACTOR_LIB_PATH = "/config/qlib_data/factor_library.json"


def _load_factors() -> List[dict]:
    if not os.path.isfile(FACTOR_LIB_PATH):
        return []
    try:
        with open(FACTOR_LIB_PATH, encoding="utf-8") as f:
            lib = json.load(f)
        return lib.get("stable_new_factors") or []
    except Exception:
        return []


def score(code: str) -> Optional[Dict]:
    from data_converter import fetch_kline_baostock
    from risk_metrics import calc_cvar, calc_multi_momentum, calc_max_drawdown

    factors = _load_factors()
    try:
        records = fetch_kline_baostock(
            code,
            (datetime.now() - timedelta(days=180)).strftime('%Y%m%d'),
            datetime.now().strftime('%Y%m%d'),
        )
        if not records or len(records) < _MIN_DAYS:
            return None

        closes = np.array([float(r['收盘']) for r in records])
        latest = closes[-1]

        # ── Base metrics ──
        cvar = calc_cvar(list(closes))
        cvar_val = cvar * 100 if cvar is not None else -10.0
        mdd = calc_max_drawdown(list(closes)) or 0.0
        mom = calc_multi_momentum(list(closes))
        mom_20d = (latest / closes[-21] - 1) * 100 if len(closes) >= 21 else 0
        mom_5d = (latest / closes[-6] - 1) * 100 if len(closes) >= 6 else 0
        consistency = mom.get('consistency', 0) if mom else 0

        # ── RD-Agent factor scoring (IC-weighted) ──
        rdagent_score = 0.0
        used_factors = 0
        if factors:
            total_ic = sum(abs(f.get("avg_ic", 0)) for f in factors) or 1.0
            daily_rets = np.diff(np.log(closes)) if len(closes) > 1 else np.zeros(1)
            for f in factors:
                ic_weight = abs(f.get("avg_ic", 0)) / total_ic
                # Simplified factor calculation — in production, use the actual factor formula
                # For now, use risk-adjusted momentum as a proxy for factor quality
                factor_val = mom_20d / 20.0 if mom_20d > 0 else mom_20d / 10.0
                # Adjust by factor's IC sign (positive IC → positive weight for momentum)
                rdagent_score += ic_weight * factor_val * (1.0 if f.get("avg_ic", 0) > 0 else -0.5)
                used_factors += 1

        # ── Composite: 60% screener-style + 40% RD-Agent factors ──
        screener_score = (
            0.35 * (mom_20d / 20 if mom_20d > 0 else mom_20d / 10) +
            0.15 * (mom_5d / 10 if mom_5d > 0 else mom_5d / 5) +
            0.15 * max(0.0, 1.0 + cvar_val / 15.0) +
            0.15 * (0.8 + consistency * 0.4) +
            0.20 * max(0.0, 1.0 - abs(mdd) / 40.0)
        )
        if used_factors > 0:
            composite = 0.6 * screener_score + 0.4 * rdagent_score
        else:
            composite = screener_score

        name = records[0].get('名称', '') or records[0].get('name', code) if records else code

        return {
            'code': code, 'name': name, 'price': round(latest, 2),
            'composite_score': round(composite, 4),
            'mom_20d': round(mom_20d, 2), 'mom_5d': round(mom_5d, 2),
            'cvar': round(cvar_val, 2), 'max_drawdown': round(mdd, 2),
            'consistency': round(consistency, 2),
            'rdagent_factors_used': used_factors,
            'rdagent_weight': round(rdagent_score, 4) if used_factors else 0,
            'n_days': len(records),
        }
    except Exception:
        return None

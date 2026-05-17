#!/config/quant_env/bin/python3
"""
portfolio_de_risk.py — 组合级减仓 / 控仓计划（配合 event_calendar playbook）
"""

from __future__ import annotations

import math
from typing import Any, Dict, List

MIN_LOT = 100


def build_de_risk_plan(
    holdings: List[dict],
    total_assets: float,
    playbook: dict,
    *,
    lineage_id: str = "",
) -> dict:
    """
    根据 max_gross_exposure 生成减仓建议（100 股整数倍）。
    holdings: [{code, name, shares, price, ...}]
    """
    max_gross = float(playbook.get("max_gross_exposure") or 1.0)
    level = playbook.get("level") or "NORMAL"
    allow_buy = bool(playbook.get("allow_new_buy", True))

    if total_assets <= 0:
        return {
            "required": False,
            "level": level,
            "actions": [],
            "message": "总资产未知，跳过组合减仓计划",
        }

    market_value = sum(
        float(h.get("price") or 0) * int(h.get("shares") or 0)
        for h in holdings
        if float(h.get("price") or 0) > 0
    )
    current_gross = market_value / total_assets if total_assets > 0 else 0
    target_mv = total_assets * max_gross
    excess = market_value - target_mv

    actions: List[dict] = []
    if excess <= 0 or level in ("NORMAL", "WATCH") and current_gross <= max_gross + 0.02:
        return {
            "required": level in ("HIGH", "CRITICAL") and not allow_buy,
            "level": level,
            "current_gross_pct": round(current_gross * 100, 1),
            "target_gross_pct": round(max_gross * 100, 1),
            "actions": [],
            "message": playbook.get("message", ""),
            "lineage_id": lineage_id,
        }

    # 按市值从大到小减
    ranked = sorted(
        [h for h in holdings if float(h.get("price") or 0) > 0 and int(h.get("shares") or 0) > 0],
        key=lambda x: float(x["price"]) * int(x["shares"]),
        reverse=True,
    )
    remaining = excess
    for h in ranked:
        if remaining <= 0:
            break
        price = float(h["price"])
        shares = int(h["shares"])
        pos_val = price * shares
        if pos_val <= 0:
            continue
        # 至少减 1 手，最多减到只剩 1 手（若原>1手）
        sell_val = min(remaining, pos_val * 0.5 if level == "HIGH" else pos_val * 0.8)
        sell_shares = int(math.floor(sell_val / price / MIN_LOT) * MIN_LOT)
        if sell_shares < MIN_LOT:
            sell_shares = MIN_LOT if shares >= MIN_LOT * 2 else shares
        sell_shares = min(sell_shares, shares)
        if sell_shares <= 0:
            continue
        actions.append(
            {
                "code": h.get("code"),
                "name": h.get("name"),
                "direction": "SELL",
                "shares": sell_shares,
                "price": round(price, 2),
                "reason": f"组合控仓 {current_gross:.0%}→目标≤{max_gross:.0%} ({level})",
                "lineage_id": lineage_id,
            }
        )
        remaining -= sell_shares * price

    return {
        "required": True,
        "level": level,
        "current_gross_pct": round(current_gross * 100, 1),
        "target_gross_pct": round(max_gross * 100, 1),
        "excess_market_value": round(max(0, excess), 2),
        "actions": actions,
        "allow_new_buy": allow_buy,
        "message": playbook.get("message", ""),
        "lineage_id": lineage_id,
    }

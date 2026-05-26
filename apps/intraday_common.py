#!/config/quant_env/bin/python3
"""
intraday_common.py — 11:30 / 14:00 / 15:05 等盘中管线共享逻辑（从 midday 抽取）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

FLASH_JSON = os.path.join(os.environ.get("QUANT_RUNTIME_DATA_DIR") or "/config/quant_scripts/data", "flash_output.json")
MIDDAY_JSON = os.path.join(os.environ.get("QUANT_RUNTIME_DATA_DIR") or "/config/quant_scripts/data", "midday_output.json")
NOON_JSON = os.path.join(os.environ.get("QUANT_RUNTIME_DATA_DIR") or "/config/quant_scripts/data", "noon_output.json")
SCREENER_JSON = os.path.join(os.environ.get("QUANT_RUNTIME_DATA_DIR") or "/config/quant_scripts/data", "screener_top15.json")

# H2 Tier 1.5：高现金闲置时强制输出「持仓外」可部署标的行情，避免 LLM 仅读 3 只持仓收工
TIER15_CASH_RATIO_TRIGGER = 0.20
TIER15_CASH_ABS_TRIGGER = 8000.0
TIER15_MAX_SYMBOLS = 24


def load_json(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def fetch_live(codes: list) -> dict:
    try:
        from market_data import fetch_quotes_batch

        return fetch_quotes_batch(codes) if codes else {}
    except Exception:
        return {}


def load_holdings_and_quotes() -> Tuple[list, float, float]:
    from trade_account_context import load_portfolio_truth

    pf = load_portfolio_truth()
    positions = pf.get("positions", {})
    cash = float(pf.get("cash", 0) or 0)

    codes = list(positions.keys())
    quotes = fetch_live(codes) if codes else {}

    holdings = []
    for code, info in positions.items():
        q = quotes.get(code, {})
        price = float(q.get("price", 0) or 0)
        open_p = float(q.get("open", 0) or 0)
        pre_close = float(q.get("pre_close", 0) or 0)
        high = float(q.get("high", 0) or 0)
        low = float(q.get("low", 0) or 0)

        if price <= 0:
            holdings.append(
                {
                    "code": code,
                    "name": info["name"],
                    "shares": info["shares"],
                    "cost": info["cost"],
                    "price": 0,
                    "error": "实时行情不可用",
                }
            )
            continue

        pnl = (price - info["cost"]) * info["shares"]
        pnl_pct = (price - info["cost"]) / info["cost"] * 100 if info["cost"] > 0 else 0

        holdings.append(
            {
                "code": code,
                "name": info["name"],
                "shares": info["shares"],
                "cost": round(float(info["cost"]), 2),
                "price": round(price, 2),
                "open": round(open_p, 2),
                "pre_close": round(pre_close, 2),
                "high": round(high, 2),
                "low": round(low, 2),
                "change_pct": round((price - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0,
                "vs_open_pct": round((price - open_p) / open_p * 100, 2) if open_p > 0 else 0,
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2) if pnl_pct else 0,
            }
        )

    total_market = sum(h["price"] * h["shares"] for h in holdings if h.get("price", 0) > 0)
    total_assets = total_market + cash
    return holdings, cash, total_assets


def merge_flash_context(holdings: list, flash: dict) -> list:
    if not flash:
        return holdings

    flash_holdings = {h["code"]: h for h in flash.get("holdings", [])}

    for h in holdings:
        code = h["code"]
        fh = flash_holdings.get(code, {})
        if not fh or h.get("price", 0) <= 0:
            continue

        price_at_flash = float(fh.get("price", 0) or 0)
        h["vs_flash_price"] = round(price_at_flash, 2) if price_at_flash else None
        h["vs_flash_pct"] = (
            round((h["price"] - price_at_flash) / price_at_flash * 100, 2) if price_at_flash > 0 else None
        )

        gap_at_flash = float(fh.get("gap_pct", 0) or 0)
        vs_open = float(h.get("vs_open_pct", 0) or 0)

        if gap_at_flash > 1 and vs_open < -0.5:
            h["direction"] = "REVERSAL_DOWN"
        elif gap_at_flash < -1 and vs_open > 0.5:
            h["direction"] = "REVERSAL_UP"
        elif vs_open > 1:
            h["direction"] = "CONTINUE_UP"
        elif vs_open < -1:
            h["direction"] = "CONTINUE_DOWN"
        else:
            h["direction"] = "FLAT"

        range_then = float(fh.get("intraday_range_pct", 0) or 0)
        op = float(h.get("open", 0) or 0)
        range_now = (h["high"] - h["low"]) / op * 100 if op > 0 else 0
        if range_now > range_then * 1.5 and range_now > 3:
            h["volatility_surge"] = True
            h["range_then"] = round(range_then, 1)
            h["range_now"] = round(range_now, 1)

    return holdings


def merge_prior_snapshot(holdings: list, prior: dict, field_prefix: str) -> list:
    """将上一档 JSON 中的 holdings 价格与当前价对比（如 vs_midday_pct）。"""
    if not prior:
        return holdings
    ph = {x["code"]: x for x in prior.get("holdings", []) if isinstance(x, dict)}
    key = f"vs_{field_prefix}_pct"
    for h in holdings:
        p = ph.get(h["code"])
        if not p or h.get("price", 0) <= 0:
            continue
        ref = float(p.get("price", 0) or 0)
        if ref > 0:
            h[key] = round((h["price"] - ref) / ref * 100, 2)
    return holdings


def run_quant_flat(holdings: list) -> dict:
    from data_converter import fetch_kline_baostock
    from risk_metrics import calc_cvar, calc_multi_momentum, calc_garch_vol

    quant: Dict[str, Any] = {}
    for h in holdings:
        code = h["code"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                records = fetch_kline_baostock(code, "20260101", datetime.now().strftime("%Y%m%d"))
            if not records or len(records) < 20:
                continue
            closes = [float(r["收盘"]) for r in records]

            cvar = calc_cvar(closes)
            mom = calc_multi_momentum(closes)
            garch = calc_garch_vol(closes)

            quant[code] = {
                "cvar": round(cvar * 100, 2) if cvar is not None else None,
                "momentum_5d": mom.get("5d") if mom else None,
                "momentum_20d": mom.get("20d") if mom else None,
                "garch_ann_vol": round(garch["ann_vol"] * 100, 1) if garch and garch.get("converged") else None,
                "vol_regime": garch.get("vol_regime") if garch and garch.get("converged") else None,
            }
        except Exception:
            pass
    return quant


def check_constraints_intraday(
    holdings: list,
    cash: float,
    total_assets: float,
    quant: dict,
    alerts: list,
    block_reversal_down_buy: bool,
) -> list:
    try:
        from core.constraints import check_all

        quant_wrapped = quant if isinstance(quant, dict) and "per_stock" in quant else {"per_stock": quant}
        results = check_all(holdings, cash, total_assets, quant_wrapped)
        out = [{"check": r[0], "pass": r[1], "message": r[2]} for r in results]
    except ImportError:
        out = []

    if block_reversal_down_buy:
        for alert in alerts:
            if alert.get("type") == "REVERSAL_DOWN":
                out.append(
                    {
                        "check": f"reversal_{alert['code']}",
                        "pass": False,
                        "message": f"逆转拦截: {alert['code']} 高开后转跌，禁止买入",
                    }
                )
    return out


def load_candidates_top(n: int = 5) -> list:
    if not os.path.exists(SCREENER_JSON):
        return []
    try:
        with open(SCREENER_JSON, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("results", [])[:n]
    except Exception:
        return []


def tier15_cash_triggered(cash: float, total_assets: float) -> tuple[bool, str]:
    """现金闲置达到阈值则触发 Tier1.5 全景扫描（与 TODO H2 / rebalance_monitor 20% 口径对齐）。"""
    if total_assets and total_assets > 0:
        ratio = cash / total_assets
        if ratio >= TIER15_CASH_RATIO_TRIGGER:
            return True, f"cash_ratio={ratio:.1%}>={TIER15_CASH_RATIO_TRIGGER:.0%}"
    if cash >= TIER15_CASH_ABS_TRIGGER:
        return True, f"cash={cash:.0f}>={TIER15_CASH_ABS_TRIGGER:.0f}"
    return False, ""


def build_tier15_deploy_scan(
    holdings: list,
    cash: float,
    total_assets: float,
    *,
    screener_path: str = SCREENER_JSON,
) -> dict:
    """
    H2：当现金占比 ≥20% 或 绝对现金 ≥8000 时，合并持仓 + screener_top15 + stock_kb 监控列表，
    拉取实时行情写入 JSON，供 14:00 短 prompt 强制消费（代码层不可被模型跳过）。
    """
    ok, reason = tier15_cash_triggered(cash, total_assets)
    if not ok:
        return {
            "triggered": False,
            "trigger_reason": "",
            "policy": "Tier1.5：仅当高现金闲置时强制扩展扫描",
            "rows": [],
        }

    screener_full = load_json(screener_path) or {}
    screener_rows = list(screener_full.get("results", []) or [])[:15]

    watch_rows: List[dict] = []
    try:
        from stock_kb import StockKB

        watch_rows = StockKB().get_monitoring_list(min_level=1) or []
    except Exception:
        watch_rows = []

    codes: List[str] = []
    meta_by_code: Dict[str, Dict[str, Any]] = {}

    def _add_code(code: str, source: str, extra: Optional[dict] = None) -> None:
        if not code:
            return
        if code in meta_by_code:
            if extra:
                prev = meta_by_code[code]
                if prev.get("composite_score") is None and extra.get("composite_score") is not None:
                    prev["composite_score"] = extra.get("composite_score")
                if prev.get("attention_level") is None and extra.get("attention_level") is not None:
                    prev["attention_level"] = extra.get("attention_level")
                if (not prev.get("name")) or prev.get("name") == code:
                    prev["name"] = extra.get("name") or prev.get("name") or code
            return
        codes.append(code)
        meta_by_code[code] = {"source": source, **(extra or {})}

    for h in holdings:
        c = str(h.get("code") or "")
        if c:
            _add_code(
                c,
                "holding",
                {
                    "name": h.get("name") or c,
                    "attention_level": None,
                    "composite_score": None,
                },
            )

    for r in screener_rows:
        c = str(r.get("code") or "")
        if not c:
            continue
        _add_code(
            c,
            "screener",
            {
                "name": r.get("name") or c,
                "composite_score": r.get("composite_score"),
                "attention_level": None,
            },
        )

    for w in watch_rows:
        c = str(w.get("code") or "")
        if not c:
            continue
        _add_code(
            c,
            "watchlist",
            {
                "name": w.get("name") or c,
                "attention_level": w.get("attention_level"),
                "composite_score": None,
            },
        )

    codes = codes[:TIER15_MAX_SYMBOLS]
    quotes = fetch_live(codes)
    rows_out: List[dict] = []
    for code in codes:
        q = quotes.get(code, {}) or {}
        price = float(q.get("price", 0) or 0)
        pre_close = float(q.get("pre_close", 0) or 0)
        open_p = float(q.get("open", 0) or 0)
        meta = meta_by_code.get(code, {})
        chg = round((price - pre_close) / pre_close * 100, 2) if pre_close > 0 else None
        vs_open = round((price - open_p) / open_p * 100, 2) if open_p > 0 else None
        row: Dict[str, Any] = {
            "code": code,
            "name": meta.get("name") or code,
            "source": meta.get("source"),
            "price": round(price, 2) if price > 0 else None,
            "change_pct": chg,
            "vs_open_pct": vs_open,
            "composite_score": meta.get("composite_score"),
            "attention_level": meta.get("attention_level"),
        }
        if price <= 0:
            row["error"] = "实时行情不可用"
        rows_out.append(row)

    return {
        "triggered": True,
        "trigger_reason": reason,
        "policy": "H2 Tier1.5：高现金闲置 → 强制输出 screener+自选+持仓 合并行情；Hermes 须逐只点名资金部署含义",
        "cash": round(cash, 2),
        "total_assets": round(total_assets, 2),
        "cash_ratio_pct": round(cash / total_assets * 100, 2) if total_assets and total_assets > 0 else None,
        "symbol_cap": TIER15_MAX_SYMBOLS,
        "rows": rows_out,
    }


def detect_alerts_intraday(holdings: list, *, pullback_alert: bool = True) -> list:
    alerts = []
    for h in holdings:
        code = h["code"]
        name = h["name"]
        if h.get("price", 0) <= 0:
            continue

        if h.get("direction") == "REVERSAL_DOWN":
            alerts.append(
                {
                    "code": code,
                    "name": name,
                    "type": "REVERSAL_DOWN",
                    "severity": "HIGH",
                    "message": f"🚨 {name} 高开后逆转下跌 {h['vs_open_pct']:+.1f}%",
                }
            )
        elif h.get("direction") == "REVERSAL_UP":
            alerts.append(
                {
                    "code": code,
                    "name": name,
                    "type": "REVERSAL_UP",
                    "severity": "MEDIUM",
                    "message": f"📈 {name} 低开后逆转上涨 {h['vs_open_pct']:+.1f}%",
                }
            )

        if h.get("volatility_surge"):
            alerts.append(
                {
                    "code": code,
                    "name": name,
                    "type": "VOLATILITY_SURGE",
                    "severity": "HIGH",
                    "message": f"⚠️ {name} 振幅暴增({h['range_then']}%→{h['range_now']}%)",
                }
            )

        if h["change_pct"] < -4:
            alerts.append(
                {
                    "code": code,
                    "name": name,
                    "type": "SHARP_DECLINE",
                    "severity": "HIGH",
                    "message": f"🔴 {name} 大跌 {h['change_pct']:+.1f}%",
                }
            )

        if pullback_alert and h["high"] > 0 and h["price"] > 0:
            pullback = (h["high"] - h["price"]) / h["high"] * 100
            if h["change_pct"] > 1 and pullback > 3:
                alerts.append(
                    {
                        "code": code,
                        "name": name,
                        "type": "INTRADAY_PULLBACK",
                        "severity": "MEDIUM",
                        "message": f"📉 {name} 冲高回落{pullback:.1f}%",
                    }
                )

    return alerts


def detect_close_alerts(holdings: list) -> list:
    """收盘附近：强调全日振幅与收跌。"""
    alerts = []
    for h in holdings:
        if h.get("price", 0) <= 0:
            continue
        code, name = h["code"], h["name"]
        if h["change_pct"] <= -5:
            alerts.append(
                {
                    "code": code,
                    "name": name,
                    "type": "EOD_LARGE_DROP",
                    "severity": "HIGH",
                    "message": f"全日收跌 {h['change_pct']:+.1f}%",
                }
            )
        hi, lo, op = h.get("high", 0), h.get("low", 0), h.get("open", 0)
        if op > 0:
            rng = (hi - lo) / op * 100
            if rng > 8:
                alerts.append(
                    {
                        "code": code,
                        "name": name,
                        "type": "EOD_WIDE_RANGE",
                        "severity": "MEDIUM",
                        "message": f"全日振幅 {rng:.1f}%",
                    }
                )
    return alerts


def pnl_summary_from_holdings(holdings: list, *, cash: float = 0, total_assets: float = 0) -> dict:
    """当日收盘口径：浮动盈亏合计 + 仓位市值（与 prompt 中 pnl_summary 对齐）。"""
    rows = [h for h in holdings if isinstance(h, dict) and h.get("price", 0) > 0]
    if not rows:
        return {
            "positions": 0,
            "total_pnl": None,
            "day_pnl_approx": None,
            "cash": round(cash, 2) if cash else None,
            "total_assets": round(total_assets, 2) if total_assets else None,
        }
    total_pnl = sum(float(h.get("pnl", 0) or 0) for h in rows)
    mval = sum(float(h.get("price", 0)) * int(h.get("shares", 0) or 0) for h in rows)
    cost_basis = sum(float(h.get("cost", 0)) * int(h.get("shares", 0) or 0) for h in rows)
    w = round(total_pnl / cost_basis * 100, 2) if cost_basis > 0 else None
    out = {
        "positions": len(rows),
        "total_pnl": round(total_pnl, 2),
        "market_value": round(mval, 2),
        "cost_basis": round(cost_basis, 2),
        "unrealized_pnl_pct_vs_cost": w,
    }
    if cash or total_assets:
        out["cash"] = round(cash, 2)
        out["total_assets"] = round(total_assets, 2)
    return out


def recommend_from(constraints: list, alerts: list, *, caution_types: Optional[set] = None) -> str:
    blocked = any(not c["pass"] for c in constraints)
    if blocked:
        return "BLOCKED"
    caution_types = caution_types or {"REVERSAL_DOWN", "SHARP_DECLINE", "EOD_LARGE_DROP"}
    if any(a.get("type") in caution_types for a in alerts):
        return "CAUTION"
    return "READY"


def apply_macro_risk(bundle: dict, *, slot: str = "intraday", scan_news: bool = True) -> dict:
    """注入宏观/地缘评估与组合减仓计划，并收紧 recommendation。"""
    try:
        from core.engines.macro_risk import assess_and_enrich

        return assess_and_enrich(bundle, slot=slot, scan_news=scan_news)
    except Exception as e:
        bundle["event_risk_error"] = str(e)[:200]
        return bundle


def save_stdout_main(module_file: str, path: str, extra_args: Optional[List[str]] = None) -> None:
    import subprocess
    import sys

    cmd = [sys.executable, module_file]
    if extra_args:
        cmd.extend(extra_args)
    r = subprocess.run(cmd, capture_output=True, text=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(r.stdout)
    with open(path, encoding="utf-8") as f:
        print(f.read(), end="")

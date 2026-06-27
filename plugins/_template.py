"""
插件模板 — 复制为 my_factor.py 并在 quant_registry.yaml 登记。

def run(ctx: dict) -> dict:
    code = ctx["code"]
    ...
    return {"score": 0.0, "interpretation": "..."}
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def run(ctx: Dict[str, Any]) -> Dict[str, Any]:
    code = ctx.get("code", "")
    closes: List[float] = ctx.get("closes") or []
    if len(closes) < 20:
        return {"ok": False, "reason": "insufficient_bars", "code": code}

    # 示例：20日动量百分数
    ret = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] else 0.0
    return {
        "ok": True,
        "code": code,
        "plugin": "template_momentum_20d",
        "momentum_20d_pct": round(ret, 2),
        "interpretation": f"{code} 20日动量 {ret:+.1f}%",
    }


if __name__ == "__main__":
    import json
    import sys
    sys.path.insert(0, "/root/ai_trading_package/quant/quant_scripts")
    from data_converter import fetch_kline_baostock
    from datetime import date

    code = sys.argv[1] if len(sys.argv) > 1 else "000001"
    end = date.today().strftime("%Y%m%d")
    start = date.today().replace(year=date.today().year - 1).strftime("%Y%m%d")
    rec = fetch_kline_baostock(code, start, end)
    closes = [float(r["收盘"]) for r in rec] if rec else []
    print(json.dumps(run({"code": code, "closes": closes}), ensure_ascii=False, indent=2))

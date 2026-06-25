#!python3
"""
落盘周六共享数据 weekend_data.json（Option C）

供「周末周报」「自选股池维护」两个 job 共用：prompt 只读该文件 + 四步门禁，禁止 bash 取数。

字段:
  - generated_at
  - portfolio: read_portfolio_truth() 摘要
  - positions_active: get_active_positions 列表（可序列化字段）
  - watchlist: get_monitoring_list(min_level=1) 与 stock_kb list 默认一致
  - factor_library: /config/qlib_data/factor_library.json（过大则截断摘要）
  - rdagent_preflight_text: RD-Agent 子进程 stdout 全文（由调用方传入文件路径）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

FACTOR_LIBRARY = "/config/qlib_data/factor_library.json"
DEFAULT_OUT = "/config/quant_scripts/data/weekend_data.json"


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def _load_factor_library(max_chars: int = 350_000) -> dict:
    if not os.path.exists(FACTOR_LIBRARY):
        return {"_missing": True, "path": FACTOR_LIBRARY}
    try:
        with open(FACTOR_LIBRARY, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        if len(raw) <= max_chars:
            return json.loads(raw)
        # 过大：只解析前 max_chars 尝试失败则返回摘要
        try:
            return json.loads(raw[:max_chars])
        except json.JSONDecodeError:
            return {
                "_truncated": True,
                "path": FACTOR_LIBRARY,
                "size_bytes": len(raw.encode("utf-8", errors="replace")),
                "head": raw[:8000],
            }
    except Exception as e:
        return {"_error": str(e), "path": FACTOR_LIBRARY}


def _read_rdagent_text(path: Optional[str]) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def build_bundle(*, rdagent_text_file: Optional[str] = None, rdagent_text: str = "") -> dict:
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from stock_kb import StockKB
    from trade_accounts import desk_primary_account
    from trade_account_context import load_account_snapshot

    kb = StockKB()
    watch = kb.get_monitoring_list(min_level=1)

    # ── v5.14 (2026-06-06)：EasyTHS live 是唯一持仓真相源（Windows 端 upstream API）──
    easyths_positions: List[dict] = []
    easyths_cash = 0.0
    primary_account = desk_primary_account() or "live_easyths"
    try:
        snap = load_account_snapshot(primary_account)
        err = snap.get("error")
        if err:
            print(f"[weekend_data_export] WARN live snapshot error: {err}", file=sys.stderr)
        for row in (snap.get("positions") or []):
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "").strip()
            if not code:
                continue
            easyths_positions.append({
                "code": code,
                "name": row.get("name") or code,
                "shares": row.get("shares", 0),
                "cost": round(float(row.get("cost") or 0), 4),
                "total_cost": round(float((row.get("cost") or 0) * (row.get("shares") or 0)), 2),
                "source": "easyths_live",
            })
        easyths_cash = float(snap.get("cash") or 0.0)
    except Exception as exc:
        print(f"[weekend_data_export] WARN live snapshot failed: {exc}", file=sys.stderr)

    # stock_kb 仍用于历史交易/自选池，但持仓以 EasyTHS live 为准
    kb_positions = kb.get_active_positions()
    portfolio = kb.read_portfolio_truth()
    # 用 EasyTHS live 数据覆盖 portfolio 中的持仓和现金
    if easyths_positions:
        portfolio["positions"] = easyths_positions
        portfolio["cash"] = round(easyths_cash, 2)
        portfolio["position_source"] = "easyths_live"
        portfolio["account_id"] = primary_account
    else:
        portfolio["position_source"] = "stock_kb"

    text = rdagent_text or _read_rdagent_text(rdagent_text_file)
    if len(text) > 1_200_000:
        text = text[:1_200_000] + "\n…[truncated rdagent_preflight_text]…"

    return {
        "generated_at": datetime.now().isoformat(),
        "portfolio": _json_safe(portfolio),
        "positions_active": _json_safe(easyths_positions if easyths_positions else kb_positions),
        "watchlist": _json_safe(watch),
        "factor_library": _load_factor_library(),
        "rdagent_preflight_text": text,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Export weekend_data.json for Saturday crons.")
    ap.add_argument("--save", default=DEFAULT_OUT, help="Output JSON path")
    ap.add_argument("--rdagent-text-file", default=None, help="Path to UTF-8 file with RD-Agent stdout")
    ap.add_argument("--rdagent-text", default="", help="Inline RD-Agent text (small runs only)")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.save) or ".", exist_ok=True)
    bundle = build_bundle(rdagent_text_file=args.rdagent_text_file, rdagent_text=args.rdagent_text or "")
    with open(args.save, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2)
    print(f"OK wrote {args.save}", file=sys.stderr)


if __name__ == "__main__":
    main()

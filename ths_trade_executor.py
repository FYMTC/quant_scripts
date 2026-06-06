#!/config/quant_env/bin/python3
"""
ths_trade_executor.py — Hermes v5 执行层：TradeClient → EasyTHS

用户微信确认 trade_outbox 后，由 Hermes 调用本脚本向 EasyTHS 下单，
并可选写入 stock_kb。

配置：/config/quant_scripts/data/easyths_trade.yaml
环境变量覆盖：EASYTHS_HOST, EASYTHS_PORT, EASYTHS_API_KEY, EASYTHS_EXPECTED_MODE
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

DEFAULT_CONFIG = Path("/config/quant_scripts/data/easyths_trade.yaml")
EXAMPLE_CONFIG = Path("/config/quant_scripts/easyths_trade.example.yaml")
DEFAULT_STATE_PATH = Path("/config/quant_scripts/data/agent_state.json")
QUANT_ROOT = Path(__file__).resolve().parent


def state_path() -> Path:
    data_dir = os.environ.get("QUANT_RUNTIME_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir) / "agent_state.json"
    return Path(os.environ.get("QUANT_AGENT_STATE_PATH", str(DEFAULT_STATE_PATH)))


def load_trade_config(path: Optional[Path] = None) -> Dict[str, Any]:
    cfg_path = path or Path(os.environ.get("EASYTHS_TRADE_CONFIG", str(DEFAULT_CONFIG)))
    if not cfg_path.is_file():
        example = EXAMPLE_CONFIG
        raise FileNotFoundError(
            f"缺少交易配置 {cfg_path}，请复制 {example} 并修改"
        )
    with cfg_path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg["host"] = os.environ.get("EASYTHS_HOST", cfg.get("host", "127.0.0.1"))
    cfg["port"] = int(os.environ.get("EASYTHS_PORT") or cfg.get("port") or 7649)
    cfg["api_key"] = os.environ.get("EASYTHS_API_KEY", cfg.get("api_key", ""))
    cfg["expected_mode"] = os.environ.get(
        "EASYTHS_EXPECTED_MODE", cfg.get("expected_mode", "paper")
    )
    return cfg


def build_client(cfg: Dict[str, Any]):
    easyths_root = Path(cfg.get("easyths_path", "/config/easyths"))
    if str(easyths_root) not in sys.path:
        sys.path.insert(0, str(easyths_root))
    from easyths import TradeClient  # type: ignore[attr-defined]

    return TradeClient(
        host=cfg["host"],
        port=int(cfg["port"]),
        api_key=cfg.get("api_key") or "",
        timeout=float(cfg.get("timeout", 60)),
        scheme=cfg.get("scheme", "http"),
    )


def verify_server_mode(client, expected_mode: str) -> Dict[str, Any]:
    """
    校验服务端模式；返回模式信息载荷。

    兼容两个版本：
    - fork（easyths <1.7）：有 /api/v1/system/mode 端点，data 含 "mode": "paper"|"live"
    - upstream（easyths >=1.7）：没有 mode 概念，所有调用都走 xiadan.exe 实盘
      - /api/v1/system/mode 返回 404 时，降级为读 /api/v1/system/status
      - expected_mode 为 "live"/""/"any" 时不报错；为 "paper" 时 WARNING（继续）
    """
    try:
        resp = client._request("GET", "/api/v1/system/mode")
    except Exception:
        # upstream 没有 mode 端点 — 降级
        try:
            status = client._request("GET", "/api/v1/system/status")
            data: Dict[str, Any] = (status or {}).get("data") or {}
            data.setdefault("mode", "live")  # upstream 默认就是 live
            data["mode_source"] = "system_status"
        except Exception:
            data = {"mode": "live", "mode_source": "fallback"}
        data["_mode_endpoint_missing"] = True
        if expected_mode and expected_mode not in ("", "any", "live"):
            print(
                f"[ths_trade_executor] WARNING: 服务端无 /api/v1/system/mode，"
                f"无法校验 expected_mode={expected_mode}；按 upstream 默认 live 继续。",
                file=sys.stderr,
            )
        return data
    data = (resp or {}).get("data") or {}
    actual = data.get("mode", "unknown")
    if expected_mode and actual != expected_mode:
        raise RuntimeError(
            f"EasyTHS 模式不匹配：期望 {expected_mode}，实际 {actual}。"
            "请检查服务端 TRADING_MODE 与 easyths_trade.yaml 的 expected_mode。"
        )
    return data


def execute_trade(
    code: str,
    direction: str,
    *,
    price: Optional[float] = None,
    shares: int,
    use_market: bool = False,
    config: Optional[Dict[str, Any]] = None,
    wait_timeout: float = 30.0,
) -> Dict[str, Any]:
    """向 EasyTHS 提交单笔买卖并等待结果。"""
    cfg = config or load_trade_config()
    direction = direction.upper()
    if direction not in ("BUY", "SELL"):
        return {"ok": False, "error": f"invalid direction: {direction}"}
    if shares <= 0:
        return {"ok": False, "error": "shares must be positive"}

    client = build_client(cfg)
    verify_server_mode(client, cfg.get("expected_mode", ""))

    if use_market or price is None:
        if direction == "BUY":
            op_result = client.market_buy(code, shares, timeout=wait_timeout)
        else:
            op_result = client.market_sell(code, shares, timeout=wait_timeout)
    else:
        if direction == "BUY":
            op_result = client.buy(code, price, shares, timeout=wait_timeout)
        else:
            op_result = client.sell(code, price, shares, timeout=wait_timeout)

    return {
        "ok": bool(op_result.get("success")),
        "direction": direction,
        "code": code,
        "price": price,
        "shares": shares,
        "result": op_result,
        "message": op_result.get("message"),
    }


def _load_outbox_request(request_id: str) -> Optional[Dict[str, Any]]:
    path = state_path()
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as f:
        state = json.load(f)
    for row in state.get("pending_trade_requests") or []:
        if row.get("request_id") == request_id:
            return row
    return None


def execute_from_outbox(
    request_id: str,
    *,
    record_kb: bool = True,
    gate_note: str = "",
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """根据 trade_outbox 的 request_id 执行下单并可选记账。"""
    row = _load_outbox_request(request_id)
    if not row:
        return {"ok": False, "error": f"request_id not found: {request_id}"}
    if row.get("status") not in ("pending", "resolved"):
        return {
            "ok": False,
            "error": f"request status={row.get('status')}，不可执行",
        }

    code = row["code"]
    direction = row["direction"]
    shares = int(row.get("shares") or 0)
    price = row.get("price")
    use_market = price is None

    cfg = load_trade_config(config_path)
    out = execute_trade(
        code,
        direction,
        price=float(price) if price is not None else None,
        shares=shares,
        use_market=use_market,
        config=cfg,
    )
    out["account_id"] = row.get("account_id")
    if not out.get("ok"):
        return out

    if record_kb:
        try:
            sys.path.insert(0, str(QUANT_ROOT))
            from stock_kb import StockKB
            from trade_accounts import should_update_symbol_book, stock_kb_book_mode

            aid = row.get("account_id") or ""
            kb_mode = stock_kb_book_mode(aid) if aid else "symbol"
            kb = StockKB()
            fill = (out.get("result") or {}).get("data") or {}
            fill_price = fill.get("price") or fill.get("fill_price") or price
            dg = row.get("decision_gate") or {}
            signal_source = str(dg.get("source") or row.get("signal_id") or "unknown")
            tid = kb.record_trade(
                code,
                direction,
                float(fill_price) if fill_price else 0.0,
                shares,
                rationale=gate_note or row.get("gate_summary") or "easyths execute",
                decision_process=f"lineage:{row.get('lineage_id') or '-'}",
                signal_source=signal_source,
                account_id=aid,
                update_symbol_book=should_update_symbol_book(aid) if aid else True,
            )
            out["stock_kb_trade_id"] = tid
            out["stock_kb_book_mode"] = kb_mode
        except Exception as exc:
            out["stock_kb_error"] = str(exc)

    row["execution"] = out
    row["executed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    path = state_path()
    with path.open(encoding="utf-8") as f:
        state = json.load(f)
    for i, p in enumerate(state.get("pending_trade_requests") or []):
        if p.get("request_id") == request_id:
            state["pending_trade_requests"][i] = row
            break
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Hermes → EasyTHS 交易执行")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("execute", help="直接下单")
    ex.add_argument("code")
    ex.add_argument("direction", choices=["BUY", "SELL"])
    ex.add_argument("--price", type=float)
    ex.add_argument("--shares", type=int, required=True)
    ex.add_argument("--market", action="store_true")

    fo = sub.add_parser("from-outbox", help="按 trade_outbox request_id 执行")
    fo.add_argument("request_id")
    fo.add_argument("--no-record-kb", action="store_true")
    fo.add_argument("--config", type=Path, default=None, help="easyths_trade.yaml 路径")

    chk = sub.add_parser("check", help="检查 EasyTHS 连通性与模式")
    args = ap.parse_args()

    if args.cmd == "check":
        cfg = load_trade_config()
        client = build_client(cfg)
        health = client.health_check()
        try:
            mode = client._request("GET", "/api/v1/system/mode")
            mode_source = "system_mode"
        except Exception:
            mode = client._request("GET", "/api/v1/system/status")
            mode_source = "system_status"
        print(
            json.dumps(
                {
                    "ok": True,
                    "config_expected_mode": cfg.get("expected_mode"),
                    "mode_source": mode_source,
                    "health": health,
                    "mode": mode,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.cmd == "execute":
        result = execute_trade(
            args.code,
            args.direction,
            price=args.price,
            shares=args.shares,
            use_market=args.market,
        )
    else:
        result = execute_from_outbox(
            args.request_id,
            record_kb=not args.no_record_kb,
            config_path=args.config,
        )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()

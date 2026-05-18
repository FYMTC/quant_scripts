"""smart_guard 按操盘账户加载监控池与持仓（与 trade_accounts 绑定）。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent
DEFAULT_GUARD = ROOT / "guard_config.json"
DEFAULT_POSITION_CACHE = ROOT / "position_cache.json"


def resolve_guard_account_id() -> str:
    """守护进程使用的账户：环境变量 > Desk 主账户 > 首个 active > manual。"""
    env = os.environ.get("GUARD_ACCOUNT_ID", "").strip()
    if env:
        return env
    try:
        from trade_accounts import desk_primary_account, hermes_trading_active, resolve_trading_account

        active = hermes_trading_active()
        if active:
            primary = desk_primary_account()
            if primary and primary in active:
                return primary
            if len(active) == 1:
                return active[0]
        return resolve_trading_account()
    except Exception:
        return "manual_wechat"


def _paths_for_account(account_id: str) -> Dict[str, Optional[Path]]:
    from trade_accounts import get_account

    acct = get_account(account_id)
    g = acct.get("guard_config_path") or str(DEFAULT_GUARD)
    p = acct.get("position_cache_path")
    return {
        "guard_config": Path(g),
        "position_cache": Path(p) if p else None,
    }


def _positions_from_snapshot(snap: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for p in snap.get("positions") or []:
        code = p.get("code")
        if not code:
            continue
        out[code] = {
            "name": p.get("name", code),
            "shares": p.get("shares") or 0,
            "cost": p.get("cost"),
            "market_value": p.get("market_value"),
            "_account_id": snap.get("account_id"),
        }
    return out


def load_guard_bundle(account_id: Optional[str] = None) -> Dict[str, Any]:
    """返回 smart_guard 用的 config dict（含 positions / watch_list）。"""
    aid = account_id or resolve_guard_account_id()
    paths = _paths_for_account(aid)
    cfg_path = paths["guard_config"]
    if not cfg_path.is_file():
        raise FileNotFoundError(f"guard config missing for {aid}: {cfg_path}")

    with cfg_path.open(encoding="utf-8") as f:
        cfg = json.load(f)

    cfg["guard_account_id"] = aid
    cfg["guard_config_path"] = str(cfg_path)

    from trade_accounts import get_account

    acct = get_account(aid)
    pos_src = (acct.get("position_source") or "").lower()

  # 持仓：paper 从 EasyTHS 快照；manual 从 position_cache
    if pos_src in ("easyths", "easyths_paper", "paper"):
        try:
            from trade_account_context import load_account_snapshot

            snap = load_account_snapshot(aid)
            cfg["positions"] = _positions_from_snapshot(snap)
            cfg["cash"] = (snap.get("summary") or {}).get("cash", 0)
            cfg["available_capital"] = cfg["cash"]
            cfg["position_source_note"] = "easyths_snapshot"
        except Exception as exc:
            cfg["positions"] = {}
            cfg["position_load_error"] = str(exc)[:300]
    else:
        cache = paths["position_cache"] or DEFAULT_POSITION_CACHE
        try:
            if Path(cache).is_file():
                with open(cache, encoding="utf-8") as f:
                    pos_data = json.load(f)
                cfg["positions"] = pos_data.get("positions", {})
                cfg["cash"] = pos_data.get("cash", 0)
                cfg["available_capital"] = pos_data.get("cash", 0)
                cfg["position_source_note"] = str(cache)
        except Exception:
            cfg.setdefault("positions", {})

    if "watch_list" not in cfg or not cfg["watch_list"]:
        cfg["watch_list"] = cfg.get("monitored_codes") or {}

    return {"account_id": aid, "config": cfg}

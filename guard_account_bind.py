"""smart_guard 按操盘账户加载监控池与持仓（热加载，无需重启进程）。"""

from __future__ import annotations

from system_config import cfg

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parent
DEFAULT_GUARD = ROOT / "guard_config.json"
DEFAULT_POSITION_CACHE = ROOT / "position_cache.json"
STATE_PATH = ROOT / "data" / "trade_accounts_state.json"
PAPER_POSITION_REFRESH_SEC = int(os.environ.get("GUARD_PAPER_POSITION_REFRESH_SEC", "30"))


def resolve_guard_account_id() -> str:
    """守护进程使用的账户：GUARD_ACCOUNT_ID 固定覆盖 > Desk 主账户 > 单 active。"""
    env = os.environ.get("GUARD_ACCOUNT_ID", "").strip()
    if env:
        return env
    from trade_accounts import resolve_trading_account

    return resolve_trading_account()


def _file_mtime(path: Optional[Path]) -> float:
    if not path:
        return 0.0
    p = Path(path)
    if p.is_file():
        return os.path.getmtime(p)
    return 0.0


def bind_signature(account_id: Optional[str] = None) -> Tuple:
    """用于 smart_guard 判断是否需要重载配置（含切换操盘主账户）。"""
    aid = account_id or resolve_guard_account_id()
    paths = _paths_for_account(aid)
    guard_mtime = _file_mtime(paths["guard_config"])
    state_mtime = _file_mtime(STATE_PATH)
    pos_mtime = _file_mtime(paths["position_cache"] or DEFAULT_POSITION_CACHE)

    from trade_accounts import get_account

    pos_src = (get_account(aid).get("position_source") or "").lower()
    paper_bucket = 0
    if pos_src in ("easyths", "easyths_paper", "paper"):
        paper_bucket = int(time.time()) // max(PAPER_POSITION_REFRESH_SEC, 5)

    env_pin = os.environ.get("GUARD_ACCOUNT_ID", "").strip()
    return (aid, guard_mtime, state_mtime, pos_mtime, paper_bucket, env_pin)


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
    cfg["bind_signature"] = bind_signature(aid)

    from trade_accounts import get_account

    acct = get_account(aid)
    pos_src = (acct.get("position_source") or "").lower()

    if pos_src not in ("easyths", "easyths_paper", "paper"):
        raise ValueError(f"unsupported position_source for guard runtime: {pos_src or 'missing'}")
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

    root_cfg = {}
    root_cfg_path = Path(ROOT) / "guard_config.json"
    if root_cfg_path.is_file() and str(cfg_path) != str(root_cfg_path):
        try:
            with root_cfg_path.open(encoding="utf-8") as f:
                root_cfg = json.load(f)
        except Exception:
            root_cfg = {}

    watch_list_original = cfg.get("watch_list")
    fallback_from_root = bool(root_cfg) and not (cfg.get("watch_list") or cfg.get("monitored_codes") or cfg.get("signals"))
    monitored_codes = cfg.get("monitored_codes") or (root_cfg.get("monitored_codes") or {} if fallback_from_root else {})
    signals = cfg.get("signals") or (root_cfg.get("signals") or [] if fallback_from_root else [])
    price_alerts = cfg.get("price_alerts") or (root_cfg.get("price_alerts") or {} if fallback_from_root else {})
    alert_thresholds = cfg.get("alert_thresholds") or (root_cfg.get("alert_thresholds") or {} if fallback_from_root else {})
    watch_list = watch_list_original or monitored_codes or (root_cfg.get("watch_list") or {} if fallback_from_root else {})

    cfg["monitored_codes"] = monitored_codes
    cfg["signals"] = signals
    cfg["price_alerts"] = price_alerts
    cfg["alert_thresholds"] = alert_thresholds
    cfg["watch_list"] = watch_list
    cfg["watch_list_original"] = watch_list_original

    runtime_health = {
        "positions_count": len(cfg.get("positions") or {}),
        "watch_list_count": len(watch_list),
        "monitored_codes_count": len(monitored_codes),
        "signals_count": len(signals),
        "has_positions": bool(cfg.get("positions")),
        "has_watch_list": bool(watch_list),
        "contract_hollow": not bool(cfg.get("positions")) and not bool(watch_list),
        "watchlist_degraded_to_monitored_codes": bool(monitored_codes) and not bool(cfg.get("watch_list_original") or cfg.get("watch_list")),
    }
    cfg["runtime_health"] = runtime_health

    return {"account_id": aid, "config": cfg, "signature": cfg["bind_signature"]}

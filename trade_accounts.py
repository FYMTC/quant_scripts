"""Hermes 多账户：隔离持仓源 + 启用/停用操盘（非 cross-account propose）。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ROOT = Path(__file__).resolve().parent
DEFAULT_PATH = ROOT / "data" / "trade_accounts.yaml"
STATE_PATH = ROOT / "data" / "trade_accounts_state.json"
EXAMPLE_PATH = ROOT / "trade_accounts.example.yaml"


class HermesTradingError(Exception):
    """Hermes 未授权对该账户操盘或状态冲突。"""


def load_registry(path: Optional[Path] = None) -> Dict[str, Any]:
    cfg_path = Path(path or os.environ.get("TRADE_ACCOUNTS_CONFIG", str(DEFAULT_PATH)))
    if not cfg_path.is_file():
        raise FileNotFoundError(
            f"缺少 {cfg_path}，请复制 {EXAMPLE_PATH} 为 data/trade_accounts.yaml"
        )
    with cfg_path.open(encoding="utf-8") as f:
        reg = yaml.safe_load(f) or {}
    reg.setdefault("accounts", {})
    reg.setdefault("version", 2)
    return reg


def load_state() -> Dict[str, Any]:
    state_path = Path(STATE_PATH)
    if not state_path.is_file():
        reg = load_registry()
        init_active = reg.get("initial_hermes_trading_active") or ["paper_easyths"]
        init_primary = reg.get("initial_desk_primary_account") or (
            init_active[0] if init_active else None
        )
        st = {
            "hermes_trading_active": list(init_active),
            "desk_primary_account": init_primary,
            "updated_at": datetime.now().isoformat(),
            "updated_by": "auto_init",
        }
        save_state(st)
        return st
    with state_path.open(encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict[str, Any]) -> None:
    state["updated_at"] = datetime.now().isoformat()
    state_path = Path(STATE_PATH)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def list_accounts(*, enabled_only: bool = True) -> List[Dict[str, Any]]:
    reg = load_registry()
    active = set(hermes_trading_active())
    out = []
    for aid, acct in (reg.get("accounts") or {}).items():
        if enabled_only and not acct.get("enabled", True):
            continue
        row = dict(acct)
        row["account_id"] = aid
        row["hermes_trading_active"] = aid in active
        out.append(row)
    return out


def get_account(account_id: str) -> Dict[str, Any]:
    reg = load_registry()
    acct = (reg.get("accounts") or {}).get(account_id)
    if not acct:
        raise KeyError(f"unknown account_id: {account_id}")
    if not acct.get("enabled", True):
        raise ValueError(f"account disabled: {account_id}")
    row = dict(acct)
    row["account_id"] = account_id
    row["hermes_trading_active"] = account_id in hermes_trading_active()
    return row


def hermes_trading_active() -> List[str]:
    st = load_state()
    return list(st.get("hermes_trading_active") or [])


def desk_primary_account() -> Optional[str]:
    return load_state().get("desk_primary_account")


def start_hermes_trading(account_id: str, *, set_primary: bool = False) -> Dict[str, Any]:
    get_account(account_id)
    st = load_state()
    active = set(st.get("hermes_trading_active") or [])
    active.add(account_id)
    st["hermes_trading_active"] = sorted(active)
    if set_primary or not st.get("desk_primary_account"):
        st["desk_primary_account"] = account_id
    st["updated_by"] = f"start:{account_id}"
    save_state(st)
    return {
        "ok": True,
        "hermes_trading_active": st["hermes_trading_active"],
        "desk_primary_account": st["desk_primary_account"],
        "guard_note": "smart_guard 约 30s 内自动热加载，无需重启进程",
    }


def stop_hermes_trading(account_id: str) -> Dict[str, Any]:
    get_account(account_id)
    st = load_state()
    active = [a for a in (st.get("hermes_trading_active") or []) if a != account_id]
    st["hermes_trading_active"] = active
    if st.get("desk_primary_account") == account_id:
        st["desk_primary_account"] = active[0] if len(active) == 1 else None
    st["updated_by"] = f"stop:{account_id}"
    save_state(st)
    return {
        "ok": True,
        "hermes_trading_active": active,
        "desk_primary_account": st.get("desk_primary_account"),
        "guard_note": "smart_guard 约 30s 内自动热加载，无需重启进程",
    }


def set_desk_primary(account_id: str) -> Dict[str, Any]:
    if account_id not in hermes_trading_active():
        raise HermesTradingError(
            f"{account_id} 未在 Hermes 操盘列表中，请先 start_hermes_trading"
        )
    st = load_state()
    st["desk_primary_account"] = account_id
    st["updated_by"] = f"primary:{account_id}"
    save_state(st)
    return {
        "ok": True,
        "desk_primary_account": account_id,
        "guard_note": "smart_guard 约 30s 内自动热加载，无需重启进程",
    }


def assert_hermes_may_trade(account_id: str) -> None:
    if account_id not in hermes_trading_active():
        raise HermesTradingError(
            f"Hermes 已停止对账户 {account_id} 的操盘。"
            f"当前启用: {hermes_trading_active() or '（无）'}。"
            f"请执行: trade_accounts.py start {account_id}"
        )


def resolve_trading_account(explicit: Optional[str] = None) -> str:
    """Desk / 默认 propose 使用的唯一操盘账户（必须已 start）。"""
    if explicit:
        assert_hermes_may_trade(explicit)
        return explicit

    active = hermes_trading_active()
    if not active:
        raise HermesTradingError(
            "Hermes 当前未启用任何操盘账户。请: trade_accounts.py start paper_easyths"
        )

    primary = desk_primary_account()
    if primary and primary in active:
        return primary

    if len(active) == 1:
        return active[0]

    raise HermesTradingError(
        f"多个账户已启用 {active}，请 set-primary 或 propose 时 --account 指定其一"
    )


def status_report() -> Dict[str, Any]:
    from trade_account_context import load_account_snapshot, format_snapshot_brief

    rows = []
    for acct in list_accounts():
        aid = acct["account_id"]
        row = {
            "account_id": aid,
            "label": acct.get("label"),
            "position_source": acct.get("position_source"),
            "hermes_trading_active": acct.get("hermes_trading_active"),
            "execution_provider": (acct.get("execution") or {}).get("provider"),
        }
        if acct.get("hermes_trading_active"):
            try:
                snap = load_account_snapshot(aid)
                row["snapshot_brief"] = format_snapshot_brief(snap)
                row["position_count"] = snap.get("position_count")
                if snap.get("error"):
                    row["snapshot_error"] = str(snap["error"])[:300]
            except Exception as exc:
                row["snapshot_error"] = str(exc)[:300]
        rows.append(row)
    active = hermes_trading_active()
    multi = len(active) > 1
    note = ""
    if multi:
        note = (
            f"多账户操盘：hermes_trading_active={active}。"
            f"Desk/guard 默认跟随 desk_primary_account={desk_primary_account()}；"
            f"非主账户的 propose 必须显式 --account；每笔请示仅绑定一个 account_id，禁止混用持仓快照。"
        )
    return {
        "hermes_trading_active": active,
        "hermes_trading_active_count": len(active),
        "multi_account_hermes_trading": multi,
        "hermes_multi_account_note": note or None,
        "desk_primary_account": desk_primary_account(),
        "accounts": rows,
    }


def execution_provider(account_id: str) -> str:
    acct = get_account(account_id)
    return (acct.get("execution") or {}).get("provider") or "none"


def auto_execute_on_resolve(account_id: str) -> bool:
    acct = get_account(account_id)
    return bool((acct.get("execution") or {}).get("auto_execute_on_resolve"))


def easyths_config_path(account_id: str) -> Path:
    acct = get_account(account_id)
    p = (acct.get("execution") or {}).get("easyths_config")
    if not p:
        raise ValueError(f"account {account_id} has no easyths_config")
    return Path(p)


def should_notify_execution_wechat(account_id: str) -> bool:
    acct = get_account(account_id)
    return bool((acct.get("wechat") or {}).get("on_execution_result"))


def default_wechat_chat_id() -> str:
    reg = load_registry()
    return (reg.get("wechat") or {}).get("default_chat_id") or ""


def stock_kb_book_mode(account_id: str) -> str:
    """symbol：更新标的账本；audit_only：仅记流水带 account_id（模拟盘）。"""
    return (get_account(account_id).get("stock_kb_book") or "symbol").lower()


def should_update_symbol_book(account_id: str) -> bool:
    return stock_kb_book_mode(account_id) != "audit_only"


def main() -> None:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Hermes 操盘账户 启用/停用/状态")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="各账户操盘状态 + 持仓摘要")
    st = sub.add_parser("start", help="允许 Hermes 对该账户操盘")
    st.add_argument("account_id")
    st.add_argument("--primary", action="store_true", help="并设为 Desk 主账户")
    sp = sub.add_parser("stop", help="停止 Hermes 对该账户操盘")
    sp.add_argument("account_id")
    pr = sub.add_parser("set-primary", help="指定 Desk/默认请示账户（须已 start）")
    pr.add_argument("account_id")

    args = ap.parse_args()
    if args.cmd == "status":
        print(json.dumps(status_report(), ensure_ascii=False, indent=2))
    elif args.cmd == "start":
        print(json.dumps(start_hermes_trading(args.account_id, set_primary=args.primary), ensure_ascii=False, indent=2))
    elif args.cmd == "stop":
        print(json.dumps(stop_hermes_trading(args.account_id), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(set_desk_primary(args.account_id), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

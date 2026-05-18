"""Hermes 多账户交易注册表（人工微信 / EasyTHS 自动执行）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ROOT = Path(__file__).resolve().parent
DEFAULT_PATH = ROOT / "data" / "trade_accounts.yaml"
EXAMPLE_PATH = ROOT / "trade_accounts.example.yaml"


def load_registry(path: Optional[Path] = None) -> Dict[str, Any]:
    cfg_path = Path(path or os.environ.get("TRADE_ACCOUNTS_CONFIG", str(DEFAULT_PATH)))
    if not cfg_path.is_file():
        raise FileNotFoundError(
            f"缺少 {cfg_path}，请复制 {EXAMPLE_PATH} 为 data/trade_accounts.yaml"
        )
    with cfg_path.open(encoding="utf-8") as f:
        reg = yaml.safe_load(f) or {}
    reg.setdefault("accounts", {})
    reg.setdefault("default_propose_account", "paper_easyths")
    return reg


def list_accounts(*, enabled_only: bool = True) -> List[Dict[str, Any]]:
    reg = load_registry()
    out = []
    for aid, acct in (reg.get("accounts") or {}).items():
        if enabled_only and not acct.get("enabled", True):
            continue
        row = dict(acct)
        row["account_id"] = aid
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
    return row


def default_propose_account() -> str:
    reg = load_registry()
    aid = reg.get("default_propose_account") or "paper_easyths"
    get_account(aid)
    return aid


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

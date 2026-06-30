"""close_loop_reflow.py — T1.10 二期清仓标的回流窗口（2026-06-30）

SELL 执行后记录清仓标的，5-10 天内视为 Tier B 回流（防"卖飞就忘"）；
过期自动清理。

设计参考：
  - post_execution_rescan.py 的"SELL 后记录 → 下次循环读取"模式
  - single-stock-swing-strategy.md §6.2 "清仓后回流，避免卖飞就忘"

集成点：
  - trade_outbox.resolve_and_execute() SELL 执行后段调 record_clear()
  - signal_loop._is_high_attention() 开头查 is_in_reflow() → 回流窗口内视为 Tier B
  - signal_loop.auto_generate() 开头调 prune_expired() 清理过期记录
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from typing import Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from system_config import cfg

# ========== 阈值常量 ==========

REFLOW_DAYS_MIN = 5           # 最短回流窗口
REFLOW_DAYS_MAX = 10          # 最长回流窗口
REFLOW_DAYS_DEFAULT = 7       # 默认回流窗口（区间中值）

STATE_PATH = cfg.path.close_loop_reflow


# ========== 状态读写（原子替换）==========

def _load_state() -> dict:
    """读取回流状态文件。文件不存在/损坏 → 返回空结构。"""
    if not os.path.isfile(STATE_PATH):
        return {"records": [], "updated_at": ""}
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"records": [], "updated_at": ""}
        records = data.get("records")
        if not isinstance(records, list):
            data["records"] = []
        return data
    except Exception:
        return {"records": [], "updated_at": ""}


def _save_state(state: dict) -> None:
    """原子写入状态文件（tmp + os.replace）。"""
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    state["updated_at"] = datetime.now().isoformat()
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def _parse_expire(record: dict) -> Optional[datetime]:
    """解析记录的 expire_at，失败返回 None。"""
    expire_str = record.get("expire_at") or ""
    if not expire_str:
        return None
    try:
        return datetime.fromisoformat(expire_str)
    except Exception:
        return None


# ========== 公开 API ==========

def record_clear(code: str, name: str = "", *, sell_price: float = 0.0,
                 shares: int = 0, account_id: str = "", signal_id: str = "",
                 tier_at_sale: str = "A", reflow_days: Optional[int] = None) -> dict:
    """SELL 执行后调用：记录清仓标的进回流窗口。

    Args:
        code: 标的代码
        name: 标的名称
        sell_price: 卖出价
        shares: 卖出股数
        account_id: 账户 ID
        signal_id: 触发卖出的信号 ID
        tier_at_sale: 卖出时 Tier（清仓前必为持仓 → 默认 "A"）
        reflow_days: 回流天数，缺省=REFLOW_DAYS_DEFAULT，clamp 到 [MIN, MAX]

    Returns:
        写入的 record dict
    """
    if not code:
        return {}
    # clamp reflow_days
    if reflow_days is None:
        rd = REFLOW_DAYS_DEFAULT
    else:
        rd = max(REFLOW_DAYS_MIN, min(REFLOW_DAYS_MAX, int(reflow_days)))

    now = datetime.now()
    sold_date_str = now.strftime("%Y-%m-%d")
    expire_at = (now + timedelta(days=rd)).isoformat()

    record = {
        "code": str(code),
        "name": str(name or ""),
        "sold_at": now.isoformat(),
        "sold_date": sold_date_str,
        "sell_price": float(sell_price or 0.0),
        "shares": int(shares or 0),
        "account_id": str(account_id or ""),
        "signal_id": str(signal_id or ""),
        "tier_at_sale": str(tier_at_sale or "A"),
        "reflow_days": rd,
        "expire_at": expire_at,
    }

    state = _load_state()
    records = state.get("records") or []
    # 同 code 旧记录覆盖（保留最新清仓）
    records = [r for r in records if str(r.get("code") or "") != str(code)]
    records.append(record)
    state["records"] = records
    _save_state(state)
    return record


def is_in_reflow(code: str) -> bool:
    """code 是否在有效回流窗口内（未过期）。"""
    if not code:
        return False
    record = get_reflow_record(code)
    if not record:
        return False
    expire = _parse_expire(record)
    if expire is None:
        return False
    return datetime.now() <= expire


def get_reflow_codes() -> list:
    """返回所有有效回流代码列表（已过期的不返回）。"""
    state = _load_state()
    records = state.get("records") or []
    now = datetime.now()
    codes = []
    for r in records:
        expire = _parse_expire(r)
        if expire is not None and now <= expire:
            codes.append(str(r.get("code") or ""))
    return codes


def get_reflow_record(code: str) -> Optional[dict]:
    """返回 code 的回流记录（无则 None）。不检查过期。"""
    if not code:
        return None
    state = _load_state()
    records = state.get("records") or []
    for r in records:
        if str(r.get("code") or "") == str(code):
            return r
    return None


def prune_expired() -> int:
    """删除过期记录，返回删除条数。调用方：signal_loop.auto_generate() 开头。"""
    state = _load_state()
    records = state.get("records") or []
    now = datetime.now()
    kept = []
    removed = 0
    for r in records:
        expire = _parse_expire(r)
        if expire is not None and now > expire:
            removed += 1
            continue
        kept.append(r)
    if removed > 0:
        state["records"] = kept
        _save_state(state)
    return removed


# ========== CLI ==========

def cli():
    """CLI 入口：
      python3 close_loop_reflow.py list
      python3 close_loop_reflow.py prune
    """
    import argparse
    p = argparse.ArgumentParser(description="T1.10 二期清仓回流窗口管理")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出所有回流记录（含过期）")
    sub.add_parser("prune", help="清理过期记录")
    args = p.parse_args()

    if args.cmd == "list":
        state = _load_state()
        records = state.get("records") or []
        now = datetime.now()
        print(f"回流记录共 {len(records)} 条（updated_at={state.get('updated_at') or 'N/A'}）：")
        for r in records:
            expire = _parse_expire(r)
            status = "有效" if (expire and now <= expire) else "过期"
            print(
                f"  [{status}] {r.get('code',''):6s} {r.get('name','')[:8]:8s} "
                f"sold={r.get('sold_date','')} expire={r.get('expire_at','')[:10]} "
                f"price={r.get('sell_price',0):.2f} shares={r.get('shares',0)}"
            )
    elif args.cmd == "prune":
        n = prune_expired()
        print(f"已清理 {n} 条过期记录")


if __name__ == "__main__":
    cli()

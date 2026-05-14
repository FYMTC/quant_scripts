#!/usr/bin/env python3
"""
update_position.py — 持仓更新统一入口

问题：历史上有两个数据源（guard_config.json + stock_kb DB），
      交易后只更新一个导致cron读到过时数据。

解决：本脚本原子更新两者。以后所有交易录入必须走此入口。
"""

import json
import sqlite3
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "guard_config.json")
DB_PATH = os.path.join(BASE, "trade_log.db")


def update_trade(code: str, action: str, price: float, shares: int,
                 rationale: str = ""):
    """记录一笔交易，同步更新DB + guard_config"""
    conn = sqlite3.connect(DB_PATH)

    # 1. 读当前状态
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    cur = conn.cursor()

    # 2. 获取当前持仓
    cur.execute("SELECT current_shares, avg_cost FROM stock_kb WHERE code=?", (code,))
    row = cur.fetchone()
    old_shares = row[0] if row else 0
    old_cost = row[1] if row else 0.0

    amount = price * shares

    if action == "BUY":
        new_shares = old_shares + shares
        new_cost = round((old_cost * old_shares + amount) / new_shares, 4) if new_shares > 0 else price
        cash_delta = -amount
    elif action == "SELL":
        new_shares = old_shares - shares
        new_cost = old_cost  # 减仓不改变成本基准
        cash_delta = amount
        if new_shares < 0:
            print(f"❌ 卖出{shares}股超过持仓{old_shares}股", file=sys.stderr)
            conn.close()
            return False
    else:
        print(f"❌ 未知操作: {action}", file=sys.stderr)
        conn.close()
        return False

    # 3. 更新DB (stock_kb + stock_trades + portfolio_cash)
    cur.execute("""
        INSERT INTO stock_kb (code, name, current_shares, avg_cost, first_tracked_at)
        VALUES (?, ?, ?, ?, datetime('now','localtime'))
        ON CONFLICT(code) DO UPDATE SET current_shares=?, avg_cost=?, last_traded_at=datetime('now','localtime')
    """, (code, code, new_shares, new_cost, new_shares, new_cost))

    new_cash = round(config["cash"] + cash_delta, 2)
    cur.execute("UPDATE portfolio_cash SET amount=?", (new_cash,))

    cur.execute("""
        INSERT INTO stock_trades (stock_code, trade_date, action, price, shares, amount, rationale, created_at)
        VALUES (?, date('now','localtime'), ?, ?, ?, ?, ?, datetime('now','localtime'))
    """, (code, action, price, shares, amount, rationale))

    conn.commit()
    conn.close()

    # 4. 更新guard_config
    if code in config["positions"]:
        config["positions"][code]["shares"] = new_shares
        config["positions"][code]["cost"] = new_cost
    else:
        config["positions"][code] = {"name": code, "shares": new_shares, "cost": new_cost}

    config["cash"] = new_cash
    config["available_capital"] = new_cash

    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    print(f"✓ {code} {action} {shares}股@{price} → {new_shares}股 成本{new_cost} 现金{new_cash}")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: update_position.py <code> <BUY|SELL> <price> <shares> [rationale]")
        print("Example: update_position.py 000063 BUY 39.42 100 '加仓信号'")
        sys.exit(1)

    code = sys.argv[1]
    action = sys.argv[2]
    price = float(sys.argv[3])
    shares = int(sys.argv[4])
    rationale = sys.argv[5] if len(sys.argv) > 5 else ""

    update_trade(code, action, price, shares, rationale)

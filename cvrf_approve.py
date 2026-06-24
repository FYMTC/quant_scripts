#!/usr/local/bin/python3
"""
cvrf_approve.py — CVRF 人工确认闸

DS-3 修复: CVRF 冷启动阶段产出标记为 pending，
由人工通过此 CLI 批量 approve 后再落入 stock_kb/signal。

用法:
  python cvrf_approve.py list              # 列出所有 pending 洞察
  python cvrf_approve.py approve --id 5     # 批准单条
  python cvrf_approve.py approve --all      # 批量批准所有 pending
  python cvrf_approve.py reject --id 5      # 拒绝（删除）
"""

import sys
import json
import sqlite3
from datetime import datetime
from system_config import cfg

DB_PATH = cfg.path.trade_db


def list_pending():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, stock_code, category, content, insight_date, created_at "
        "FROM stock_insights WHERE confidence='pending' ORDER BY id"
    ).fetchall()
    conn.close()

    if not rows:
        print("✅ 无待审批的 CVRF 洞察")
        return []

    print(f"📋 待审批 CVRF 洞察 ({len(rows)}条):\n")
    for r in rows:
        print(f"  [{r['id']}] {r['stock_code']} | {r['category']} | {r['insight_date']}")
        print(f"       {r['content'][:100]}")
        print()
    return [dict(r) for r in rows]


def approve(insight_id: int = None, all_pending: bool = False):
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now().isoformat()

    if all_pending:
        conn.execute(
            "UPDATE stock_insights SET confidence='medium', "
            "content=content||' [CVRF approved '||?||']' WHERE confidence='pending'",
            [now[:10]]
        )
        n = conn.total_changes
        conn.commit()
        conn.close()
        print(f"✅ 已批量批准 {n} 条 CVRF 洞察")
        return

    conn.execute(
        "UPDATE stock_insights SET confidence='medium' WHERE id=? AND confidence='pending'",
        [insight_id]
    )
    n = conn.total_changes
    conn.commit()
    conn.close()

    if n:
        print(f"✅ 已批准洞察 #{insight_id}")
    else:
        print(f"❌ 洞察 #{insight_id} 不存在或非 pending 状态")


def reject(insight_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "DELETE FROM stock_insights WHERE id=? AND confidence='pending'",
        [insight_id]
    )
    n = conn.total_changes
    conn.commit()
    conn.close()

    if n:
        print(f"🗑️  已拒绝并删除洞察 #{insight_id}")
    else:
        print(f"❌ 洞察 #{insight_id} 不存在或非 pending 状态")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: cvrf_approve.py [list|approve|reject] ...")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "list":
        list_pending()
    elif cmd == "approve":
        if "--all" in sys.argv:
            approve(all_pending=True)
        else:
            try:
                idx = sys.argv.index("--id")
                approve(insight_id=int(sys.argv[idx + 1]))
            except (ValueError, IndexError):
                print("Usage: cvrf_approve.py approve --id <N> | --all")
                sys.exit(1)
    elif cmd == "reject":
        try:
            idx = sys.argv.index("--id")
            reject(insight_id=int(sys.argv[idx + 1]))
        except (ValueError, IndexError):
            print("Usage: cvrf_approve.py reject --id <N>")
            sys.exit(1)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)

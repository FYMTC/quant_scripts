#!/config/quant_env/bin/python3
"""
daily_context.py - Cron上下文DB存取工具
========================================
每个cron任务：启动时 load 上下文，结束后 save 报告。

用法:
  python daily_context.py load --job "盘前简报"       # 获取上下文
  python daily_context.py save --job "盘前简报"       # 保存报告（读stdin）
      --summary "比亚迪+3.14%集中度48.6%..." 
      --metrics '{"byp":104.67,"cash":4867,...}'
"""

import sys, json, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from trade_db import CronReport


def cmd_load():
    p = argparse.ArgumentParser()
    p.add_argument("--job", required=True, help="cron job name")
    p.add_argument("--max-chars", type=int, default=3000)
    args = p.parse_args()
    
    cr = CronReport()
    ctx = cr.get_context(args.job, max_chars=args.max_chars)
    if ctx:
        print(ctx)
    else:
        print("(no previous reports found)", file=sys.stderr)


def cmd_save():
    p = argparse.ArgumentParser()
    p.add_argument("--job", required=True, help="cron job name")
    p.add_argument("--summary", default="", help="one-line summary")
    p.add_argument("--metrics", default="{}", help="JSON key metrics")
    args = p.parse_args()
    
    # Read content from stdin
    content = sys.stdin.read().strip()
    if not content:
        print("ERROR: no content on stdin", file=sys.stderr)
        sys.exit(1)
    
    try:
        metrics = json.loads(args.metrics)
    except:
        metrics = {}
    
    cr = CronReport()
    rid = cr.save(args.job, content, args.summary, metrics)
    print(f"SAVED report #{rid}", file=sys.stderr)
    print(rid)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: daily_context.py [load|save] ...", file=sys.stderr)
        sys.exit(1)
    
    cmd = sys.argv[1]
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    
    if cmd == "load":
        cmd_load()
    elif cmd == "save":
        cmd_save()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)

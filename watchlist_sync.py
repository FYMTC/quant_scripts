#!python3
"""
Watchlist自动同步：Qlib因子筛选 -> guard_config.json 热加载
==========================================================
读取 Qlib screening_result.json，自动更新 guard_config.json 的 watch_list。
守护进程30秒热加载自动生效。

策略：
- 评分>=75（值得操作）-> 自动纳入 watch_list
- 评分>=55（可留观察）-> 纳入 watch_list
- 评分<55 -> 不纳入（除非已是持仓）
- 持仓标的始终保留在 watch_list
- 原有手动添加的自选不删除（仅追加）
"""

import json, sys
from datetime import datetime
from pathlib import Path
from system_config import cfg

SCREENING_PATH = "/config/qlib_data/screening/screening_result.json"
GUARD_CONFIG_PATH = cfg.path.guard_config
SYNC_LOG = "/config/qlib_data/screening/watchlist_sync.log"

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    Path(SYNC_LOG).parent.mkdir(parents=True, exist_ok=True)
    with open(SYNC_LOG, "a") as f:
        f.write(line + "\n")

def sync():
    if not Path(SCREENING_PATH).exists():
        log("SKIP: screening_result.json not found")
        return False

    screening = json.load(open(SCREENING_PATH))
    config = json.load(open(GUARD_CONFIG_PATH))

    old_watch = config.get("watch_list", {})
    positions = config.get("positions", {})
    old_count = len(old_watch)

    new_watch = dict(old_watch)

    for code, info in positions.items():
        new_watch[code] = info.get("name", code)

    added = []
    for stock in screening:
        code = stock["code"]
        name = stock.get("name", code)
        score = stock.get("composite_score", 0)
        grade = stock.get("grade", "")

        if score >= 55 and code not in new_watch:
            new_watch[code] = name
            added.append(f"+ {name}({code}) {score:.0f} {grade}")

    config["watch_list"] = new_watch
    config["_wl_last_sync"] = datetime.now().isoformat()

    json.dump(config, open(GUARD_CONFIG_PATH, "w"), ensure_ascii=False, indent=2)
    new_count = len(new_watch)

    log(f"Synced: {old_count} -> {new_count} (+{new_count-old_count})")
    for a in added:
        log(a)

    print(json.dumps({"before": old_count, "after": new_count, "added": added}, ensure_ascii=False))
    return True

if __name__ == "__main__":
    sync()

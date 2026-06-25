#!python3
"""
log_rotator.py — 日志轮转 + 缓存清理

策略：
- guard_daemon.log: 保留最近 3000 行（约30KB）
- agent.log: 保留最近 10000 行（约100KB）
- 备份旧日志到 .1/.2/.3，超出删除
- image_cache: 删除7天前的文件

用法：
  python log_rotator.py           # 检查并轮转超限日志
  python log_rotator.py --force   # 强制轮转所有日志
  python log_rotator.py --dry-run # 仅报告，不执行
"""

import os
import sys
import time
import glob
from datetime import datetime
from pathlib import Path
from system_config import cfg

ROTATION_RULES = [
    (cfg.path.guard_daemon_log, 3000, 3),
    (cfg.path.guard_log, 3000, 3),
    (cfg.path.guard_pushlog, 1000, 3),
    (cfg.path.hermes_agent_log, 10000, 2),
    (cfg.path.hermes_errors_log, 5000, 2),
    (cfg.path.hermes_gateway_log, 5000, 2),
]

CACHE_DIRS = [
    (cfg.path.hermes_image_cache, 7),   # 7天前的缓存
]


def rotate_log(filepath, max_lines, keep, dry_run=False):
    if not os.path.exists(filepath):
        return None
    with open(filepath, "rb") as f:
        count = sum(1 for _ in f)
    if count <= max_lines:
        return None
    for i in range(keep - 1, 0, -1):
        old = f"{filepath}.{i}"
        older = f"{filepath}.{i + 1}"
        if os.path.exists(old):
            if dry_run:
                continue
            os.rename(old, older)
    backup = f"{filepath}.1"
    if dry_run:
        return {"file": filepath, "lines": count, "max": max_lines, "action": "would_rotate"}
    os.rename(filepath, backup)
    with open(backup, "r") as src:
        lines = src.readlines()
    with open(filepath, "w") as dst:
        dst.writelines(lines[-max_lines:])
    for i in range(keep + 1, keep + 10):
        extra = f"{filepath}.{i}"
        if os.path.exists(extra):
            os.remove(extra)
    return {"file": filepath, "lines": count, "max": max_lines, "kept": max_lines, "backup": backup, "action": "rotated"}


def cleanup_cache(dirs, dry_run=False):
    results = []
    now = time.time()
    for path, max_age_days in dirs:
        if not os.path.isdir(path):
            continue
        for fname in os.listdir(path):
            fpath = os.path.join(path, fname)
            if not os.path.isfile(fpath):
                continue
            age = now - os.path.getmtime(fpath)
            if age > max_age_days * 86400:
                if not dry_run:
                    os.remove(fpath)
                results.append(fpath)
    return results


def cleanup_health_logs(dry_run=False):
    pattern = cfg.path.health_log_dir + "/*.md"
    files = sorted(glob.glob(pattern))
    if len(files) <= 30:
        return None
    removed = []
    for f in files[:-30]:
        if not dry_run:
            os.remove(f)
        removed.append(f)
    return {"removed": len(removed), "files": removed[:5]}


def main():
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    print(f"=== 日志轮转 + 缓存清理 {'(dry-run)' if dry_run else ''} ===")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    results = []
    for filepath, max_lines, keep in ROTATION_RULES:
        result = rotate_log(filepath, 1 if force else max_lines, keep, dry_run)
        if result:
            results.append(result)
            if result["action"] == "rotated":
                print(f"  ✂️  {result['file']}: {result['lines']:,}→{result['kept']:,}行")
            else:
                print(f"  📋 {result['file']}: {result['lines']:,}行超{result['max']:,}上限")

    h = cleanup_health_logs(dry_run)
    if h:
        print(f"  🧹 health_log/: 删除{h['removed']}个旧文件")

    cache_files = cleanup_cache(CACHE_DIRS, dry_run)
    if cache_files:
        print(f"  🗑️  缓存清理: {len(cache_files)}个文件")

    if not results and not h and not cache_files:
        print("  全部正常，无需清理")


if __name__ == "__main__":
    main()

#!/config/quant_env/bin/python3
"""
log_rotator.py — 日志轮转与截断

策略：
- guard_daemon.log: 保留最近 5000 行（约50KB）
- guard_pushlog.txt: 保留最近 1000 行
- agent.log: 保留最近 20000 行（约200KB）
- 备份旧日志到 .old 后缀，保留最后3个轮转

用法：
  python log_rotator.py           # 检查并轮转超限日志
  python log_rotator.py --force   # 强制轮转所有日志
  python log_rotator.py --dry-run # 仅报告，不执行
"""

import os
import sys
import glob
from datetime import datetime
from pathlib import Path

# 日志轮转规则: (路径, 最大行数, 保留轮转数)
ROTATION_RULES = [
    ("/config/quant_scripts/guard_daemon.log", 3000, 3),
    ("/config/quant_scripts/guard.log", 3000, 3),
    ("/config/quant_scripts/guard_pushlog.txt", 1000, 3),
    ("/config/.hermes/logs/agent.log", 10000, 2),
    ("/config/.hermes/logs/errors.log", 5000, 2),
    ("/config/.hermes/logs/gateway.log", 5000, 2),
    # health_log 目录下的 md 文件只保留最近 30 个
]


def rotate_log(filepath: str, max_lines: int, keep: int, dry_run: bool = False):
    """轮转单个日志文件"""
    if not os.path.exists(filepath):
        return None

    with open(filepath, "rb") as f:
        # 快速统计行数（二进制读）
        count = sum(1 for _ in f)

    if count <= max_lines:
        return None  # 不超限

    # 轮转旧备份
    for i in range(keep - 1, 0, -1):
        old = f"{filepath}.{i}"
        older = f"{filepath}.{i + 1}"
        if os.path.exists(old):
            if dry_run:
                continue
            os.rename(old, older)

    # 备份当前
    backup = f"{filepath}.1"
    if dry_run:
        return {"file": filepath, "lines": count, "max": max_lines, "action": "would_rotate"}
    
    os.rename(filepath, backup)

    # 裁剪保留
    with open(backup, "r") as src:
        lines = src.readlines()

    keep_lines = lines[-max_lines:]
    with open(filepath, "w") as dst:
        dst.writelines(keep_lines)

    # 删除超出保留数的旧备份
    for i in range(keep + 1, keep + 10):
        extra = f"{filepath}.{i}"
        if os.path.exists(extra):
            os.remove(extra)

    return {
        "file": filepath,
        "lines": count,
        "max": max_lines,
        "kept": max_lines,
        "backup": backup,
        "action": "rotated",
    }


def cleanup_health_logs(dry_run: bool = False):
    """清理 health_log 目录，保留最近 30 个文件"""
    pattern = "/config/quant_scripts/health_log/*.md"
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

    print(f"=== 日志轮转 {'(dry-run)' if dry_run else ''} ===")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    results = []
    for filepath, max_lines, keep in ROTATION_RULES:
        if force:
            # 强制模式：临时设 max=1 触发轮转
            result = rotate_log(filepath, 1, keep, dry_run)
        else:
            result = rotate_log(filepath, max_lines, keep, dry_run)

        if result:
            results.append(result)
            if result["action"] == "rotated":
                print(f"  ✂️  {result['file']}: {result['lines']:,}→{result['kept']:,}行 (备份: {result['backup']})")
            else:
                print(f"  📋 {result['file']}: {result['lines']:,}行超{result['max']:,}行上限，将轮转")

    h_result = cleanup_health_logs(dry_run)
    if h_result:
        print(f"  🧹 health_log/: 删除{h_result['removed']}个旧文件")

    if not results and not h_result:
        print("  所有日志正常，无需轮转")

    # 报告 .hermes/logs 整体大小
    hermes_log_dir = "/config/.hermes/logs"
    if os.path.exists(hermes_log_dir):
        total = sum(
            os.path.getsize(os.path.join(hermes_log_dir, f))
            for f in os.listdir(hermes_log_dir)
            if os.path.isfile(os.path.join(hermes_log_dir, f))
        )
        print(f"\n  .hermes/logs 总大小: {total/1024/1024:.1f}MB")


if __name__ == "__main__":
    main()

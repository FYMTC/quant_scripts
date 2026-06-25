#!python3
"""
core/monitor/intervention.py — Hermes 热干预接口

Hermes 通过写入 guard_intervention.json 向运行中的盯盘系统发送实时命令。
guard.py 每轮（30秒）读取此文件，立即执行，回写结果。

命令格式:
  {
    "command": "PAUSE|RESUME|EMERGENCY_STOP|OVERRIDE|FORCE_CHECK|STATUS",
    "params": {...},
    "issued_by": "hermes",
    "issued_at": "2026-05-14T11:30:00",
    "status": "pending|executed|failed",
    "result": ""
  }

用法（Hermes写入）:
  echo '{"command":"PAUSE","params":{},"issued_by":"hermes"}' > guard_intervention.json

用法（guard.py读取）:
  from core.monitor.intervention import read_command, acknowledge
  cmd = read_command()
  if cmd and cmd["status"] == "pending":
      execute(cmd)
      acknowledge(cmd, "done")
"""

import json, os, time
from datetime import datetime
from typing import Optional, Dict

INTERVENTION_FILE = "/config/quant_scripts/guard_intervention.json"


def issue_command(command: str, params: Dict = None) -> bool:
    """
    Hermes 发出热干预命令。

    Args:
        command: PAUSE | RESUME | EMERGENCY_STOP | OVERRIDE | FORCE_CHECK | STATUS
        params: 命令参数

    Returns:
        True if written successfully
    """
    cmd = {
        "command": command,
        "params": params or {},
        "issued_by": "hermes",
        "issued_at": datetime.now().isoformat(),
        "status": "pending",
        "result": "",
    }
    try:
        with open(INTERVENTION_FILE, "w") as f:
            json.dump(cmd, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def read_command() -> Optional[Dict]:
    """guard.py 读取待执行命令"""
    if not os.path.exists(INTERVENTION_FILE):
        return None
    try:
        with open(INTERVENTION_FILE) as f:
            cmd = json.load(f)
        if cmd.get("status") == "pending":
            return cmd
    except Exception:
        pass
    return None


def acknowledge(cmd: Dict, result: str):
    """guard.py 执行后回写结果"""
    cmd["status"] = "executed" if "error" not in result.lower() else "failed"
    cmd["result"] = result
    cmd["executed_at"] = datetime.now().isoformat()
    try:
        with open(INTERVENTION_FILE, "w") as f:
            json.dump(cmd, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def clear_command():
    """清除已执行的命令"""
    if os.path.exists(INTERVENTION_FILE):
        os.remove(INTERVENTION_FILE)


# ── 便捷函数：Hermes 直接调用 ──

def pause_trading(reason: str = "") -> bool:
    """暂停所有交易决策"""
    return issue_command("PAUSE", {"reason": reason or "Hermes手动暂停"})


def resume_trading() -> bool:
    """恢复正常"""
    return issue_command("RESUME", {})


def emergency_stop(reason: str = "") -> bool:
    """紧急停止——清空信号队列"""
    return issue_command("EMERGENCY_STOP", {"reason": reason or "Hermes紧急停止"})


def override_signal(signal_id: str, new_action: str) -> bool:
    """覆盖信号裁决"""
    return issue_command("OVERRIDE", {"signal_id": signal_id, "action": new_action})


def force_check(code: str) -> bool:
    """强制对某标的做全量分析"""
    return issue_command("FORCE_CHECK", {"code": code})


def get_status() -> Optional[Dict]:
    """获取系统状态"""
    issue_command("STATUS", {})
    time.sleep(2)  # 等 guard 处理
    if os.path.exists(INTERVENTION_FILE):
        with open(INTERVENTION_FILE) as f:
            return json.load(f)
    return None


# ── CLI ──

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: intervention.py <command> [args]")
        print("Commands: pause, resume, stop, override <id> <action>, check <code>, status")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "pause":
        reason = sys.argv[2] if len(sys.argv) > 2 else ""
        ok = pause_trading(reason)
        print(f"{'✅' if ok else '❌'} PAUSE issued" + (f": {reason}" if reason else ""))
    elif cmd == "resume":
        ok = resume_trading()
        print(f"{'✅' if ok else '❌'} RESUME issued")
    elif cmd == "stop":
        reason = sys.argv[2] if len(sys.argv) > 2 else ""
        ok = emergency_stop(reason)
        print(f"{'✅' if ok else '❌'} EMERGENCY_STOP issued")
    elif cmd == "override":
        if len(sys.argv) < 4:
            print("Usage: override <signal_id> <BUY|SELL|HOLD>")
            sys.exit(1)
        ok = override_signal(sys.argv[2], sys.argv[3])
        print(f"{'✅' if ok else '❌'} OVERRIDE {sys.argv[2]} → {sys.argv[3]}")
    elif cmd == "check":
        if len(sys.argv) < 3:
            print("Usage: check <stock_code>")
            sys.exit(1)
        ok = force_check(sys.argv[2])
        print(f"{'✅' if ok else '❌'} FORCE_CHECK {sys.argv[2]}")
    elif cmd == "status":
        s = get_status()
        if s:
            print(json.dumps(s, ensure_ascii=False, indent=2))
        else:
            print("❌ 无法获取状态")
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)

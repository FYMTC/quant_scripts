#!python3
"""
信号注册工具 — 供cron和Agent统一调用
将信号写入guard_config.json，带context_ref上下文链

用法:
  # 注册一个盯盘信号
  python signal_register.py add --id ziguang_dip_bounce --code 000938 --type rapid_drop_bounce \\
      --params '{"drop_pct":-2.5,"prev_gain":2.0}' --rationale "理由" --context_ref "report_id_xxx"

  # 列出当前所有信号
  python signal_register.py list

  # 移除已触发或失效的信号
  python signal_register.py remove --id ziguang_dip_bounce
"""
import json, os, sys, argparse
from datetime import datetime
from system_config import cfg

CONFIG_FILE = cfg.path.guard_config


def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_config(c):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)


def add_signal(sig_id: str, code: str, name: str, sig_type: str,
               params: dict, rationale: str, context_ref: str = "",
               target: float = 0):
    c = load_config()
    signals = c.setdefault("signals", [])
    # 去重：同id不重复注册
    for s in signals:
        if s.get("id") == sig_id:
            print(f"⚠️ 信号 {sig_id} 已存在，跳过")
            return False
    entry = {
        "id": sig_id,
        "code": code,
        "name": name,
        "type": sig_type,
        "params": params,
        "rationale": rationale,
        "context_ref": context_ref,
        "registered": datetime.now().isoformat(),
        "registered_by": "agent_loop",
    }
    if target:
        entry["target"] = target
    signals.append(entry)
    save_config(c)
    print(f"✅ 信号已注册: {sig_id} ({sig_type}) → {name}[{code}]")
    print(f"   上下文链: {context_ref or '(无)'}")
    return True


def remove_signal(sig_id: str):
    c = load_config()
    signals = c.get("signals", [])
    before = len(signals)
    c["signals"] = [s for s in signals if s.get("id") != sig_id]
    after = len(c["signals"])
    if before > after:
        save_config(c)
        print(f"✅ 已移除信号: {sig_id}")
    else:
        print(f"⚠️ 未找到信号: {sig_id}")


def list_signals():
    c = load_config()
    signals = c.get("signals", [])
    if not signals:
        print("(无注册信号)")
        return
    print(f"当前信号 ({len(signals)}):")
    for s in signals:
        ctx = s.get("context_ref", "")
        ctx_str = f" ctx:{ctx[:20]}..." if ctx else ""
        print(f"  [{s.get('type','?')}] {s.get('id')} → {s.get('name')}({s.get('code')}){ctx_str}")
        print(f"    理由: {s.get('rationale','')[:80]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="信号注册工具")
    sub = parser.add_subparsers(dest="action")

    p_add = sub.add_parser("add")
    p_add.add_argument("--id", required=True)
    p_add.add_argument("--code", default="")
    p_add.add_argument("--name", default="")
    p_add.add_argument("--type", required=True)
    p_add.add_argument("--params", default="{}")
    p_add.add_argument("--rationale", default="")
    p_add.add_argument("--context-ref", default="")
    p_add.add_argument("--target", type=float, default=0)

    sub.add_parser("list")
    p_rm = sub.add_parser("remove")
    p_rm.add_argument("--id", required=True)

    args = parser.parse_args()
    if args.action == "add":
        add_signal(args.id, args.code, args.name, args.type,
                   json.loads(args.params), args.rationale,
                   args.context_ref, args.target)
    elif args.action == "list":
        list_signals()
    elif args.action == "remove":
        remove_signal(args.id)
    else:
        parser.print_help()

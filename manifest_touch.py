#!/config/quant_env/bin/python3
"""
manifest_touch.py — 组件运行时间戳自动回写

P2-6 修复: cron harness 或 post-hook 自动更新 system_manifest.json 的 actual_last_run。
避免手工维护导致误报红灯/漏报。

用法:
  python manifest_touch.py --component cron_21:00_yebao
  python manifest_touch.py --component smart_guard --note "手动触发"
"""

import json
import sys
import os
from datetime import datetime

MANIFEST_PATH = "/config/quant_scripts/system_manifest.json"

# cron job ID → manifest component ID 映射
CRON_TO_COMPONENT = {
    "5a69c039950e": "cron_08:30_panqian",
    "38a1c0401a1d": "cron_09:30_kaipan",
    "718bad2ea1fe": "cron_10:00_panzhong",
    "81c08b8f2cbe": "cron_11:30_wujian",
    "6907661c0a15": "cron_14:00_xiawu",
    "1af47883139e": "cron_15:05_shoupan",
    "dd8c45af9154": "cron_21:00_yebao",
    "d42222c31b23": "cron_weekend_zhoubao",
    "76ef0dd15954": "cron_emergency_push",
    "79612ad78789": "smart_guard_watchdog",
    "0ea4cbecc59a": "cron_19:00_component_audit",
}


def touch(component_id: str, note: str = ""):
    """更新指定组件的 actual_last_run"""
    if not os.path.exists(MANIFEST_PATH):
        print(f"ERROR: {MANIFEST_PATH} not found", file=sys.stderr)
        return False

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    found = False

    for comp in manifest.get("components", []):
        if comp["id"] == component_id:
            comp["actual_last_run"] = now
            if note:
                comp["note"] = f"{comp.get('note', '')} | {note}"
            found = True
            break

    if not found:
        print(f"WARN: component '{component_id}' not in manifest", file=sys.stderr)
        return False

    manifest["meta"]["last_updated"] = now
    manifest["meta"]["updated_by"] = "manifest_touch.py"

    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"OK: {component_id} → {now}")
    return True


def touch_by_cron_id(cron_id: str):
    """根据 cron job ID 更新对应组件"""
    component = CRON_TO_COMPONENT.get(cron_id)
    if not component:
        print(f"WARN: no manifest mapping for cron {cron_id}", file=sys.stderr)
        return False
    return touch(component)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="组件时间戳自动回写")
    p.add_argument("--component", help="manifest 组件 ID")
    p.add_argument("--cron-id", help="cron job ID（自动映射到组件）")
    p.add_argument("--note", default="", help="备注")
    args = p.parse_args()

    if args.cron_id:
        ok = touch_by_cron_id(args.cron_id)
    elif args.component:
        ok = touch(args.component, args.note)
    else:
        p.print_help()
        sys.exit(1)

    sys.exit(0 if ok else 1)

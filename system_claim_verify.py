#!/config/quant_env/bin/python3
"""
system_claim_verify.py — 组件声明校验器
========================================
Agent在声称任何系统组件状态前，必须先调用此脚本验证。

用法:
  python3 system_claim_verify.py --claim "FinRL PPO在生产级运行"
  python3 system_claim_verify.py --component rd_agent_factor_mining
  python3 system_claim_verify.py --check-all    # 全量审计摘要

输出:
  ✅ PASS — 声明与manifest一致
  ❌ FAIL — 声明与manifest矛盾，必须纠正后重新声明
"""

import json, sys, os, argparse
from datetime import datetime

MANIFEST_PATH = "/config/quant_scripts/system_manifest.json"

def load_manifest():
    if not os.path.exists(MANIFEST_PATH):
        return {"error": "manifest not found"}
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)

def find_component(manifest, claim=None, comp_id=None):
    """通过声明关键词或组件ID查找组件"""
    results = []
    for comp in manifest.get("components", []):
        name = comp.get("name", "").lower()
        cid = comp.get("id", "").lower()
        if comp_id and comp_id.lower() == cid:
            results.append(comp)
        elif claim and (claim.lower() in name or claim.lower() in cid):
            results.append(comp)
    return results

def verify_claim(claim_text, manifest):
    """验证一个声明是否与manifest一致"""
    # 尝试直接匹配组件
    comps = find_component(manifest, claim=claim_text)
    if not comps:
        return {
            "result": "UNKNOWN",
            "message": f"未找到与'{claim_text}'匹配的组件。请用更精确的组件名重试。",
            "suggestion": "使用 --check-all 查看所有组件"
        }
    
    results = []
    for comp in comps:
        status = comp.get("status", "unknown")
        last_run = comp.get("actual_last_run", "never")
        name = comp.get("name", "?")
        note = comp.get("note", "")
        
        # 判断声明是否准确
        is_active = status == "active"
        is_running = status == "active" and last_run and last_run != "never"
        
        if is_running:
            results.append({
                "result": "PASS",
                "component": name,
                "status": status,
                "last_run": last_run,
                "message": f"✅ {name} — 状态={status}, 最后运行={last_run}"
            })
        elif is_active and not is_running:
            results.append({
                "result": "WARN",
                "component": name,
                "status": status,
                "last_run": last_run,
                "message": f"🟡 {name} — 状态={status} 但从未运行！{note}"
            })
        else:
            results.append({
                "result": "FAIL",
                "component": name,
                "status": status,
                "last_run": last_run,
                "message": f"❌ {name} — 状态={status} (最后运行={last_run})。{note}"
            })
    
    return {"result": "MULTI", "checks": results}

def check_all(manifest):
    """输出全量审计摘要"""
    comps = manifest.get("components", [])
    active = [c for c in comps if c.get("status") == "active"]
    broken = [c for c in comps if c.get("status") in ("broken", "stale")]
    dormant = [c for c in comps if c.get("status") in ("dormant", "archived", "deprecated")]
    
    lines = [
        f"## 系统组件声明审计 ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
        f"",
        f"总组件: {len(comps)} | ✅ 活跃: {len(active)} | ❌ 故障: {len(broken)} | ⏸️ 休眠: {len(dormant)}",
        f""
        f"### ✅ 活跃组件（可声称'在生产运行'）"
    ]
    for c in active:
        last_run = c.get('actual_last_run')
        last_run_str = str(last_run)[:16] if last_run else "never"
        lines.append(f"  {c['name']} (last_run={last_run_str})")
    
    if broken:
        lines.append(f"\n### ❌ 故障/失活组件（不可声称'在生产运行'）")
        for c in broken:
            lines.append(f"  {c['name']} — {c.get('note','')}")
    
    if dormant:
        lines.append(f"\n### ⏸️ 休眠/归档组件（不可声称'在生产运行'）")
        for c in dormant:
            lines.append(f"  {c['name']} — {c.get('note','')}")
    
    lines.append(f"\n⚠️ 规则：声称组件状态前必须先调 system_claim_verify.py")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="系统组件声明校验器")
    parser.add_argument("--claim", help="验证一个声明（如'FinRL在生产运行'）")
    parser.add_argument("--component", help="按组件ID验证")
    parser.add_argument("--check-all", action="store_true", help="全量审计摘要")
    args = parser.parse_args()
    
    manifest = load_manifest()
    if "error" in manifest:
        print(f"FATAL: {manifest['error']}")
        sys.exit(2)
    
    if args.check_all:
        print(check_all(manifest))
    elif args.claim:
        result = verify_claim(args.claim, manifest)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if any(c.get("result") == "FAIL" for c in result.get("checks", [result])):
            sys.exit(1)
    elif args.component:
        comps = find_component(manifest, comp_id=args.component)
        if comps:
            c = comps[0]
            print(f"{c['name']}: status={c['status']}, last_run={c.get('actual_last_run','?')}")
            if c['status'] not in ('active',):
                sys.exit(1)
        else:
            print(f"组件 '{args.component}' 未找到")
            sys.exit(2)
    else:
        print(check_all(manifest))

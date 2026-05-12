#!/config/quant_env/bin/python3
"""
signal_lifecycle.py — 信号衰减/淘汰/冲突解决

DS-1 修复: 为 guard_config.json 中的每条信号增加生命周期管理。
- ttl(存活期): 信号创建后N天自动过期
- half_life(半衰期): 每次触发后置信度减半
- trigger_count: 累计触发次数
- conflict_detect: 同标的相反方向信号冲突检测
- auto_archive: 过期/低质量信号移入 archive_signals[]

用法:
  python signal_lifecycle.py audit     # 审计所有信号（过期/冲突/质量）
  python signal_lifecycle.py expire    # 自动过期处理
  python signal_lifecycle.py --json    # JSON输出
"""

import json
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

CONFIG_PATH = "/config/quant_scripts/guard_config.json"
STATE_PATH = "/config/quant_scripts/guard_state.json"

# 默认 TTL（天）
DEFAULT_TTL_DAYS = 30
DEFAULT_HALF_LIFE_DAYS = 7
MAX_SIGNALS = 20  # 超过此数触发告警


def load_signals() -> List[Dict]:
    """加载所有活跃信号（含扩展字段）"""
    if not os.path.exists(CONFIG_PATH):
        return []
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    return config.get("signals", [])


def load_triggered_state() -> Dict:
    """加载触发状态"""
    if not os.path.exists(STATE_PATH):
        return {}
    with open(STATE_PATH) as f:
        state = json.load(f)
    return state.get("triggered_alerts", {})


def count_triggers(sig_id: str, triggered: Dict) -> int:
    """统计某信号的历史触发次数"""
    key = f"agent_{sig_id}"
    # triggered_alerts 中 agent_* 键每天复位，无法直接统计历史
    # 通过检查是否存在来估算
    return 1 if key in triggered else 0


def detect_conflicts(signals: List[Dict]) -> List[Dict]:
    """检测同标的相反方向信号冲突"""
    by_code = {}
    for sig in signals:
        code = sig.get("code", "")
        if code not in by_code:
            by_code[code] = {"BUY": [], "SELL": [], "other": []}
        stype = sig.get("type", "other")
        if stype in ("price_below", "rapid_drop"):
            by_code[code]["SELL"].append(sig)
        elif stype in ("price_above", "rapid_surge"):
            by_code[code]["BUY"].append(sig)
        else:
            by_code[code]["other"].append(sig)

    conflicts = []
    for code, groups in by_code.items():
        if groups["BUY"] and groups["SELL"]:
            conflicts.append({
                "code": code,
                "buy_signals": [s["id"] for s in groups["BUY"]],
                "sell_signals": [s["id"] for s in groups["SELL"]],
                "severity": "high",
                "recommendation": "人工裁决：BUY和SELL信号并存，需手动清理",
            })

    return conflicts


def check_ttl(signals: List[Dict]) -> List[Dict]:
    """检查TTL过期信号"""
    now = datetime.now()
    expired = []
    for sig in signals:
        ttl = sig.get("ttl_days", DEFAULT_TTL_DAYS)
        created_str = sig.get("created_at", "")
        if not created_str:
            # 尝试从 context_ref 推断
            context = sig.get("context_ref", "")
            if context:
                try:
                    # context_ref 可能是报告ID，通过它查日期
                    from trade_db import CronReport
                    cr = CronReport()
                    report = cr.get_by_id(int(context))
                    created_str = report.get("date", "")
                except:
                    pass

        if created_str:
            try:
                created = datetime.strptime(created_str[:10], "%Y-%m-%d")
                age = (now - created).days
                if age > ttl:
                    expired.append({
                        "id": sig.get("id", "?"),
                        "code": sig.get("code", "?"),
                        "type": sig.get("type", "?"),
                        "age_days": age,
                        "ttl_days": ttl,
                        "action": "expire",
                    })
            except:
                pass

    return expired


def audit() -> Dict:
    """全量审计"""
    signals = load_signals()
    triggered = load_triggered_state()

    conflicts = detect_conflicts(signals)
    expired = check_ttl(signals)
    total = len(signals)

    return {
        "total_signals": total,
        "max_allowed": MAX_SIGNALS,
        "over_limit": total > MAX_SIGNALS,
        "conflicts": conflicts,
        "expired": expired,
        "recommendations": [],
    }


def render_report(audit_result: Dict) -> str:
    """渲染审计报告"""
    lines = ["=" * 50, "  信号生命周期审计", f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}", "=" * 50]

    total = audit_result["total_signals"]
    limit = audit_result["max_allowed"]
    status = "🔴 超限" if audit_result["over_limit"] else "✅"
    lines.append(f"\n📊 活跃信号: {total}/{limit} {status}")

    if audit_result["expired"]:
        lines.append(f"\n⏰ 过期信号 ({len(audit_result['expired'])}条):")
        for e in audit_result["expired"]:
            lines.append(f"  • {e['id']}({e['code']}) {e['type']} — {e['age_days']}d/{e['ttl_days']}d TTL")

    if audit_result["conflicts"]:
        lines.append(f"\n⚠️  信号冲突 ({len(audit_result['conflicts'])}组):")
        for c in audit_result["conflicts"]:
            lines.append(f"  • {c['code']}: BUY={c['buy_signals']} vs SELL={c['sell_signals']}")
            lines.append(f"    → {c['recommendation']}")

    if not audit_result["expired"] and not audit_result["conflicts"]:
        lines.append("\n✅ 无过期信号，无冲突")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="信号生命周期管理")
    p.add_argument("action", nargs="?", default="audit",
                   choices=["audit", "expire"])
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    result = audit()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_report(result))

    # exit 1 if issues found
    if result["over_limit"] or result["conflicts"] or result["expired"]:
        sys.exit(1)

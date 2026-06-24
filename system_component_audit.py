#!/usr/local/bin/python3
"""
system_component_audit.py — 系统组件使用审计
============================================
读取 system_manifest.json，检查每个组件是否在规定时间窗口内被执行。
输出红/黄/绿报告，永久化到 health_log/ 目录。

用法:
  python3 system_component_audit.py          # 普通输出
  python3 system_component_audit.py --json   # JSON输出
  python3 system_component_audit.py --push   # 生成紧急推送文件（有红灯时）
"""

import json, os, sys, subprocess, time
from datetime import datetime, timedelta
from system_config import cfg

MANIFEST_PATH = cfg.path.system_manifest
HEALTH_LOG_DIR = cfg.path.health_log_dir
EMERGENCY_SIGNAL = cfg.path.guard_emergency_signal
EMERGENCY_FILE = cfg.path.guard_emergency

REDS = []
YELLOWS = []
GREENS = []

def red(msg):
    REDS.append(f"🔴 {msg}")

def yellow(msg):
    YELLOWS.append(f"🟡 {msg}")

def green(msg):
    GREENS.append(f"✅ {msg}")

def parse_iso(dt_str):
    """安全解析各种ISO格式时间戳"""
    if not dt_str:
        return None
    for fmt in [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ]:
        try:
            return datetime.strptime(dt_str[:19], "%Y-%m-%dT%H:%M:%S")
        except:
            try:
                return datetime.strptime(dt_str[:19].rstrip("Z"), fmt[:len(dt_str[:19].rstrip("Z"))])
            except:
                continue
    return None

def check_alive(pid_check_cmd):
    """检查守护进程是否存活"""
    try:
        result = subprocess.run(pid_check_cmd, shell=True, capture_output=True, text=True, timeout=5)
        output = result.stdout.strip()
        return len(output.split("\n")) >= 1 if output else False
    except:
        return False

def audit_all():
    now = datetime.now()
    
    if not os.path.exists(MANIFEST_PATH):
        red(f"system_manifest.json 不存在！系统组件不被监控")
        return
    
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        manifest = json.load(f)
    
    components = manifest.get("components", [])
    green(f"组件注册表: {len(components)} 个组件")
    
    for comp in components:
        cid = comp["id"]
        name = comp["name"]
        status = comp.get("status", "unknown")
        expected_hours = comp.get("expected_interval_hours")
        last_run = comp.get("actual_last_run")
        note = comp.get("note", "")
        evidence = comp.get("evidence", "")
        
        # 已归档/休眠组件 — 仅黄灯提醒
        if status in ("archived", "dormant"):
            yellow(f"[{cid}] {name} — 状态={status}。{note}")
            continue
        
        # 已损坏组件 — 红灯
        if status == "broken":
            red(f"[{cid}] {name} — 组件损坏！{note}")
            continue
        
        # 失活组件 — 红灯
        if status == "stale":
            last_dt = parse_iso(last_run)
            if last_dt:
                days = (now - last_dt).days
                red(f"[{cid}] {name} — {days}天未运行！最后运行: {last_run}。{note}")
            else:
                red(f"[{cid}] {name} — 失活但无最后运行时间。{note}")
            continue
        
        # 活组件 — 检查时间窗口
        if expected_hours and last_run:
            last_dt = parse_iso(last_run)
            if last_dt:
                elapsed_hours = (now - last_dt).total_seconds() / 3600
                if elapsed_hours > expected_hours * 1.5:
                    red(f"[{cid}] {name} — 超期 {elapsed_hours:.1f}h (窗口={expected_hours}h)。最后: {last_run}")
                elif elapsed_hours > expected_hours:
                    yellow(f"[{cid}] {name} — 接近超期 {elapsed_hours:.1f}h (窗口={expected_hours}h)。最后: {last_run}")
                else:
                    green(f"[{cid}] {name} — {elapsed_hours:.1f}h前运行 (窗口={expected_hours}h)")
            else:
                yellow(f"[{cid}] {name} — last_run格式无法解析: {last_run}")
        elif expected_hours and not last_run:
            red(f"[{cid}] {name} — 从未运行！窗口={expected_hours}h")
        
        # 守护进程存活检查
        alive_cmd = comp.get("alive_check", "")
        if alive_cmd:
            alive = check_alive(alive_cmd)
            if alive:
                green(f"[{cid}] {name} — 进程存活 ✓")
            else:
                red(f"[{cid}] {name} — 进程已死！")
        
        # 推理引擎额外检查 — 模型文件是否过旧
        if cid == "finrl_ppo_inference":
            model_dir = cfg.path.models_dir + "/"
            if os.path.exists(model_dir):
                newest = 0
                for f in os.listdir(model_dir):
                    if f.endswith(".zip") or f.endswith(".pth"):
                        fpath = os.path.join(model_dir, f)
                        mtime = os.path.getmtime(fpath)
                        if mtime > newest:
                            newest = mtime
                if newest:
                    days = (now - datetime.fromtimestamp(newest)).days
                    if days > 14:
                        yellow(f"  模型文件最后修改: {days}天前，考虑是否需重新训练")
    
    # 额外统计检查
    green(f"TradingAgents: analyst_reports表 {count_table_rows('analyst_reports')} 条历史报告")
    green(f"Cron报告: cron_reports表 {count_table_rows('cron_reports')} 条记录")
    check_cron_report_h6_today()


def check_cron_report_h6_today():
    """当日交易类 cron_reports：artifact 缺失 (_hydrate_failed) 或仍占位且未注水 → 黄灯。"""
    try:
        import sqlite3
        from trade_db import CronReport, DB_PATH
    except Exception as e:
        yellow(f"Cron报告 H6 检查跳过: {e}")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    rtypes = tuple(CronReport.ARTIFACT_JSON_BY_REPORT_TYPE.keys())
    ph = ",".join("?" * len(rtypes))
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT id, job_name, report_type, content, key_metrics FROM cron_reports "
            f"WHERE date = ? AND report_type IN ({ph}) ORDER BY id",
            (today,) + rtypes,
        ).fetchall()
        conn.close()
    except Exception as e:
        yellow(f"Cron报告 H6 查询失败: {e}")
        return

    issues = 0
    for row in rows:
        raw_km = row["key_metrics"] or "{}"
        try:
            km = json.loads(raw_km) if isinstance(raw_km, str) else {}
        except json.JSONDecodeError:
            km = {}
        if km.get("_hydrate_failed"):
            yellow(
                f"Cron报告 H6: id={row['id']} {row['job_name']} — artifact 缺失 "
                f"_hydrate_failed={km.get('_hydrate_failed')}"
            )
            issues += 1
        elif CronReport._is_placeholder_content(row["content"] or "") and not km.get("_hydrated_from"):
            yellow(
                f"Cron报告 H6: id={row['id']} {row['job_name']} — 正文仍为过短/占位且未注水 "
                f"(report_type={row['report_type']})"
            )
            issues += 1
        if issues >= 12:
            yellow("Cron报告 H6: 已达 12 条展示上限，其余请直接查 trade_log.db")
            break

    if issues == 0:
        if rows:
            green(f"Cron报告(H6/今日 {today}): 已扫描 {len(rows)} 条交易类报告，无占位未注水 / 无 _hydrate_failed")
        else:
            green(f"Cron报告(H6/今日 {today}): 无交易类报告行（正常若尚未跑档）")


def count_table_rows(table):
    try:
        import sqlite3
        conn = sqlite3.connect(cfg.path.trade_db)
        c = conn.cursor()
        c.execute(f"SELECT COUNT(*) FROM {table}")
        return c.fetchone()[0]
    except:
        return "?"

def generate_report(format="text"):
    audit_all()
    
    dt = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    if format == "json":
        return json.dumps({
            "time": dt,
            "reds": len(REDS),
            "yellows": len(YELLOWS),
            "greens": len(GREENS),
            "items": {"red": REDS, "yellow": YELLOWS, "green": GREENS}
        }, ensure_ascii=False, indent=2)
    
    lines = [f"## 🔍 系统组件审计报告 ({dt})"]
    
    n_red = len(REDS)
    n_yellow = len(YELLOWS)
    n_green = len(GREENS)
    total = n_red + n_yellow + n_green
    
    lines.append(f"\n**概览**: 🔴{n_red} / 🟡{n_yellow} / ✅{n_green} / 总计{total}\n")
    
    if REDS:
        lines.append(f"### 🔴 警报 ({n_red})")
        lines.extend(REDS)
    
    if YELLOWS:
        lines.append(f"\n### 🟡 观察 ({n_yellow})")
        lines.extend(YELLOWS)
    
    if GREENS:
        lines.append(f"\n### ✅ 正常 ({n_green})")
        lines.extend(GREENS)
    
    lines.append(f"\n---\n组件审计 v1.0 | 来源: system_manifest.json")
    return "\n".join(lines)


def push_emergency(report):
    """有红灯时写入紧急推送通道"""
    if not REDS:
        return
    
    summary = f"🔴 组件审计: {len(REDS)}个组件异常\n\n"
    for r in REDS[:5]:
        summary += r + "\n"
    if len(REDS) > 5:
        summary += f"... 及另外{len(REDS)-5}项"
    
    with open(EMERGENCY_SIGNAL, "w") as f:
        f.write("COMPONENT_AUDIT")
    with open(EMERGENCY_FILE, "w") as f:
        f.write(summary)
    print(f"[PUSH] 已写入紧急通道: {summary[:100]}...")


# ========== DS-4: 紧急消费可观测性 ==========

def check_emergency_consumption():
    """检查紧急信号是否被 cron 消费
    
    DS-4 修复: 信号文件写入≠已被消费。若文件时间戳超阈值
    仍无 cron_reports 消费记录 → 推送告警。
    """
    import sqlite3
    DB = cfg.path.trade_db
    
    if not os.path.exists(EMERGENCY_SIGNAL):
        return
    
    # 检查信号文件修改时间
    signal_mtime = os.path.getmtime(EMERGENCY_SIGNAL)
    signal_age_min = (time.time() - signal_mtime) / 60
    
    # 读取信号内容（非空才检查）
    try:
        with open(EMERGENCY_SIGNAL) as f:
            content = f.read().strip()
    except:
        return
    
    if not content or content == "COMPONENT_AUDIT":
        return
    
    # 信号超过5分钟但少于2小时 → 应已被消费
    if signal_age_min < 5:
        return  # 刚写入，给cron时间
    if signal_age_min > 120:
        return  # 太旧，可能是历史遗留
    
    # 查询是否有对应的紧急cron消费记录
    try:
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        signal_time = datetime.fromtimestamp(signal_mtime)
        since = signal_time.strftime("%Y-%m-%d %H:%M:%S")
        
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM cron_reports "
            "WHERE job_name LIKE '%紧急%' AND created_at >= ?",
            [since]
        ).fetchone()
        conn.close()
        
        if not row or row["cnt"] == 0:
            red(f"⚠️ 紧急信号未被消费: {signal_age_min:.0f}分钟前写入，"
                f"无对应cron消费记录 (内容: {content[:60]}...)")
    except Exception as e:
        yellow(f"紧急消费检查异常: {e}")


if __name__ == "__main__":
    do_push = "--push" in sys.argv
    
    report = generate_report("text")
    print(report)
    
    # DS-4: 紧急消费可观测性检查
    check_emergency_consumption()
    
    # 持久化
    os.makedirs(HEALTH_LOG_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    with open(f"{HEALTH_LOG_DIR}/{today}_audit.md", "w") as f:
        f.write(report)
    
    if do_push:
        push_emergency(report)
    
    # 有红灯时 exit 1
    if REDS:
        sys.exit(1)

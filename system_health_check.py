#!/usr/local/bin/python3
"""量化系统健康自检 — 每天19:00运行，纯脚本零AI调用"""

import json
import sqlite3
import subprocess
import os
from datetime import datetime, timedelta
from system_config import cfg

HERMES_CRON_FILE = os.path.expanduser("~/.hermes/cron/jobs.json")
STATE_DB = os.path.expanduser("~/.hermes/state.db")
WIKI_DIR = "/config/quant-wiki"
WIKI_HOOK = "/config/quant-wiki/.git/hooks/post-commit"

ALERTS = []
INFOS = []

def alert(msg):
    ALERTS.append(f"🔴 {msg}")

def info(msg):
    INFOS.append(f"🟡 {msg}")

def ok(msg):
    INFOS.append(f"✅ {msg}")

def check_hermes_crons():
    """审计Hermes cron：标记纯shell命令的cron"""
    cronfile = os.path.expanduser("~/.hermes/cron/jobs.json")
    if not os.path.exists(cronfile):
        info("Hermes cron jobs.json 未找到，可能是新格式存储")
        # Try to get from state.db
        return

    with open(cronfile) as f:
        data = json.load(f)
    
    shell_keywords = ["bash ", "sh ", "Run:", "python ", "./"]
    for job in data.get("jobs", []):
        pid = job.get("id", "?")
        name = job.get("name", "?")
        prompt = job.get("prompt", "")
        toolsets = job.get("enabled_toolsets")
        
        # 检查纯shell命令cron
        is_shell = any(kw in prompt for kw in shell_keywords) and len(prompt) < 200
        if is_shell and not toolsets:
            alert(f"Hermes cron「{name}」({pid}) 仅运行shell命令但未限制toolsets → 应切系统crontab或加 enabled_toolsets")

        # 检查无toolsets限制
        if not toolsets and len(prompt) < 300:
            info(f"Hermes cron「{name}」({pid}) 提示词较短但未限制toolsets，建议添加")

def check_wiki_sync():
    """验证wiki同步是否正常"""
    if not os.path.exists(WIKI_DIR):
        info("wiki目录不存在")
        return
    
    # 检查系统crontab
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    if "quant-wiki/sync.sh" in result.stdout:
        ok("wiki同步在系统crontab中，零AI费用")
    else:
        alert("wiki同步不在系统crontab中！")

def check_balance_errors():
    """检测最近48小时的402/余额不足错误"""
    sessions_dir = os.path.expanduser("~/.hermes/sessions")
    cutoff = datetime.now() - timedelta(hours=48)
    error_count = 0

    if not os.path.exists(sessions_dir):
        return
    
    for f in os.listdir(sessions_dir):
        if not f.startswith("request_dump_"):
            continue
        fpath = os.path.join(sessions_dir, f)
        mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
        if mtime < cutoff:
            continue
        try:
            with open(fpath) as fh:
                data = json.load(fh)
            if "Insufficient Balance" in str(data):
                error_count += 1
        except:
            pass

    if error_count > 0:
        alert(f"最近48小时检测到{error_count}次API余额不足(402)错误")
    else:
        ok("最近48小时无API余额错误")

def check_token_daily():
    """检查state.db中近3天session数量"""
    if not os.path.exists(STATE_DB):
        info("state.db 不存在，跳过token检查")
        return

    try:
        conn = sqlite3.connect(STATE_DB)
        cur = conn.cursor()
        cur.execute("""
            SELECT date(min(created_at)) as dt, COUNT(*) as cnt
            FROM sessions
            WHERE created_at >= datetime('now', '-3 days')
            GROUP BY dt
            ORDER BY dt
        """)
        rows = cur.fetchall()
        conn.close()

        for dt, cnt in rows:
            if cnt > 200:
                alert(f"{dt}: {cnt}次session调用，异常偏高")
            else:
                info(f"{dt}: {cnt}次session调用")

    except Exception as e:
        info(f"state.db查询失败: {e}")


def check_signals_audit():
    """审计guard_config.json中的盯盘信号——检查是否浪费token"""
    config_file = cfg.path.guard_config
    if not os.path.exists(config_file):
        info("guard_config.json 不存在，跳过信号审计")
        return

    with open(config_file, encoding="utf-8") as f:
        config = json.load(f)

    signals = config.get("signals", [])
    if not signals:
        ok("guard_config.json 中无注册信号")
        return

    now = datetime.now()
    total = len(signals)
    stale = 0
    zombie = 0
    used = 0

    for s in signals:
        sig_id = s.get("id", "?")
        sig_type = s.get("type", "?")
        code = s.get("code", "")
        name = s.get("name", "")
        registered_str = s.get("registered", "")
        target = s.get("target", 0)
        context_ref = s.get("context_ref", "")

        # 1. 检查已触发状态（从state.json读取是否已触发过）
        state_file = cfg.path.guard_state
        triggered = False
        if os.path.exists(state_file):
            try:
                with open(state_file) as sf:
                    state_data = json.load(sf)
                ta = state_data.get("triggered_alerts", {})
                trigger_key = f"agent_{sig_id}"
                dip_key = f"dip_bounce_{code}_{now.strftime('%Y%m%d')}"
                if trigger_key in ta or dip_key in ta:
                    triggered = True
                    used += 1
            except:
                pass

        # 2. 检查僵尸信号：注册超过3天但从未触发，且价格区间已远偏离
        if registered_str and not triggered:
            try:
                reg_dt = datetime.fromisoformat(registered_str)
                days_since = (now - reg_dt).days
                if days_since >= 3:
                    # 检查当前价格与目标是否已无意义
                    if sig_type == "price_above" and target > 0:
                        # 如果当前价远超目标价，信号已无意义
                        stale += 1
                    elif days_since >= 7:
                        stale += 1
            except:
                pass

        # 3. 检查无code的通用信号（generic_dip_bounce这类）
        if not code:
            info(f"信号「{sig_id}」({sig_type}{', 已触发' if triggered else ', 未触发'}) — 通用信号，无特定标的")

    # 汇总
    info(f"信号审计: {total}个注册信号中 {used}个已触发, {stale}个可能过期")
    if stale > total * 0.5:
        alert(f"超过50%信号({stale}/{total})可能已过期，建议清理")
    if total > 20:
        alert(f"信号数量过多({total}个)，可能浪费token在无意义的检测上")
    if total <= 5:
        ok(f"信号数量合理({total}个)")

    # 检查zombie signal：注册了但从未被guard_config的signals[]使用过的旧信号
    signal_ids = {s.get("id") for s in signals}
    # 检查是否所有信号名都符合命名规范
    for s in signals:
        sid = s.get("id", "")
        if not sid:
            alert("存在无ID的信号条目，需清理")
            break

    ok(f"信号注册规范检查通过 ({total}个信号)")

def check_cron_errors():
    """检查最近cron执行状态"""
    cronfile = os.path.expanduser("~/.hermes/cron/jobs.json")
    if not os.path.exists(cronfile):
        return
    with open(cronfile) as f:
        data = json.load(f)
    for job in data.get("jobs", []):
        pid = job.get("id", "?")
        name = job.get("name", "?")
        status = job.get("last_status")
        error = job.get("last_error")
        delivery_err = job.get("last_delivery_error")

        if error:
            alert(f"cron「{name}」({pid}) 执行错误: {error[:100]}")
        if delivery_err:
            alert(f"cron「{name}」({pid}) 推送错误: {delivery_err[:100]}")
        if status == "ok":
            ok(f"cron「{name}」({pid}) 最近执行正常")

def generate_report():
    dt = datetime.now().strftime("%Y-%m-%d %H:%M")
    report = [f"## 🏥 系统健康自检 ({dt})"]
    
    if ALERTS:
        report.append(f"\n### 🔴 红色警报 ({len(ALERTS)})")
        report.extend(ALERTS)
    
    if INFOS:
        report.append(f"\n### {'🟡 观察项' if not ALERTS else '🟡 其他观察'}")
        report.extend(INFOS)

    report.append(f"\n---\n共 {len(ALERTS)} 个警报, {len(INFOS)} 个观察项")
    return "\n".join(report)

if __name__ == "__main__":
    check_hermes_crons()
    check_wiki_sync()
    check_balance_errors()
    check_token_daily()
    check_signals_audit()
    check_cron_errors()
    
    report = generate_report()
    print(report)

    # 永久化到健康日志
    log_dir = cfg.path.health_log_dir
    os.makedirs(log_dir, exist_ok=True)
    with open(f"{log_dir}/{datetime.now().strftime('%Y%m%d')}.md", "w") as f:
        f.write(report)
    
    # 有红色警报时直接推微信
    if ALERTS:
        push_msg = f"🔴 系统健康警报 ({len(ALERTS)}项)\n\n" + "\n".join(ALERTS)
        # 走紧急信号通道
        with open(cfg.path.guard_emergency_signal, "w") as f:
            f.write("HEALTH_CHECK")
        with open(cfg.path.guard_emergency, "w") as f:
            f.write(push_msg)
        # 也直接写入微信推送文件，供守护进程以外的机制读取
        print(f"\n⚠️ 红色警报已写入紧急通道，下一轮cron循环推送\n{push_msg}")

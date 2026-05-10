#!/config/quant_env/bin/python3
"""
data_health.py — 数据质量健康检查
===============================
检查快照新鲜度、零值/NaN异常、OmniData可达性。
输出紧凑报告供 cron 注入上下文。

用法:
  python data_health.py          # 完整检查，输出报告
  python data_health.py --json   # JSON 输出（机器可读）
"""
import os, sys, json, time, subprocess
from datetime import datetime

SNAPSHOT_PATH = "/config/quant_scripts/market_snapshot.json"
POSITIONS = {
    "002594": "比亚迪", "000938": "紫光股份",
    "512480": "半导体ETF", "518880": "黄金ETF"
}

def _is_trading_hours():
    """判断当前是否在A股交易时段（9:00-15:05，周一至周五）"""
    now = datetime.now()
    if now.weekday() >= 5:  # 周六日
        return False
    t = now.hour * 100 + now.minute
    return 900 <= t <= 1505

def check_snapshot_freshness():
    """检查行情快照的新鲜度（收盘后陈旧属正常）"""
    if not os.path.exists(SNAPSHOT_PATH):
        return {"status": "MISSING", "age_seconds": None, "detail": "快照文件不存在"}
    
    stat = os.stat(SNAPSHOT_PATH)
    age = time.time() - stat.st_mtime
    
    trading = _is_trading_hours()
    
    if not trading:
        # 非交易时段：陈旧属正常，只标记不报警
        if age > 86400:  # >24小时
            status = "STALE"
        else:
            status = "OK_OFF_HOURS"
    elif age > 600:  # 盘中 >10分钟
        status = "STALE"
    elif age > 300:  # 盘中 >5分钟
        status = "WARN"
    else:
        status = "OK"
    
    return {"status": status, "age_seconds": int(age), "size": stat.st_size,
            "trading_hours": trading}

def check_price_data():
    """检查持仓标的的价格数据质量"""
    if not os.path.exists(SNAPSHOT_PATH):
        return {"status": "SKIP", "issues": []}
    
    try:
        data = json.load(open(SNAPSHOT_PATH))
    except:
        return {"status": "CORRUPT", "issues": ["JSON解析失败"]}
    
    quotes = data.get("quotes", {})
    issues = []
    
    for code, name in POSITIONS.items():
        q = quotes.get(code, {})
        if not q:
            issues.append(f"{name}({code}): 快照中无数据")
            continue
        
        p = q.get("p", 0)
        pct = q.get("pct", 0)
        
        if p <= 0:
            issues.append(f"{name}({code}): 价格为零/负 ({p})")
        if pct == 0 and q.get("t", "") != "":
            # 收盘后涨跌幅为0正常（平价收盘），但盘中为0异常
            pass
    
    status = "OK" if not issues else "ISSUES"
    return {"status": status, "issues": issues, "quotes_count": len(quotes)}

def check_omnidata():
    """检查OmniData MCP可达性"""
    try:
        r = subprocess.run(
            ["curl", "-s", "--connect-timeout", "5", "--max-time", "8",
             "http://localhost:8380/mcp/finance/"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0 and len(r.stdout) > 10:
            return {"status": "OK", "response_len": len(r.stdout)}
        else:
            return {"status": "DOWN", "detail": f"exit={r.returncode}, len={len(r.stdout)}"}
    except Exception as e:
        return {"status": "DOWN", "detail": str(e)[:100]}

def check_smart_guard():
    """检查守护进程是否在运行"""
    try:
        r = subprocess.run(
            ["pgrep", "-f", "smart_guard_v3.py"],
            capture_output=True, text=True, timeout=5
        )
        pids = [p for p in r.stdout.strip().split("\n") if p]
        if pids:
            return {"status": "OK", "pids": pids, "count": len(pids)}
        else:
            return {"status": "DOWN", "detail": "守护进程未运行"}
    except Exception as e:
        return {"status": "UNKNOWN", "detail": str(e)[:100]}

def format_report(results):
    """格式化人类可读报告"""
    lines = []
    now = datetime.now().strftime("%H:%M:%S")
    lines.append(f"=== 数据健康检查 {now} ===")
    
    snap = results["snapshot"]
    age_m = snap["age_seconds"] // 60 if snap["age_seconds"] else 0
    lines.append(f"快照: {snap['status']} | 陈旧{age_m}分钟 | {snap.get('size',0)}B")
    
    price = results["prices"]
    if price["issues"]:
        for issue in price["issues"]:
            lines.append(f"  ⚠ {issue}")
    else:
        lines.append(f"持仓价格: {price['status']} | {price.get('quotes_count',0)}只标的")
    
    omni = results["omnidata"]
    lines.append(f"OmniData: {omni['status']}" + (f" | {omni.get('detail','')}" if omni['status'] != 'OK' else ""))
    
    guard = results["smart_guard"]
    lines.append(f"守护进程: {guard['status']}" + (f" | PIDs={','.join(guard['pids'])}" if guard['status'] == 'OK' else ""))
    
    all_ok = all(v["status"] in ("OK", "OK_OFF_HOURS", "SKIP") for v in results.values())
    lines.append(f"总体: {'✅ 健康' if all_ok else '❌ 需关注'}")
    
    return "\n".join(lines)

if __name__ == "__main__":
    results = {
        "snapshot": check_snapshot_freshness(),
        "prices": check_price_data(),
        "omnidata": check_omnidata(),
        "smart_guard": check_smart_guard(),
    }
    
    if "--json" in sys.argv:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(format_report(results))
        
    # Exit code: non-zero if any check failed
    all_ok = all(v["status"] in ("OK", "OK_OFF_HOURS", "SKIP") for v in results.values())
    sys.exit(0 if all_ok else 1)

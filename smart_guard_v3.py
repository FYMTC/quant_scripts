#!/usr/bin/env python3
"""
智能盯盘守护系统 v3.0 — 热加载 + 主动推送
===========================================
核心改进：
1. 配置热加载（已有，增强）
2. 主动微信推送（通过 Hermes MCP 或直接 curl 企业微信机器人）
3. 更智能的异常检测
4. 守护进程自我监控

推送机制：使用企业微信机器人 Webhook（如未配置则用文件信号量）
"""

import time
import json
import os
import sys
import subprocess
import hashlib
from datetime import datetime, date
from threading import Lock
from zoneinfo import ZoneInfo
from trade_db import TradeDB, MarketSnapshot, DailyPlan, log_and_snapshot
from risk_metrics import calc_cvar
from agent_desk_config import DESK_LLM_CRON_ID
from system_config import cfg

# ========== TradingAgents 风控集成 ==========
RISK_CHECK_SCRIPT = os.path.join(os.path.dirname(__file__), "risk_check.py")
PYTHON_BIN = cfg.python

# ========== 配置 ==========
CONFIG_FILE = cfg.path.guard_config
STATE_FILE = cfg.path.guard_state
SIGNAL_FILE = cfg.path.guard_emergency_signal
ALERT_FILE = cfg.path.guard_emergency
HEARTBEAT_FILE = cfg.path.guard_heartbeat
PUSHLOG_FILE = cfg.path.guard_pushlog

# 企业微信机器人 Webhook（如有）
WEBHOOK_URL = ""  # 留空则用文件信号量+本地推送
CST = ZoneInfo("Asia/Shanghai")

# ========== 状态 ==========
state = {"triggered_alerts": {}, "last_prices": {}, "last_push_time": {},
          "price_history": {}, "cvar_baseline": {}, "last_cvar_check_day": "",
          "vol_history": {}, "avg_volumes": {}, "trading_day": ""}
_state_lock = Lock()

_config_cache = None
_config_mtime = 0
_bind_signature = None


def _runtime_health_snapshot(cfg):
    runtime = dict(cfg.get("runtime_health") or {})
    positions = cfg.get("positions") or {}
    watch_list = cfg.get("watch_list") or {}
    monitored_codes = cfg.get("monitored_codes") or {}
    signals = cfg.get("signals") or []
    runtime.setdefault("positions_count", len(positions))
    runtime.setdefault("watch_list_count", len(watch_list))
    runtime.setdefault("monitored_codes_count", len(monitored_codes))
    runtime.setdefault("signals_count", len(signals))
    runtime.setdefault("has_positions", bool(positions))
    runtime.setdefault("has_watch_list", bool(watch_list))
    runtime.setdefault("contract_hollow", not positions and not watch_list)
    runtime.setdefault(
        "watchlist_degraded_to_monitored_codes",
        bool(monitored_codes) and not bool(cfg.get("watch_list")),
    )
    return runtime


def _evaluate_runtime_blindness(cfg, quotes, cycle_count, fetch_time):
    runtime = _runtime_health_snapshot(cfg)
    critical_reasons = []
    warning_reasons = []
    if runtime.get("contract_hollow"):
        critical_reasons.append("持仓与自选同时为空")
    if runtime.get("watchlist_degraded_to_monitored_codes"):
        warning_reasons.append("自选池回退到 monitored_codes 导出视图")
    if runtime.get("watch_list_count", 0) <= 1 and runtime.get("positions_count", 0) <= 1:
        warning_reasons.append("监控范围过小")
    if runtime.get("signals_count", 0) == 0:
        critical_reasons.append("signals 为空")
    if quotes is not None and not quotes and (runtime.get("positions_count", 0) > 0 or runtime.get("watch_list_count", 0) > 0):
        critical_reasons.append("行情抓取结果为空")
    if fetch_time is not None and fetch_time > 20:
        warning_reasons.append(f"行情获取耗时过长({fetch_time:.1f}s)")
    if state.get("last_heartbeat_status") == "idle":
        last_hb = state.get("last_heartbeat_at")
        try:
            if last_hb:
                age = (_now_bj() - datetime.fromisoformat(last_hb)).total_seconds()
                if age > 600:
                    warning_reasons.append(f"heartbeat 超过 600s 未更新 ({age:.1f}s)")
        except Exception:
            pass

    reasons = critical_reasons + warning_reasons
    severity = "critical" if critical_reasons else ("warning" if warning_reasons else "healthy")
    prev_reasons = state.get("runtime_blindness", {}).get("reasons", [])
    prev_severity = state.get("runtime_blindness", {}).get("severity")
    if reasons == prev_reasons and severity == prev_severity:
        consecutive = state.get("runtime_blindness", {}).get("consecutive", 0) + 1
    else:
        consecutive = 1 if reasons else 0

    blindness = {
        "status": "blind" if critical_reasons else ("degraded" if warning_reasons else "healthy"),
        "severity": severity,
        "reasons": reasons,
        "critical_reasons": critical_reasons,
        "warning_reasons": warning_reasons,
        "consecutive": consecutive,
        "cycle": cycle_count,
        "checked_at": _now_bj().isoformat(),
    }
    state["runtime_blindness"] = blindness
    return blindness


def _emit_runtime_blindness_alert(blindness):
    if blindness.get("status") != "blind":
        return []
    if blindness.get("consecutive", 0) < 3:
        return []

    today = _today_bj()
    reason_slug = hashlib.md5("|".join(blindness.get("reasons", [])).encode("utf-8")).hexdigest()[:8]
    trigger_key = f"runtime_blind_{today}_{reason_slug}"
    if trigger_key in state.get("triggered_alerts", {}):
        return []

    state.setdefault("triggered_alerts", {})[trigger_key] = _now_bj().isoformat()
    reason = "；".join(blindness.get("reasons", []))
    msg = f"[SYSTEM_BLIND] smart_guard运行态失明|{reason}|连续{blindness.get('consecutive', 0)}轮"
    print(f"[{_now_bj().strftime('%H:%M:%S')}] 🧯 {msg}", flush=True)
    return [("🧯", msg)]

def _now_bj():
    return datetime.now(CST)


def _today_bj():
    return _now_bj().strftime("%Y%m%d")



def push_wechat(content: str, alert_type: str = "⚠️"):
    """主动推送微信消息"""
    timestamp = _now_bj().strftime('%H:%M:%S')
    msg = f"{alert_type} {timestamp}\n{content}"

    # 方式1: 企业微信机器人（如配置）
    if WEBHOOK_URL:
        try:
            payload = json.dumps({"msgtype": "markdown", "markdown": {"content": msg}})
            subprocess.run(
                ["curl", "-s", "-X", "POST", WEBHOOK_URL,
                 "-H", "Content-Type: application/json",
                 "-d", payload],
                capture_output=True, timeout=10
            )
            _log_push("webhook", content[:60])
            return True
        except Exception as e:
            print(f"[{timestamp}] ⚠️ Webhook 失败: {e}", flush=True)

    # 方式2: 写入信号文件，等待 cronjob 3分钟轮回推送
    # 写入 ALERT_FILE（完整告警正文，供人类阅读）
    with open(ALERT_FILE, "w", encoding="utf-8") as f:
        f.write(f"{alert_type} **盯盘警报** {alert_type}\n{content}\n⏰ {timestamp}")
    # 写入 SIGNAL_FILE（cron 解析用）
    # P0-1 修复: 含 [AGENT_ALERT] 的正文同步写入 SIGNAL_FILE，确保 cron 能提取
    signal_content = content
    is_agent_alert = "[AGENT_ALERT]" in content
    if is_agent_alert:
        # 提取所有 [AGENT_ALERT] 行 + PUSH 标记
        agent_lines = [l for l in content.split("\n") if "[AGENT_ALERT]" in l]
        signal_content = "\n".join(agent_lines)
    with open(SIGNAL_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n🔴 PUSH:{timestamp}\n{signal_content}\n")
    _log_push("signal_file", content[:60])
    print(f"[{timestamp}] 🚀 已写入推送信号", flush=True)

    # v5: Agent 信号入队 + 去抖唤醒 Agent Desk（非 */1 扫文件念报告）
    if is_agent_alert:
        try:
            from agent_queue import enqueue_from_alert_message, should_wake_desk, touch_wake_lock

            for line in content.split("\n"):
                if "[AGENT_ALERT]" in line:
                    enqueue_from_alert_message(line)
            if should_wake_desk():
                touch_wake_lock()
                subprocess.Popen(
                    ["hermes", "cron", "run", DESK_LLM_CRON_ID],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(f"[{timestamp}] 📞 v5 Agent Desk 唤醒 (queue)", flush=True)
        except Exception as e:
            print(f"[{timestamp}] ⚠️ Agent Desk 入队/唤醒失败: {e}", flush=True)

    return True


def push_startup():
    """守护进程启动通知"""
    t = _now_bj().strftime('%Y-%m-%d %H:%M:%S')
    cfg = _config_cache or {}
    push_wechat(
        f"🤖 盯盘守护已启动 @ {t}\n"
        f"轮询间隔: 30秒 | 持仓: {len(cfg.get('positions',{}))} 只\n"
        f"自选: {len(cfg.get('watch_list',{}))} 只",
        "✅"
    )


def _log_push(method: str, summary: str):
    """记录推送日志"""
    with open(PUSHLOG_FILE, "a") as f:
        f.write(f"[{_now_bj().isoformat()}] {method} | {summary}\n")


def _write_heartbeat(status: str, cycle_count: int, alerts_count: int = 0, extra: str = ""):
    payload = f"{_now_bj().isoformat()}|{status}|cycle={cycle_count}|alerts={alerts_count}"
    if extra:
        payload += f"|{extra}"
    with open(HEARTBEAT_FILE, "w") as f:
        f.write(payload)
    state["last_heartbeat_status"] = status
    state["last_heartbeat_at"] = _now_bj().isoformat()


# ========== 配置管理 ==========

def load_config():
    """热加载：操盘主账户 / guard 池 / trade_log.db / 模拟盘持仓 变更均自动生效（~30s 内）。"""
    global _config_cache, _config_mtime, _bind_signature
    try:
        from guard_account_bind import bind_signature, load_guard_bundle

        sig = bind_signature()
        if sig != _bind_signature or _config_cache is None:
            prev_aid = (_config_cache or {}).get("guard_account_id")
            bundle = load_guard_bundle()
            cfg = bundle["config"]
            aid = bundle.get("account_id", "?")
            _config_cache = cfg
            _bind_signature = sig
            _config_mtime = os.path.getmtime(cfg.get("guard_config_path", CONFIG_FILE))
            switch = f"（切换 {prev_aid}→{aid}）" if prev_aid and prev_aid != aid else ""
            print(
                f"[{_now_bj().strftime('%H:%M:%S')}] 📄 guard 热加载 account={aid}{switch} "
                f"持仓{len(_config_cache.get('positions', {}))}只 "
                f"自选{len(_config_cache.get('watch_list', {}))}只 "
                f"信号{len(_config_cache.get('signals', []))}个 "
                f"src={_config_cache.get('position_source_note', '-')}",
                flush=True,
            )
    except Exception as e:
        print(f"[{_now_bj().strftime('%H:%M:%S')}] ⚠️ 配置加载失败: {e}", flush=True)
        if _config_cache is None:
            try:
                with open(CONFIG_FILE, encoding="utf-8") as f:
                    _config_cache = json.load(f)
            except Exception:
                raise RuntimeError("无法加载配置文件") from e
    if _config_cache is None:
        raise RuntimeError("无法加载配置文件")

    if "watch_list" not in _config_cache or not _config_cache["watch_list"]:
        _config_cache["watch_list"] = _config_cache.get("monitored_codes", {})

    return _config_cache


def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
        except:
            pass


def save_state():
    with _state_lock:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)


# ========== 数据获取 ==========

def fetch_quote(code):
    """获取单个股票/ETF行情 — P1-6: 统一走 market_data 模块"""
    from market_data import fetch_quote as _md_fetch
    q = _md_fetch(code)
    if not q:
        return None
    
    # market_data 使用英文字段 → smart_guard 内部使用中文字段（保持兼容）
    is_etf = q.get("etf", False)
    return {
        "code": code,
        "最新价": q["price"],
        "最高": q["high"],
        "最低": q["low"],
        "今开": q["open"],
        "昨收": q["pre_close"],
        "涨跌幅": q["pct"],
        "涨跌额": (q["price"] - q.get("pre_close", 0)),
        "成交量(手)": q["vol"],
        "成交额(万)": q["amount"],
        "换手(%)": q.get("turnover", 0),
        "名称": q["name"],
        "时间": q.get("time", ""),
        "_is_etf": is_etf,
    }


def fetch_quotes_batch(codes: list):
    """批量获取行情 — P1-6: 统一走 market_data 模块"""
    import signal as _signal

    def _timeout_handler(signum, frame):
        raise TimeoutError("行情获取超时(30s)")

    old_handler = _signal.signal(_signal.SIGALRM, _timeout_handler)
    _signal.alarm(30)  # 30秒超时
    try:
        from market_data import fetch_quotes_batch as _md_batch
        results = _md_batch(codes)
    finally:
        _signal.alarm(0)
        _signal.signal(_signal.SIGALRM, old_handler)

    # market_data 返回英文字段，需要转换（防御性：处理缺失键）
    converted = {}
    for code, q in (results or {}).items():
        try:
            is_etf = q.get("etf", False)
            converted[code] = {
            "code": code,
            "最新价": q["price"],
            "最高": q["high"],
            "最低": q["low"],
            "今开": q["open"],
            "昨收": q["pre_close"],
            "涨跌幅": q["pct"],
            "涨跌额": (q["price"] - q.get("pre_close", 0)),
            "成交量(手)": q["vol"],
            "成交额(万)": q["amount"],
            "换手(%)": q.get("turnover", 0),
            "名称": q["name"],
            "时间": q.get("time", ""),
            "_is_etf": is_etf,
        }
        except (KeyError, TypeError) as ke:
            print(f"  ⚠ {code} 字段缺失: {ke}，跳过", flush=True)
            continue
    return converted


def fetch_fund_flow(code):
    """主力资金流向（OmniData）"""
    secid = f"0.{code}" if code.startswith(("0", "3")) else f"1.{code}"
    # P3-1: 统一走 omnidata_config
    try:
        from omnidata_config import OMNIDATA_API_URL
        api_url = OMNIDATA_API_URL
    except ImportError:
        api_url = "http://localhost:8380/api/v1"
    cmd = f"""curl -s --connect-timeout 8 --max-time 12 -X POST {api_url}/spiders/run \
      -H "Content-Type: application/json" \
      -d '{{"spider_name": "eastmoney_realtime_stock_fund_flow", "params": {{"secid": "{secid}", "data_format": "json"}}}}'"""
    try:
        out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=15)
        d = json.loads(out.stdout)
        if d.get("success"):
            return d["data"]
    except:
        pass
    return None


# ========== 分析逻辑 ==========

def format_position_row(name, price, pct, vol, cost, shares):
    """格式化持仓行"""
    profit = (price - cost) * shares
    profit_pct = (price - cost) / cost * 100 if cost > 0 else 0
    arrow = "🟢" if profit >= 0 else "🔴"
    return f"{arrow} {name} | {price:.2f} | {pct:+.2f}% | {vol/10000:.0f}万手 | {profit:+.0f}元({profit_pct:+.2f}%)"


def analyze_positions(positions, quotes):
    """分析持仓状态"""
    items = []
    total_profit = 0
    for code, info in positions.items():
        q = quotes.get(code)
        if not q:
            continue
        price = q["最新价"]
        pct = q["涨跌幅"]
        vol = q["成交量(手)"]
        cost = info.get("cost", 0)
        shares = info.get("shares", 0)
        profit = (price - cost) * shares
        total_profit += profit
        items.append(format_position_row(info.get("name", code), price, pct, vol, cost, shares))
    return items, total_profit


def need_push_alert(alert_key: str, cooldown: int = 300):
    """检查是否需要推送（去重 + 冷却）"""
    now = time.time()
    last = state.get("last_push_time", {}).get(alert_key, 0)
    if now - last < cooldown:
        return False
    if "last_push_time" not in state:
        state["last_push_time"] = {}
    state["last_push_time"][alert_key] = now
    return True


# ========== 施密特触发器 ==========
# 每条规则有:
#   trigger_line: 触发阈值（如 -4.0%）
#   reset_line:   复位阈值（如 -2.0%，涨回此线以上才可再次触发）
#   状态: 保存在 state["schmitt"][code][rule_key]
#         fired=True 表示已触发，在复位前不再重复推

def _schmitt_fired(code: str, rule_key: str) -> bool:
    """检查该规则是否已处于触发状态"""
    return state.setdefault("schmitt", {}).setdefault(code, {}).get(rule_key, {}).get("fired", False)

def _schmitt_set(code: str, rule_key: str, fired: bool):
    """设置施密特状态"""
    state.setdefault("schmitt", {}).setdefault(code, {})[rule_key] = {
        "fired": fired,
        "time": time.time()
    }

def _schmitt_should_push(code: str, rule_key: str, current_value: float, trigger_line: float, reset_line: float, cooldown: int = 600) -> bool:
    """
    施密特触发器判断：
    - trigger_line: 触发线（如 -4.0）
    - reset_line:   复位线（如 -2.0，靠近0的方向）
    - 跌穿 trigger → 推一次，标记 fired
    - 涨回 reset 以上 → 清除 fired，允许下次再触发
    - 示例：大跌 -4% 触发，必须涨回 -2% 以上才可再次触发
    """
    fired = _schmitt_fired(code, rule_key)

    # 当前值在复位线以内（如 > -2.0%），清除触发状态
    if fired:
        if (trigger_line < 0 and current_value > reset_line) or \
           (trigger_line > 0 and current_value < reset_line):
            _schmitt_set(code, rule_key, False)
            return False

    # 未触发状态：检查是否达到触发线
    if not fired:
        if (trigger_line < 0 and current_value <= trigger_line) or \
           (trigger_line > 0 and current_value >= trigger_line):
            # 冷却检查（一天内只推一次同类型，但复位后可再次触发）
            key = f"{code}_{rule_key}_{_today_bj()}"
            if need_push_alert(key, cooldown):
                _schmitt_set(code, rule_key, True)
                return True

    return False


def check_movements(positions, quotes, thresholds, price_alerts):
    """综合检查异动（施密特触发器）"""
    alerts = []
    now = _now_bj()
    up_pct = thresholds.get("up_pct", 5.0)
    down_pct = thresholds.get("down_pct", -4.0)
    reset_margin = thresholds.get("schmitt_reset_margin", 2.0)
    # 复位线：大涨触发后跌回 (up_pct - reset_margin) 才复位
    #         大跌触发后涨回 (down_pct + reset_margin) 才复位
    up_reset = up_pct - reset_margin   # 如 5.0 - 2.0 = 3.0%
    down_reset = down_pct + reset_margin  # 如 -4.0 + 2.0 = -2.0%

    for code, info in positions.items():
        q = quotes.get(code)
        if not q:
            continue
        name = info.get("name", code)
        price = q["最新价"]
        pct = q["涨跌幅"]
        vol = q["成交量(手)"]
        last_vol = state.get("last_volumes", {}).get(code, 0)

        # 1. 大涨异动（施密特）
        if pct >= up_pct:
            if _schmitt_should_push(code, "surge", pct, up_pct, up_reset, 600):
                alerts.append(("🔴", f"🔥 {name} 大涨 {pct:+.2f}%！现价 {price:.2f}，成交 {vol/10000:.0f}万手"))
        elif pct <= down_pct:
            if _schmitt_should_push(code, "plunge", pct, down_pct, down_reset, 600):
                alerts.append(("🔴", f"🚨 {name} 大跌 {pct:+.2f}%！现价 {price:.2f}，成交 {vol/10000:.0f}万手"))

        # 2. 放量异动（半小时同维度只推一次）
        if last_vol > 0 and vol > last_vol * 3:
            key = f"{code}_volume_{now.strftime('%H')}"
            if need_push_alert(key, 1800):
                direction = "放量拉升" if pct > 0 else "放量下跌"
                alerts.append(("⚠️", f"📊 {name} {direction}！成交量 {vol/10000:.0f}万手（{vol/last_vol:.1f}倍）"))

        # 3. 关键价位突破（去重，每个价位只推一次不重复）
        if code in price_alerts:
            for target in price_alerts[code].get("above", []):
                tkey = f"{code}_above_{target}"
                if tkey not in state.get("triggered_alerts", {}):
                    if price >= target:
                        state.setdefault("triggered_alerts", {})[tkey] = now.isoformat()
                        alerts.append(("⚠️", f"📈 {name} 突破 {target} 元！现价 {price:.2f}（{pct:+.2f}%）"))
            for target in price_alerts[code].get("below", []):
                tkey = f"{code}_below_{target}"
                if tkey not in state.get("triggered_alerts", {}):
                    if price <= target:
                        state.setdefault("triggered_alerts", {})[tkey] = now.isoformat()
                        alerts.append(("🔴", f"📉 {name} 跌破 {target} 元！现价 {price:.2f}（{pct:+.2f}%）"))

        # 记录成交量用于对比
        state.setdefault("last_volumes", {})[code] = vol

        # 4. 振幅异动检测 (振幅>4% 且 量>avg_vol*2)
        high = q.get("最高", 0)
        low = q.get("最低", 0)
        prev_close = q.get("昨收", 0)
        if high > 0 and low > 0 and prev_close > 0:
            amplitude = (high - low) / prev_close * 100
            avg_vol = state.get("avg_volumes", {}).get(code, 0)
            vol_ratio = vol / avg_vol if avg_vol > 0 else 1.0
            if amplitude >= 4.0 and vol_ratio >= 2.0:
                amp_key = f"{code}_amplitude_{now.strftime('%Y%m%d_%H')}"
                if need_push_alert(amp_key, 1800):
                    direction = "拉升" if pct > 0 else "下探" if pct < 0 else "震荡"
                    alerts.append(("⚡", f"📐 {name} 振幅异动！振幅{amplitude:.1f}% {direction} 价{price:.2f} 量{vol/10000:.0f}万手({vol_ratio:.1f}x)"))

        # 5. 持仓V型反转检测 (跌>3%→翻红)
        v_key = f"{code}_pos_vreversal_{now.strftime('%Y%m%d')}"
        if v_key not in state.get("triggered_alerts", {}):
            open_price = q.get("今开", 0)
            day_high = q.get("最高", 0)
            day_low = q.get("最低", 0)
            if open_price > 0 and day_low > 0 and day_high > 0:
                drop_from_open = (day_low - open_price) / open_price * 100
                recover_from_low = (price - day_low) / day_low * 100 if day_low > 0 else 0
                if drop_from_open <= -3.0 and pct > 0 and recover_from_low >= 3.0:
                    state.setdefault("triggered_alerts", {})[v_key] = now.isoformat()
                    alerts.append(("🔄", f"持仓 {name} V型反转！跌至{day_low:.2f}({drop_from_open:+.1f}%)→翻红{price:.2f}({pct:+.2f}%) 反弹{recover_from_low:.1f}%"))

    return alerts


def run_risk_check_on_plunges(positions, quotes, alerts):
    """对触发了大跌告警的持仓标的自动运行风控评估
    
    当持仓大跌触发推送时，自动评估是否触及止损位，
    输出结构化风控建议供后续参考。
    """
    risk_results = []
    for typ, msg in alerts:
        if "大跌" in msg or "砸盘" in msg:
            # 从消息中提取代码
            for code, info in positions.items():
                if info.get("name", "") in msg:
                    q = quotes.get(code, {})
                    price = q.get("最新价", 0)
                    try:
                        result = subprocess.run(
                            [PYTHON_BIN, RISK_CHECK_SCRIPT, "portfolio", "--json"],
                            capture_output=True, text=True, timeout=10
                        )
                        if result.returncode == 0:
                            risk_results.append({
                                "code": code,
                                "name": info.get("name", code),
                                "price": price,
                                "pct": q.get("涨跌幅", 0),
                                "alert": msg,
                                "risk_portfolio": json.loads(result.stdout.strip())
                            })
                    except Exception:
                        pass
                    break
    return risk_results


# ========== 板块背离检测 ==========

# 股票→板块ETF映射
SECTOR_ETF_MAP = {
    "000063": "515050", "600487": "515050", "600522": "515050", "600105": "515050",
    "512480": "512480", "300042": "159995",
    "002594": "515790", "515790": "515790", "002015": "516160", "601016": "516160",
    "002300": "560390", "300444": "560390", "300617": "560390",
    "000938": "588730", "000977": "588730",
    "600711": "159880", "002466": "159880",
    "002297": "512660", "600150": "512660",
    "002475": "159732",
    "518880": "518880",
}

def check_sector_divergence(positions, watch_list, quotes):
    """板块背离检测：个股与板块ETF方向相反且差距>3%"""
    alerts = []
    now = _now_bj()
    all_codes = {}
    for code, info in positions.items():
        all_codes[code] = info.get("name", code)
    for code, name in watch_list.items():
        if code not in all_codes:
            all_codes[code] = name
    
    sectors_needed = set()
    for code in all_codes:
        if code in SECTOR_ETF_MAP:
            sectors_needed.add(SECTOR_ETF_MAP[code])
    if not sectors_needed:
        return alerts
    
    for code, name in all_codes.items():
        sector_etf = SECTOR_ETF_MAP.get(code)
        if not sector_etf:
            continue
        q = quotes.get(code)
        sq = quotes.get(sector_etf)
        if not q or not sq:
            continue
        stock_pct = q["涨跌幅"]
        sector_pct = sq["涨跌幅"]
        divergence = abs(stock_pct - sector_pct)
        if divergence >= 3.0 and stock_pct * sector_pct < 0:
            div_key = f"{code}_sector_div_{now.strftime('%Y%m%d_%H')}"
            if need_push_alert(div_key, 3600):
                direction = "逆势走强" if stock_pct > 0 else "逆势走弱"
                alerts.append(("⚠️", f"板块背离 {name}：个股{stock_pct:+.2f}% vs 板块{sector_pct:+.2f}% ({direction}) 差距{divergence:.1f}%"))
    return alerts


def check_watchlist(watch_list, quotes, thresholds):
    """检查自选股异动（施密特触发器 + 振幅 + 放量 + V反）"""
    alerts = []
    up_pct = thresholds.get("up_pct", 5.0)
    down_pct = thresholds.get("down_pct", -4.0)
    reset_margin = thresholds.get("schmitt_reset_margin", 2.0)
    up_reset = up_pct - reset_margin
    down_reset = down_pct + reset_margin
    now = _now_bj()

    for code, name in watch_list.items():
        q = quotes.get(code)
        if not q:
            continue
        pct = q["涨跌幅"]
        price = q["最新价"]
        vol = q["成交量(手)"]

        # 1. 涨跌幅异动
        if pct >= up_pct:
            if _schmitt_should_push(code, "watch_surge", pct, up_pct, up_reset, 600):
                alerts.append(("👀", f"自选 {name} 大涨 {pct:+.2f}%！现价 {price:.2f}，成交 {vol/10000:.0f}万手"))
        elif pct <= down_pct:
            if _schmitt_should_push(code, "watch_plunge", pct, down_pct, down_reset, 600):
                alerts.append(("🔴", f"自选 {name} 大跌 {pct:+.2f}%！现价 {price:.2f}，成交 {vol/10000:.0f}万手"))

        # 2. 振幅异动 (>4% + 放量2x)
        high = q.get("最高", 0)
        low = q.get("最低", 0)
        prev_close = q.get("昨收", 0)
        if high > 0 and low > 0 and prev_close > 0:
            amplitude = (high - low) / prev_close * 100
            avg_vol = state.get("avg_volumes", {}).get(code, 0)
            vol_ratio = vol / avg_vol if avg_vol > 0 else 1.0
            if amplitude >= 4.0 and vol_ratio >= 2.0:
                amp_key = f"{code}_wamplitude_{now.strftime('%Y%m%d_%H')}"
                if need_push_alert(amp_key, 1800):
                    direction = "拉升" if pct > 0 else "下探" if pct < 0 else "震荡"
                    alerts.append(("⚡", f"自选 {name} 振幅异动！振幅{amplitude:.1f}% {direction} 价{price:.2f} 量{vol/10000:.0f}万手({vol_ratio:.1f}x)"))

        # 3. V型反转检测 (跌>2%后翻红)
        v_key = f"{code}_vreversal_{now.strftime('%Y%m%d')}"
        if v_key not in state.get("triggered_alerts", {}):
            open_price = q.get("今开", 0)
            day_high = q.get("最高", 0)
            day_low = q.get("最低", 0)
            if open_price > 0 and day_low > 0 and day_high > 0:
                drop_from_open = (day_low - open_price) / open_price * 100
                recover_from_low = (price - day_low) / day_low * 100 if day_low > 0 else 0
                if drop_from_open <= -3.0 and pct > 0 and recover_from_low >= 3.0:
                    state.setdefault("triggered_alerts", {})[v_key] = now.isoformat()
                    alerts.append(("🔄", f"自选 {name} V型反转！跌至{day_low:.2f}({drop_from_open:+.1f}%)→翻红{price:.2f}({pct:+.2f}%) 反弹{recover_from_low:.1f}%"))

        # 4. 放量异动 (量>avg_vol*4 且不在±1%横盘)
        avg_vol2 = state.get("avg_volumes", {}).get(code, 0)
        if avg_vol2 > 0 and vol > avg_vol2 * 4 and abs(pct) > 1.0:
            vol_key = f"{code}_wvolume_{now.strftime('%Y%m%d_%H')}"
            if need_push_alert(vol_key, 1800):
                alerts.append(("📊", f"自选 {name} 异常放量！量{vol/10000:.0f}万手({vol/avg_vol2:.1f}x) 价{price:.2f}({pct:+.2f}%)"))

    return alerts


# ========== Agent信号注册 ==========

def _reset_triggered_for_new_day():
    """P0-2 修复: 交易日切换时清除 triggered_alerts 中非持久化条目
    持久化条目: 价格突破 (above/below 固定目标，只触发一次合理)
    交易日复位条目: agent_*, dip_bounce_* (每日可重新触发)
    """
    today = _today_bj()
    current_day = state.get("trading_day", "")
    if current_day == today:
        return  # 同日，不重置
    # 交易日切换 → 清除可重复触发的信号
    old_alerts = state.get("triggered_alerts", {})
    new_alerts = {}
    for key, val in old_alerts.items():
        # 保留价格突破类（只触发一次合理）
        if "_above_" in key or "_below_" in key:
            new_alerts[key] = val
        # dip_bounce 已自带日期，但切换日时清理旧日期键
        elif key.startswith("dip_bounce_"):
            if today in key:
                new_alerts[key] = val
            # 否则丢弃（旧交易日的键）
        # agent_ 信号每日复位
        elif key.startswith("agent_"):
            pass  # 丢弃，允许重新触发
        else:
            new_alerts[key] = val
    state["triggered_alerts"] = new_alerts
    state["trading_day"] = today

def check_agent_signals(quotes):
    """检查Agent注册的智能信号，触发时标记[AGENT_ALERT]"""
    c = load_config()
    signals = c.get("signals", [])
    alerts = []
    now = _now_bj()

    for sig in signals:
        sig_id = sig.get("id", "")
        code = sig.get("code", "")
        q = quotes.get(code)
        if not q:
            continue
        price = q["最新价"]
        pct = q["涨跌幅"]
        vol = q["成交量(手)"]
        name = sig.get("name", code)
        sig_type = sig.get("type", "")
        params = sig.get("params", {})
        target = params.get("target", sig.get("target", 0))

        # 检查是否已经触发过（防止重复）
        trigger_key = f"agent_{sig_id}"
        if trigger_key in state.get("triggered_alerts", {}):
            continue

        triggered = False
        reason = ""

        if sig_type == "price_above" and price >= target:
            triggered = True
            reason = f"突破{target}元"
        elif sig_type == "price_below" and price <= target:
            triggered = True
            reason = f"跌破{target}元"
        elif sig_type == "rapid_surge":
            surge_pct = params.get("surge_pct", 3.0)
            if pct >= surge_pct:
                triggered = True
                reason = f"急涨{pct:+.2f}%"
        elif sig_type == "rapid_drop":
            drop_pct = params.get("drop_pct", -3.0)
            if pct <= drop_pct:
                triggered = True
                reason = f"急跌{pct:+.2f}%"
        elif sig_type == "volume_surge":
            vol_ratio = params.get("vol_ratio", 3.0)
            avg_vol = state.get("avg_volumes", {}).get(code, 0)
            if avg_vol > 0 and vol > avg_vol * vol_ratio:
                triggered = True
                reason = f"放量{vol/vol_ratio:.1f}倍"
        elif sig_type == "surge_peak":
            """放量冲顶信号：大涨+放量+从高点回落"""
            surge_pct = params.get("surge_pct", 3.0)
            vol_ratio = params.get("vol_ratio", 2.0)
            avg_vol = state.get("avg_volumes", {}).get(code, 0)
            day_high = q.get("最高", price)
            if pct >= surge_pct and avg_vol > 0 and vol > avg_vol * vol_ratio:
                # 从最高点回撤超过0.5%才触发（已经在回落了）
                retrace = (day_high - price) / day_high * 100
                if retrace > 0.3:
                    triggered = True
                    reason = f"放量冲顶！涨{pct:+.2f}%+量{vol/vol_ratio:.1f}倍+从高点回{retrace:.2f}%"

        if triggered:
            state.setdefault("triggered_alerts", {})[trigger_key] = now.isoformat()
            msg = f"[AGENT_ALERT] {sig_id}|{code}|{name}|{reason}|现价{price:.2f}|涨{pct:+.2f}%|量{vol/10000:.0f}万手"
            alerts.append(("🤖", msg))
            print(f"[{now.strftime('%H:%M:%S')}] 🤖 Agent信号触发: {name} - {reason}", flush=True)

    return alerts


def check_rolling_decline(quotes):
    """检查多日连续阴跌或累计跌幅过大。"""
    c = load_config()
    positions = c.get("positions", {})
    watch_list = c.get("watch_list", {})
    all_codes = {}
    all_codes.update(positions)
    for code, name in watch_list.items():
        if code not in all_codes:
            all_codes[code] = {"name": name}

    alerts = []
    now = _now_bj()

    for code, info in all_codes.items():
        hist = state.get("price_history", {}).get(code, {})
        sorted_dates = sorted(hist.keys())
        if len(sorted_dates) < 5:
            continue

        recent_dates = sorted_dates[-7:]
        recent_prices = [hist.get(d) for d in recent_dates if hist.get(d)]
        if len(recent_prices) < 5:
            continue

        start_price = recent_prices[0]
        latest_price = recent_prices[-1]
        if start_price <= 0 or latest_price <= 0:
            continue

        cumulative_pct = (latest_price - start_price) / start_price * 100
        consecutive_down = 0
        for prev, curr in zip(recent_prices, recent_prices[1:]):
            if curr < prev:
                consecutive_down += 1
            else:
                consecutive_down = 0

        if cumulative_pct > -5.0 and consecutive_down < 3:
            continue

        trigger_key = f"rolling_decline_{code}_{now.strftime('%Y%m%d')}"
        if trigger_key in state.get("triggered_alerts", {}):
            continue

        q = quotes.get(code)
        if not q:
            continue
        price = q["最新价"]
        pct = q["涨跌幅"]
        name = info.get("name", code)
        state.setdefault("triggered_alerts", {})[trigger_key] = now.isoformat()
        reason = f"{len(recent_prices)}日累计{cumulative_pct:+.2f}% / 连跌{consecutive_down + 1}天"
        msg = f"[AGENT_ALERT] rolling_decline|{code}|{name}|{reason}|现价{price:.2f}|涨{pct:+.2f}%|量0万手"
        alerts.append(("📉", msg))
        print(f"[{now.strftime('%H:%M:%S')}] 📉 连续阴跌检测: {name} - {reason}", flush=True)

    return alerts


def check_rapid_drop_bounce(quotes):
    """通用急跌反弹检测：检查所有持仓+自选是否满足rapid_drop_bounce模式

    参数阈值固定（从guard_config.json读取），适用于所有标的：
    - 前日涨幅>2%（昨日有大涨才有获利回吐压力）
    - 今日从昨收盘跌超-2.5%
    - 量比<2倍（缩量，非恐慌抛售）
    """
    c = load_config()
    positions = c.get("positions", {})
    watch_list = c.get("watch_list", {})
    all_codes = {}
    all_codes.update(positions)
    for code, name in watch_list.items():
        if code not in all_codes:
            all_codes[code] = {"name": name}

    alerts = []
    now = _now_bj()
    
    for code, info in all_codes.items():
        q = quotes.get(code)
        if not q:
            continue
        price = q["最新价"]
        pct = q["涨跌幅"]
        vol = q["成交量(手)"]
        name = info.get("name", code)
        yest_close = q.get("昨收", 0)
        
        # 去重：每天每只标的只触发一次
        trigger_key = f"dip_bounce_{code}_{now.strftime('%Y%m%d')}"
        if trigger_key in state.get("triggered_alerts", {}):
            continue
        
        # 条件1：今日从昨收盘跌超-2.5%
        if pct > -2.5:
            continue
        
        # 条件2：前日涨幅>2%（通过price_history计算昨日收益）
        hist = state.get("price_history", {}).get(code, {})
        sorted_dates = sorted(hist.keys())
        if len(sorted_dates) < 2:
            continue
        dby_close = hist.get(sorted_dates[-2], 0)  # 前日收盘
        if yest_close <= 0 or dby_close <= 0:
            continue
        yest_gain = (yest_close - dby_close) / dby_close * 100
        if yest_gain < 2.0:
            continue
        
        # 条件3：缩量（量比<2倍）
        avg_vol = state.get("avg_volumes", {}).get(code, 0)
        vol_ratio = vol / avg_vol if avg_vol > 0 else 999
        if avg_vol > 0 and vol_ratio >= 2.0:
            continue
        
        # 全部条件满足 → 触发！
        state.setdefault("triggered_alerts", {})[trigger_key] = now.isoformat()
        msg = (f"[AGENT_ALERT] rapid_drop_bounce|{code}|{name}|"
               f"急跌反弹模式！昨涨{yest_gain:.1f}%→今跌{pct:.1f}%→量比{vol_ratio:.1f}x(缩量无恐慌)|"
               f"现价{price:.2f}|昨收{yest_close:.2f}")
        alerts.append(("🚨", msg))
        print(f"[{now.strftime('%H:%M:%S')}] 🚨 急跌反弹检测: {name} - 昨涨{yest_gain:.1f}%→今跌{pct:.1f}% 缩量{vol_ratio:.1f}x", flush=True)
    
    return alerts


# ========== 主循环 ==========

def main_loop():
    """主循环：每30秒轮询 + 自动推送"""
    load_state()
    c = load_config()

    print(f"🤖 盯盘守护 v3.0 启动 @ {_now_bj().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"   持仓: {len(c.get('positions',{}))} 只 | 自选: {len(c.get('watch_list',{}))} 只 | 信号: {len(c.get('signals',[]))} 个", flush=True)
    print(f"   轮询: 30秒 | 涨跌≥{c.get('alert_thresholds',{}).get('up_pct',5)}%/≤{c.get('alert_thresholds',{}).get('down_pct',-4)}% | 振幅>{c.get('alert_thresholds',{}).get('amplitude_pct',4)}% | 板块背离>{c.get('alert_thresholds',{}).get('sector_divergence_pct',3)}%", flush=True)
    print("-" * 50, flush=True)

    # 启动通知（只发一次，避免每次重启都推）
    startup_done = False
    cycle_count = 0
    last_summary_time = 0

    while True:
        try:
            now = datetime.now(CST)
            cycle_count += 1
            print(f"[{now.strftime('%H:%M:%S')}] 🔄 第{cycle_count}轮", flush=True)
            hour, minute, wd = now.hour, now.minute, now.weekday()

            # 非交易日：跳过
            if wd >= 5:
                time.sleep(300)
                continue

            # 非交易时段：按本机时区判断 A 股窗口，避免将北京时间误判为盘后
            local_now = datetime.now()
            local_hour, local_minute = local_now.hour, local_now.minute
            market_now = _now_bj()
            market_hour, market_minute = market_now.hour, market_now.minute
            is_morning = (market_hour == 9 and market_minute >= 25) or (market_hour == 10) or (market_hour == 11 and market_minute <= 30)
            is_afternoon = (market_hour == 13) or (market_hour == 14) or (market_hour == 15 and market_minute == 0)
            if not (is_morning or is_afternoon):
                if market_hour < 9 or (market_hour == 9 and market_minute < 25):
                    sleep_seconds = 600
                elif market_hour >= 15:
                    sleep_seconds = 1800
                else:
                    sleep_seconds = 300
                print(
                    f"[{now.strftime('%H:%M:%S')}] ⏸ 非交易时段，本机 {local_hour:02d}:{local_minute:02d} / 北京 {market_hour:02d}:{market_minute:02d}，休眠 {sleep_seconds}s",
                    flush=True,
                )
                _write_heartbeat(
                    "idle",
                    cycle_count,
                    0,
                    f"local={local_hour:02d}:{local_minute:02d}|market={market_hour:02d}:{market_minute:02d}|sleep={sleep_seconds}",
                )
                save_state()
                time.sleep(sleep_seconds)
                continue

            # 热加载配置
            c = load_config()
            positions = c.get("positions", {})
            watch_list = c.get("watch_list", {})
            price_alerts = c.get("price_alerts", {})
            thresholds = c.get("alert_thresholds", {})

            # ====== 获取数据 ======
            all_codes = list(positions.keys()) + [k for k in watch_list if k not in positions]
            # 板块背离检测需要板块ETF行情
            sector_etfs = set()
            for code in all_codes:
                if code in SECTOR_ETF_MAP:
                    sector_etfs.add(SECTOR_ETF_MAP[code])
            all_codes = all_codes + [s for s in sector_etfs if s not in all_codes]
            t0 = time.time()
            try:
                quotes = fetch_quotes_batch(all_codes)
            except Exception as fe:
                print(f"[{now.strftime('%H:%M:%S')}] ⚠️ 行情获取异常: {fe}，跳过本轮", flush=True)
                blindness = _evaluate_runtime_blindness(c, {}, cycle_count, 30.0)
                blind_alerts = _emit_runtime_blindness_alert(blindness)
                if blind_alerts:
                    push_wechat("\n".join(msg for _, msg in blind_alerts), "🧯")
                    save_state()
                time.sleep(30)
                continue
            fetch_time = time.time() - t0

            blindness = _evaluate_runtime_blindness(c, quotes, cycle_count, fetch_time)
            blind_alerts = _emit_runtime_blindness_alert(blindness)
            if blindness.get("status") == "degraded":
                print(
                    f"[{now.strftime('%H:%M:%S')}] ⚠️ 运行态降级: {'；'.join(blindness.get('reasons', []))}",
                    flush=True,
                )

            # ====== 写入行情快照（供其他 cron 任务读取） ======
            snap = MarketSnapshot()
            snapshot_data = {}
            for code, q in quotes.items():
                snapshot_data[code] = {
                    "p": q.get("最新价", 0),
                    "pct": q.get("涨跌幅", 0),
                    "h": q.get("最高", 0),
                    "l": q.get("最低", 0),
                    "o": q.get("今开", 0),
                    "pre": q.get("昨收", 0),
                    "v": q.get("成交量(手)", 0),
                    "a": q.get("成交额(万)", 0),
                    "to": q.get("换手(%)", 0),
                    "name": q.get("名称", ""),
                    "etf": q.get("_is_etf", False),
                    "t": q.get("时间", ""),
                }
            snap.update_batch(snapshot_data)  # 一笔写入，避免循环update的竞态

            # ====== 收盘价累加（用于CVaR计算） ======
            today_str = now.strftime("%Y-%m-%d")
            for code, q in quotes.items():
                price = q.get("最新价", 0)
                if price > 0:
                    # 更新日级收盘价序列（每日只记录一次）
                    if code not in state.get("price_history", {}):
                        state.setdefault("price_history", {})[code] = {}
                    daily_prices = state["price_history"][code]
                    # 本日还未记录 -> 记录
                    if today_str not in daily_prices:
                        daily_prices[today_str] = price

            # ====== CVaR监控（每120轮≈1小时） ======
            if cycle_count % 120 == 0 and len(quotes) > 0:
                cvar_alerts = []
                for code in positions:
                    hist = state.get("price_history", {}).get(code, {})
                    sorted_dates = sorted(hist.keys())
                    prices_series = [hist[d] for d in sorted_dates]
                    
                    if len(prices_series) < 20:
                        continue
                    
                    cvar = calc_cvar(prices_series, 0.95)
                    baseline = state.get("cvar_baseline", {}).get(code)
                    
                    if cvar is not None:
                        if baseline is None:
                            state.setdefault("cvar_baseline", {})[code] = cvar
                        else:
                            # CVaR恶化（变得更负）= 风险加大
                            if cvar < baseline * 0.8:  # 恶化超过20%
                                name = positions[code].get("name", code)
                                alert = f"⚡ CVaR恶化 {name}: {baseline*100:.2f}%→{cvar*100:.2f}%"
                                cvar_alerts.append(("🔴", alert))
                                state["cvar_baseline"][code] = cvar  # 更新基线

                if cvar_alerts:
                    for typ, msg in cvar_alerts:
                        print(f"[{now.strftime('%H:%M:%S')}] {typ} {msg}", flush=True)
                    # 写入紧急信号文件触发风控
                    # P1-4 修复: CVaR 告警使用与 Agent 信号一致的 [AGENT_ALERT] 前缀
                    signal_lines = "\n".join(
                        f"[AGENT_ALERT] cvar_deterioration|{msg}"
                        for typ, msg in cvar_alerts
                    )
                    with open(SIGNAL_FILE, "a", encoding="utf-8") as f:
                        f.write(f"\n[{now.isoformat()}] CVaR ALERT\n{signal_lines}\n")

            # ====== 启动推送（仅打印日志，不触发紧急通道） ======
            if not startup_done and quotes:
                items, total_profit = analyze_positions(positions, quotes)
                lines = "\n".join(items)
                print(f"\n{'='*50}\n✅ 开市持仓概览\n{lines}\n盈亏合计: {total_profit:+.0f}元\n{'='*50}", flush=True)
                startup_done = True

            # ====== 检查异动 ======
            # P0-2 修复: 交易日切换时复位 agent 信号去重
            _reset_triggered_for_new_day()
            alerts = check_movements(positions, quotes, thresholds, price_alerts)
            watch_alerts = check_watchlist(watch_list, quotes, thresholds)
            sector_alerts = check_sector_divergence(positions, watch_list, quotes)
            
            # 更新均量（用于放量信号判断）
            for code, q in quotes.items():
                vol = q.get("成交量(手)", 0)
                if vol > 0:
                    state.setdefault("vol_history", {}).setdefault(code, []).append(vol)
                    state["vol_history"][code] = state["vol_history"][code][-20:]  # 保留最近20轮
                    avg = sum(state["vol_history"][code]) / len(state["vol_history"][code])
                    state.setdefault("avg_volumes", {})[code] = avg
            
            agent_alerts = check_agent_signals(quotes)
            rolling_decline_alerts = check_rolling_decline(quotes)
            dip_alerts = check_rapid_drop_bounce(quotes)
            all_alerts = alerts + watch_alerts + sector_alerts + agent_alerts + rolling_decline_alerts + dip_alerts + blind_alerts

            # ====== 风控：大跌触发时自动评估 ======
            risk_results = run_risk_check_on_plunges(positions, quotes, all_alerts)
            if risk_results:
                for rr in risk_results:
                    warnings = rr.get("risk_portfolio", {}).get("warnings", [])
                    if warnings:
                        push_wechat(
                            f"🛡️ 风控自动评估 - {rr['name']}({rr['code']})\n"
                            f"大跌 {rr['pct']:+.2f}% @ {rr['price']:.2f}\n"
                            f"风险提示:\n" + "\n".join(f"  • {w}" for w in warnings),
                            "🛡️"
                        )
                        print(f"[{now.strftime('%H:%M:%S')}] 🛡️ 风控触发: {rr['name']} 大跌 {rr['pct']:+.2f}%", flush=True)

            # ====== 推送异动 ======
            if all_alerts:
                lines = []
                db = TradeDB()
                for typ, msg in all_alerts:
                    print(f"[{now.strftime('%H:%M:%S')}] {typ} {msg}", flush=True)
                    lines.append(f"{typ} {msg}")

                    # 写入数据库日志
                    code = ""
                    name = ""
                    for c, info in positions.items():
                        if info.get("name", "") in msg:
                            code = c
                            name = info.get("name", "")
                            break
                    for c, n in watch_list.items():
                        if n in msg:
                            code = c
                            name = n
                            break
                    db.log("异动", code, name, msg, {"typ": typ})

                # 有急跌/跌破才高强度推送，否则温和通知
                urgent = any("跌破" in m or "大跌" in m or "砸盘" in m for _, m in all_alerts)
                push_wechat("\n".join(lines), "🔴 URGENT" if urgent else "⚠️")

                # 写入常规告警文件
                with open(ALERT_FILE, "w") as f:
                    f.write("\n".join(lines))

            # ====== 定期状态报告（每30分钟，用独立通道不污染告警文件） ======
            if cycle_count % 60 == 0:  # 60轮 × 30秒 = 30分钟
                items, total_profit = analyze_positions(positions, quotes)
                report = "📋 **盯盘运行报告**\n\n" + "\n".join(items)
                report += f"\n\n⏱ 已运行 {cycle_count} 轮 | 持仓盈亏 {'🟢' if total_profit>=0 else '🔴'}{total_profit:+.0f}元"
                # 不用 push_wechat() — 它写告警文件导致cron重复推送
                timestamp = _now_bj().strftime('%H:%M:%S')
                msg = f"📊 {timestamp}\n{report}"
                _write_heartbeat("status", cycle_count, 0, f"profit={total_profit:+.0f}")
                print(f"[{timestamp}] 📊 状态报告（不触发推送）\n{report}", flush=True)

            # ====== 保存状态 ======
            save_state()

            # ====== 心跳文件（供外部监控） ======
            _write_heartbeat("active", cycle_count, len(all_alerts))

            time.sleep(30)

        except KeyboardInterrupt:
            push_wechat(f"🛑 盯盘守护已停止 @ {_now_bj().strftime('%H:%M:%S')}", "🔴")
            break
        except Exception as e:
            print(f"[{_now_bj().strftime('%H:%M:%S')}] ❌ 循环错误: {e}", flush=True)
            try:
                push_wechat(f"⚠️ 盯盘守护异常: {e}", "🔴")
            except:
                pass
            time.sleep(30)


if __name__ == "__main__":
    main_loop()

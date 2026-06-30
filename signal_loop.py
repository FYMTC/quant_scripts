#!/usr/bin/env python3
"""
signal_loop.py — Agent信号环路管理器

三个入口：
  auto_generate()    → 盘前08:30调用，扫描持仓+自选 → 生成初始信号集
  handle_trigger()   → 信号触发后调用，快速过滤+TradingAgents+决策
  close_loop()       → 原子操作：删旧信号+注册新信号+更新guard_config

动态配额：
  Tier A (持仓) = len(positions) × 2次/天/只
  Tier B (高关注自选) = count × 1次/天/只
  Tier C (低关注自选) = 0次
  全局日上限 = A + B（动态计算）
"""

import json
import os
import time
import subprocess
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple, Any
from system_config import cfg

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "guard_config.json")
STATE_PATH = os.path.join(BASE, "guard_state.json")
PROFILE_DIR = os.path.join(BASE, "signal_profiles")
AUDIT_LOG_PATH = os.path.join(BASE, "signal_audit.jsonl")
FEATURE_SNAPSHOT_PATH = os.path.join(BASE, "data", "feature_snapshot.json")


def _load_signal_scope() -> Tuple[Dict[str, dict], Dict[str, str]]:
    """统一解析信号生成所需的持仓/监控范围。

    持仓与监控池优先跟随当前操盘主账户：
    - easyths/paper 账户使用 account snapshot + account-bound guard bundle
    - 其他场景回退到 stock_kb + 根 guard_config
    """
    config = _load_json(CONFIG_PATH) or {}

    positions: Dict[str, dict] = {}
    watch_list = config.get("watch_list") or config.get("monitored_codes") or {}

    try:
        from trade_accounts import resolve_trading_account
        from trade_account_context import load_account_snapshot
        from guard_account_bind import load_guard_bundle

        account_id = resolve_trading_account()
        bundle = load_guard_bundle(account_id)
        bundle_cfg = (bundle or {}).get("config") or {}
        snap = load_account_snapshot(account_id)

        for row in snap.get("positions") or []:
            code = str(row.get("code") or "").strip()
            if not code:
                continue
            positions[code] = {
                "name": row.get("name", code),
                "shares": row.get("shares") or 0,
                "cost": row.get("cost"),
                "market_value": row.get("market_value"),
            }

        watch_list = bundle_cfg.get("watch_list") or bundle_cfg.get("monitored_codes") or watch_list
        if positions or watch_list:
            return positions, watch_list
    except Exception:
        pass

    try:
        positions = (_get_stock_kb_cls()().read_portfolio_truth().get("positions") or {})
    except Exception:
        raw_positions = config.get("positions") or {}
        for code, info in raw_positions.items():
            if isinstance(info, dict):
                positions[code] = info
            else:
                positions[code] = {"name": str(info)}

    watch_list = config.get("watch_list") or config.get("monitored_codes") or {}
    return positions, watch_list


def _get_stock_kb_cls():
    from stock_kb import StockKB
    return StockKB


# === 配额管理 ===

def get_daily_quota() -> dict:
    """动态配额：基于当前实际持仓+自选"""
    positions, watch_list = _load_signal_scope()
    if not positions and not watch_list:
        return {"global_limit": 0, "tier_a": 0, "tier_b": 0, "tier_c": 0}

    # 持仓标的 = Tier A
    tier_a_codes = list(positions.keys())
    tier_a_count = len(tier_a_codes)

    # 高关注自选 = 近5日波动>3% 或有近期交易记录的标的
    tier_b_codes = []
    tier_c_codes = []
    for code, name in watch_list.items():
        if code in tier_a_codes:
            continue  # 已在持仓中
        if _is_high_attention(code):
            tier_b_codes.append(code)
        else:
            tier_c_codes.append(code)

    global_limit = tier_a_count * 2 + len(tier_b_codes) * 1

    return {
        "tier_a": tier_a_codes,
        "tier_b": tier_b_codes,
        "tier_c": tier_c_codes,
        "tier_a_per_stock": 2,
        "tier_b_per_stock": 1,
        "tier_c_per_stock": 0,
        "global_limit": global_limit,
        "min_interval_minutes": 30,
        "morning_window": ("09:30", "10:00"),    # 开盘高优先级
        "afternoon_window": ("14:30", "15:00"),  # 尾盘高优先级
        "lunch_blackout": ("11:30", "13:00"),    # 午休不触发
    }


def _is_high_attention(code: str) -> bool:
    """判断自选标的是否高关注：近5日波动>3%或有近期交易"""
    # T1.10 二期（2026-06-30）：清仓回流窗口内视为 Tier B（防"卖飞就忘"）
    try:
        from close_loop_reflow import is_in_reflow
        if is_in_reflow(code):
            return True
    except Exception:
        pass

    try:
        from stock_signal_profile import load_profile
        profile = load_profile(code)
        if profile.get("volatility_5d", 0) > 3.0:
            return True
        if profile.get("recent_trades", 0) > 0:
            return True
    except Exception:
        pass

    # 快速判断：查stock_kb是否有交易记录
    try:
        result = subprocess.run(
            [cfg.python, "-c",
             f"from stock_kb import StockKB; kb=StockKB(); t=kb.get_trades('{code}', 5); print(len(t))"],
            capture_output=True, text=True, timeout=5,
            cwd=BASE
        )
        if result.stdout.strip().isdigit() and int(result.stdout.strip()) > 0:
            return True
    except Exception:
        pass

    return False


def check_quota(stock_code: str) -> Tuple[bool, str]:
    """检查今日配额是否还有剩余。返回(可用, 原因)"""
    quota = get_daily_quota()
    today = date.today().isoformat()

    # 判断标的属于哪个Tier
    tier = None
    per_stock_limit = 0
    if stock_code in quota["tier_a"]:
        tier = "A"
        per_stock_limit = quota["tier_a_per_stock"]
    elif stock_code in quota["tier_b"]:
        tier = "B"
        per_stock_limit = quota["tier_b_per_stock"]
    else:
        return False, f"{stock_code} 不在Tier A/B中（Tier C不触发全量分析）"

    # 检查该标的本日已跑次数
    today_count = _count_today_analyses(stock_code, today)
    if today_count >= per_stock_limit:
        return False, f"{stock_code} 今日全量分析已达上限({today_count}/{per_stock_limit})"

    # 检查全局上限
    global_count = _count_today_analyses(None, today)
    if global_count >= quota["global_limit"]:
        return False, f"全局配额已满({global_count}/{quota['global_limit']})"

    # 检查时间窗口（午休黑名单）
    now = datetime.now()
    now_str = now.strftime("%H:%M")
    if quota["lunch_blackout"][0] <= now_str < quota["lunch_blackout"][1]:
        return False, f"午休时段({quota['lunch_blackout'][0]}-{quota['lunch_blackout'][1]})不触发"

    return True, f"{tier}级配额可用(已用{today_count}/{per_stock_limit}, 全局{global_count}/{quota['global_limit']})"


def _count_today_analyses(stock_code: Optional[str], today: str) -> int:
    """统计今日已执行的全量分析次数"""
    count = 0
    try:
        if os.path.exists(AUDIT_LOG_PATH):
            with open(AUDIT_LOG_PATH) as f:
                for line in f:
                    try:
                        entry = json.loads(line.strip())
                        if entry.get("date") == today and entry.get("action_type") == "ANALYZE":
                            if stock_code is None or entry.get("stock_code") == stock_code:
                                count += 1
                    except json.JSONDecodeError:
                        continue
    except Exception:
        pass
    return count



def _load_feature_snapshot() -> Dict[str, Any]:
    data = _load_json(FEATURE_SNAPSHOT_PATH) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _feature_gate_for_stock(code: str, feature_snapshot: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    if not feature_snapshot:
        return False, {"reason": "feature_snapshot_missing"}
    runtime_flags = feature_snapshot.get("runtime_flags") or {}
    per_stock = (feature_snapshot.get("per_stock") or {}).get(code)
    if not per_stock:
        return False, {"reason": "stock_feature_missing"}
    if per_stock.get("data_quality") != "ok":
        return False, {"reason": f"data_quality={per_stock.get('data_quality')}"}
    return True, {
        "risk_level": per_stock.get("risk_level", "unknown"),
        "cvar": per_stock.get("cvar"),
        "market_regime": ((feature_snapshot.get("portfolio") or {}).get("market_regime") or {}).get("current_state"),
        "feature_fresh": runtime_flags.get("feature_fresh", False),
        "feature_generated_at": feature_snapshot.get("generated_at"),
        "risk_reasons": per_stock.get("risk_reasons") or [],
    }



def auto_generate() -> dict:
    """
    盘前自动生成信号集。
    对每只持仓/自选标的：计算技术位 → 注册price_below/price_above/rapid_drop/rapid_surge/surge_peak
    返回生成摘要
    """
    # T1.10 二期（2026-06-30）：清理过期回流记录（防"卖飞就忘"窗口维护）
    try:
        from close_loop_reflow import prune_expired
        prune_expired()
    except Exception:
        pass

    config = _load_json(CONFIG_PATH)
    if not config:
        return {"error": "无法读取guard_config.json"}

    positions, watch_list = _load_signal_scope()
    feature_snapshot = _load_feature_snapshot()
    all_codes = {}

    # 合并持仓+自选
    for code, info in positions.items():
        all_codes[code] = {"name": info.get("name", code), "tier": "A", "is_position": True}
    for code, name in watch_list.items():
        if code not in all_codes:
            all_codes[code] = {"name": name, "tier": _get_tier(code), "is_position": False}

    # T1.10 三期（2026-07-04）：轮动扫描 TOP3 up 行业龙头提升到 Tier B
    # 仅在 rotation_scan.status==ok 且 backtest_gate.passed 时生效；异常静默降级不影响主流程
    try:
        from rotation_scanner import load_rotation_scan
        rotation = load_rotation_scan()
        if rotation.get("status") == "ok" and (rotation.get("backtest_gate") or {}).get("passed"):
            for industry in rotation.get("top3_up", []):
                for stock in industry.get("top_stocks", [])[:2]:  # 每行业最多 2 只
                    code = stock.get("code")
                    if code and code not in all_codes:
                        all_codes[code] = {
                            "name": stock.get("name", code),
                            "tier": "B", "is_position": False,
                            "rotation_boost": industry.get("industry", ""),
                        }
    except Exception:
        pass

    existing_signals = {s["id"]: s for s in config.get("signals", [])}
    new_signals = []
    updated_signals = []
    deleted_signals = []
    errors = []

    for code, meta in all_codes.items():
        try:
            gate_ok, feature_info = _feature_gate_for_stock(code, feature_snapshot)
            if not gate_ok:
                errors.append(f"{code}: feature_gate {feature_info.get('reason')}")
                continue
            tech = _calc_technical_levels(code)
            if not tech:
                errors.append(f"{code}: 技术数据不可用，跳过")
                continue

            profile = _load_profile(code)
            thresholds = profile.get("effective_thresholds", {})

            # 基于个性化阈值生成信号
            signal_defs = _build_signals_for_stock(code, meta["name"], meta["tier"],
                                                    tech, thresholds, feature_info)

            for sig in signal_defs:
                sig_id = sig["id"]
                if sig_id in existing_signals:
                    # 更新已存在的信号
                    old = existing_signals[sig_id]
                    if old.get("params") != sig.get("params"):
                        updated_signals.append(sig_id)
                        existing_signals[sig_id] = sig
                else:
                    new_signals.append(sig_id)
                    existing_signals[sig_id] = sig

        except Exception as e:
            errors.append(f"{code}: {e}")

    # 清理：删除连续3天未触发的信号
    for sig_id, sig in list(existing_signals.items()):
        if sig_id in new_signals:
            continue
        if sig.get("auto_generated") and _is_stale(sig_id):
            deleted_signals.append(sig_id)
            del existing_signals[sig_id]

    # 写回
    config["signals"] = list(existing_signals.values())
    _save_json(CONFIG_PATH, config)

    return {
        "total_signals": len(config["signals"]),
        "new": len(new_signals),
        "updated": len(updated_signals),
        "deleted": len(deleted_signals),
        "errors": errors,
        "stocks_processed": len(all_codes),
        "feature_snapshot_used": bool(feature_snapshot),
    }


def _get_tier(code: str) -> str:
    """判断标的Tier（A/B/C）"""
    positions, _ = _load_signal_scope()
    if code in positions:
        return "A"
    if _is_high_attention(code):
        return "B"
    return "C"


def _calc_technical_levels(code: str) -> Optional[dict]:
    """
    计算技术位：近期高低点、MA、ATR。
    用Baostock取近20日K线。
    """
    try:
        import baostock as bs
        bs.login()

        prefix = "sz." if code.startswith(("0", "3")) else "sh."
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=30)
        rs = bs.query_history_k_data_plus(
            f"{prefix}{code}",
            "date,open,high,low,close,volume",
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
            frequency="d", adjustflag="2"
        )

        rows = []
        while rs.next():
            rows.append(rs.get_row_data())

        bs.logout()

        if len(rows) < 5:
            return None

        closes = [float(r[4]) for r in rows if r[4]]
        highs = [float(r[2]) for r in rows if r[2]]
        lows = [float(r[3]) for r in rows if r[3]]
        volumes = [float(r[5]) for r in rows if r[5]]

        if not closes:
            return None

        current = closes[-1]
        high_20 = max(highs[-20:]) if len(highs) >= 20 else max(highs)
        low_20 = min(lows[-20:]) if len(lows) >= 20 else min(lows)
        avg_vol = sum(volumes[-5:]) / min(5, len(volumes[-5:])) if volumes else 0

        # ATR(14)
        tr_list = []
        for i in range(1, min(15, len(rows))):
            h, l, pc = highs[-i], lows[-i], closes[-i-1]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            tr_list.append(tr)
        atr = sum(tr_list) / len(tr_list) if tr_list else current * 0.02

        # MA
        ma5 = sum(closes[-5:]) / min(5, len(closes[-5:]))
        ma10 = sum(closes[-10:]) / min(10, len(closes[-10:]))
        ma20 = sum(closes[-20:]) / min(20, len(closes[-20:]))

        # 5日波动率
        if len(closes) >= 5:
            returns = [(closes[i] - closes[i-1]) / closes[i-1] * 100
                       for i in range(-min(5, len(closes)-1), 0)]
            vol_5d = sum(abs(r) for r in returns) / len(returns) if returns else 2.0
        else:
            vol_5d = 2.0

        return {
            "current": current,
            "high_20": high_20,
            "low_20": low_20,
            "atr": atr,
            "ma5": ma5,
            "ma10": ma10,
            "ma20": ma20,
            "avg_vol_5d": avg_vol,
            "volatility_5d": vol_5d,
        }

    except Exception as e:
        return None


def _build_signals_for_stock(code: str, name: str, tier: str,
                              tech: dict, thresholds: dict, feature_info: Optional[dict] = None) -> List[dict]:
    """为单只标生成信号定义列表"""
    current = tech["current"]
    atr = tech["atr"]
    high = tech["high_20"]
    low = tech["low_20"]
    vol_5d = tech["volatility_5d"]

    signals = []

    feature_info = feature_info or {}
    risk_level = feature_info.get("risk_level")
    market_regime = feature_info.get("market_regime")
    cvar = feature_info.get("cvar")

    # 个性化阈值（从profile演进），否则用默认值
    rapid_drop_pct = thresholds.get("rapid_drop", -max(3.0, vol_5d * 0.8))
    rapid_surge_pct = thresholds.get("rapid_surge", max(3.0, vol_5d * 0.8))
    amplitude_pct = thresholds.get("amplitude_pct", max(4.0, vol_5d * 1.2))
    surge_peak_pct = thresholds.get("surge_peak_surge", max(2.5, vol_5d * 0.7))

    if risk_level == "danger" or market_regime == "bear" or (cvar is not None and float(cvar) <= -5.0):
        rapid_surge_pct = max(rapid_surge_pct, 4.5)
        surge_peak_pct = max(surge_peak_pct, 3.5)

    # T1.6 动态止损（2026-06-26）：持仓标的用修复速度分档替代固定 -3% 急跌阈值
    # 持仓标的的急跌信号应基于"距成本回撤能容忍多少"而非纯波动率
    dynamic_stop_info = None
    if tier == "A":  # 持仓标的
        try:
            from dynamic_stop_loss import compute_stop_loss_pct
            from risk_check import load_price_history
            from stock_kb import StockKB
            kb = StockKB()
            pf = kb.read_portfolio_truth()
            pos_info = pf["positions"].get(code, {})
            avg_cost = pos_info.get("cost", 0)
            if avg_cost > 0:
                price_history = load_price_history()
                hist_prices = price_history.get(code, [])
                # 转换为日收益率列表
                daily_returns = []
                if len(hist_prices) >= 3:
                    for i in range(1, len(hist_prices)):
                        if hist_prices[i - 1] > 0:
                            daily_returns.append((hist_prices[i] - hist_prices[i - 1]) / hist_prices[i - 1])
                stop_pct, stop_details = compute_stop_loss_pct(code, avg_cost, current, daily_returns)
                # 用动态止损阈值替代固定急跌阈值（取更紧的，避免放宽过多）
                rapid_drop_pct = max(rapid_drop_pct, stop_pct)
                dynamic_stop_info = stop_details
        except Exception:
            pass  # 失败时回退到固定阈值

    evidence = {
        "feature_snapshot_at": feature_info.get("feature_generated_at"),
        "risk_level": risk_level,
        "market_regime": market_regime,
        "cvar": cvar,
        "risk_reasons": feature_info.get("risk_reasons") or [],
        "feature_fresh": feature_info.get("feature_fresh"),
    }

    # 1. 急跌抄底位（近期低点-1 ATR）
    buy_target = round(low - atr, 2)
    if buy_target < current * 0.85:  # 不低于现价15%
        buy_target = round(current * 0.92, 2)

    signals.append({
        "id": f"{code}_price_below_{int(buy_target*100)}",
        "code": code,
        "name": name,
        "type": "price_below",
        "tier": tier,
        "params": {"target": buy_target},
        "rationale": f"自动生成：近期低点{low} - 1ATR({atr:.2f}) = {buy_target}。急跌至此位触发评估。",
        "registered": datetime.now().isoformat(),
        "registered_by": "auto_generate",
        "auto_generated": True,
        "ttl_days": 5,
        "evidence": evidence,
    })

    # 2. 突破追涨位（近期高点+0.5 ATR）
    sell_target = round(high + atr * 0.5, 2)
    signals.append({
        "id": f"{code}_price_above_{int(sell_target*100)}",
        "code": code,
        "name": name,
        "type": "price_above",
        "tier": tier,
        "params": {"target": sell_target},
        "rationale": f"自动生成：近期高点{high} + 0.5ATR。突破此位触发追涨/止盈评估。",
        "registered": datetime.now().isoformat(),
        "registered_by": "auto_generate",
        "auto_generated": True,
        "ttl_days": 5,
        "evidence": evidence,
    })

    # 3. 急跌警报
    stop_rationale = ""
    if dynamic_stop_info:
        speed_label = {"fast": "快速修复", "mid": "中速修复", "slow": "慢速修复", "default": "默认"}.get(
            dynamic_stop_info.get("speed", ""), "默认"
        )
        stop_rationale = f" [T1.6动态止损:{speed_label}档 stop={dynamic_stop_info.get('stop_loss_pct')}% stop价={dynamic_stop_info.get('stop_loss_price')}]"
    signals.append({
        "id": f"{code}_rapid_drop",
        "code": code,
        "name": name,
        "type": "rapid_drop",
        "tier": tier,
        "params": {"drop_pct": rapid_drop_pct},
        "rationale": f"自动生成：5日波动率{vol_5d:.1f}% → 急跌阈值{rapid_drop_pct:.1f}%。触发评估是否抄底/止损。{stop_rationale}",
        "registered": datetime.now().isoformat(),
        "registered_by": "auto_generate",
        "auto_generated": True,
        "ttl_days": 1,
        "dynamic_stop_loss": dynamic_stop_info,
    })

    # 4. 急涨警报
    signals.append({
        "id": f"{code}_rapid_surge",
        "code": code,
        "name": name,
        "type": "rapid_surge",
        "tier": tier,
        "params": {"surge_pct": rapid_surge_pct},
        "rationale": f"自动生成：5日波动率{vol_5d:.1f}% → 急涨阈值{rapid_surge_pct:.1f}%。触发评估是否追涨/止盈。",
        "registered": datetime.now().isoformat(),
        "registered_by": "auto_generate",
        "auto_generated": True,
        "ttl_days": 1,
    })

    # 5. 放量冲顶（仅Tier A+B）
    if tier in ("A", "B"):
        signals.append({
            "id": f"{code}_surge_peak",
            "code": code,
            "name": name,
            "type": "surge_peak",
            "tier": tier,
            "params": {"surge_pct": surge_peak_pct, "vol_ratio": 2.0},
            "rationale": f"自动生成：涨幅>{surge_peak_pct:.1f}%+量翻2倍+从高点回撤。检测冲顶信号。",
            "registered": datetime.now().isoformat(),
            "registered_by": "auto_generate",
            "auto_generated": True,
            "ttl_days": 1,
        })

    return signals


def _is_stale(sig_id: str) -> bool:
    """信号是否已过期（连续3天未触发）"""
    try:
        if os.path.exists(AUDIT_LOG_PATH):
            today = date.today().isoformat()
            trigger_dates = set()
            with open(AUDIT_LOG_PATH) as f:
                for line in f:
                    entry = json.loads(line.strip())
                    if (entry.get("signal_id") == sig_id and
                        entry.get("action_type") == "TRIGGER"):
                        trigger_dates.add(entry.get("date", ""))
            recent_dates = sorted(trigger_dates, reverse=True)[:3]
            if not recent_dates:
                return True  # 从未触发
            latest = datetime.strptime(recent_dates[0], "%Y-%m-%d")
            return (datetime.now() - latest).days >= 3
    except Exception:
        pass
    return False


# === 信号触发处理 ===

def handle_trigger(signal_id: str, code: str, current_price: float,
                   change_pct: float, volume: float) -> dict:
    """
    信号触发后Agent调用的决策入口。
    返回:
      {"action": "ANALYZE"|"SKIP"|"WAIT"|"DISMISS",
       "reason": "...",
       "new_signals": [...]}  仅在 WAIT 时有值
    """
    try:
        from core.engines import signal_lineage as sl

        lid = sl.new_lineage_id("sig")
    except Exception:
        lid = ""

    # 0. 写审计日志：触发
    lid = _audit_log(
        code, "TRIGGER", signal_id,
        rationale=f"触发: 现价{current_price} 涨跌{change_pct:.2f}% 量{volume:.0f}",
        lineage_id=lid,
    ) or lid

    # 1. 配额检查
    ok, reason = check_quota(code)
    if not ok:
        _audit_log(code, "FILTER_REJECT", signal_id, rationale=reason, lineage_id=lid)
        return {"action": "SKIP", "reason": reason, "lineage_id": lid}

    # 2. 快速过滤：T+1锁仓检查
    if _is_t1_locked(code):
        _audit_log(code, "FILTER_REJECT", signal_id, rationale="T+1锁定", lineage_id=lid)
        return {"action": "SKIP", "reason": "T+1锁定，今日买入不可卖出", "lineage_id": lid}

    # 3. 缓存检查（4h内有效）
    cached = _check_analyst_cache(code)
    if cached:
        return {
            "action": "SKIP",
            "reason": f"缓存有效({cached['age_minutes']}分钟前)，复用结论: {cached['verdict']}",
            "lineage_id": lid,
        }

    # 4. 通过快速过滤 → 需要全量分析
    _audit_log(
        code, "FILTER_PASS", signal_id,
        rationale=f"通过过滤，启动全量TradingAgents。配额:{reason}",
        lineage_id=lid,
    )

    return {
        "action": "ANALYZE",
        "reason": reason,
        "lineage_id": lid,
        "context": {
            "signal_id": signal_id,
            "current_price": current_price,
            "change_pct": change_pct,
        },
    }


def close_loop(stock_code: str, old_signal_id: str, decision: str,
               new_signal_params: Optional[List[dict]] = None) -> dict:
    """
    闭环操作：TradingAgents分析完成后的处理。
    decision: "BUY" | "SELL" | "WAIT" | "DISMISS"
    """
    config = _load_json(CONFIG_PATH)
    if not config:
        return {"error": "无法读取guard_config.json"}

    signals = config.get("signals", [])

    if decision == "DISMISS":
        # 假信号：删除
        config["signals"] = [s for s in signals if s["id"] != old_signal_id]
        _save_json(CONFIG_PATH, config)
        _audit_log(stock_code, "DECISION", old_signal_id,
                   decision="DISMISS", rationale="假信号，已删除")
        return {"action": "DISMISS", "deleted": old_signal_id}

    elif decision == "WAIT":
        # 等待：删旧信号，注册新信号
        config["signals"] = [s for s in signals if s["id"] != old_signal_id]
        if new_signal_params:
            for ns in new_signal_params:
                config["signals"].append(ns)
        _save_json(CONFIG_PATH, config)
        _audit_log(stock_code, "DECISION", old_signal_id,
                   decision="WAIT",
                   rationale=f"维持等待，旧信号已删除，新信号已注册: {[ns['id'] for ns in (new_signal_params or [])]}")
        return {
            "action": "WAIT",
            "deleted": old_signal_id,
            "registered": [ns["id"] for ns in (new_signal_params or [])]
        }

    elif decision in ("BUY", "SELL"):
        # 操作信号：走完门禁后推送用户。信号执行后删除（同日不重复触发）
        config["signals"] = [s for s in signals if s["id"] != old_signal_id]
        _save_json(CONFIG_PATH, config)
        _audit_log(stock_code, "DECISION", old_signal_id,
                   decision=decision,
                   rationale=f"门禁通过，信号已消费，待请示链处理。")
        return {
            "action": decision,
            "deleted": old_signal_id,
            "note": "信号已消费，用户执行后如需继续盯盘，Agent应注册新信号"
        }

    return {"error": f"未知决策类型: {decision}"}


# === 内部工具函数 ===

def _load_json(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _save_json(path: str, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(path, 0o644)


def _load_profile(code: str) -> dict:
    """加载标的学习档案"""
    try:
        from stock_signal_profile import load_profile
        return load_profile(code)
    except ImportError:
        return {}


def _is_t1_locked(code: str) -> bool:
    """检查是否T+1锁定（今日买入不可卖）。

    T1.9 Bug B 修复（2026-06-26）：去重防止残留信号重复消费导致误锁。
    现场问题：sig_old 残留信号被重复消费，今日写入 5 条 000063 DECISION+BUY，
    导致 000063（早就持仓，今日未真实买入）被误判 T+1 锁定。
    修复：T+1 锁定应以"真实成交"为准，而非"门禁通过的决策记录"——门禁通过
    不等于实际买入。改为只统计 stock_trades 表今日真实 BUY 成交记录。
    """
    try:
        # T1.9 Bug B: 以 stock_trades 真实成交为准，而非 signal_audit 决策记录
        # signal_audit 的 DECISION+BUY 只代表"门禁通过待请示"，不等于实际买入
        import subprocess
        result = subprocess.run(
            [cfg.python, "-c",
             f"from stock_kb import StockKB; kb=StockKB(); "
             f"print(1 if kb.has_trade_today('{code}', 'BUY') else 0)"],
            capture_output=True, text=True, timeout=5,
            cwd=BASE
        )
        return result.stdout.strip() == "1"
    except Exception:
        # fallback: 退回原 audit_log 逻辑（保守锁定，宁可错锁不可漏锁）
        try:
            today = date.today().isoformat()
            if os.path.exists(AUDIT_LOG_PATH):
                with open(AUDIT_LOG_PATH) as f:
                    for line in f:
                        try:
                            entry = json.loads(line.strip())
                        except (json.JSONDecodeError, ValueError):
                            continue
                        if (entry.get("stock_code") == code and
                            entry.get("date") == today and
                            entry.get("action_type") == "DECISION" and
                            entry.get("decision") == "BUY"):
                            return True
        except Exception:
            pass
        return False


def _check_analyst_cache(code: str) -> Optional[dict]:
    """检查analyst_reports缓存是否有效（4h内+价格变化<3%）"""
    try:
        result = subprocess.run(
            [cfg.python, "-c",
             f"from stock_kb import StockKB; kb=StockKB(); "
             f"r=kb.check_cache('{code}'); print(r or '')"],
            capture_output=True, text=True, timeout=5,
            cwd=BASE
        )
        if result.stdout.strip():
            return json.loads(result.stdout.strip())
    except Exception:
        pass
    return None


def _audit_log(stock_code: str, action_type: str, signal_id: str = "",
               decision: str = "", rationale: str = "", cost_tokens: int = 0,
               lineage_id: str = ""):
    """写审计日志 + signal_lineage（若可用）"""
    lid = lineage_id
    try:
        from core.engines import signal_lineage as sl

        if not lid:
            lid = sl.new_lineage_id("sig")
        sl.append(
            action_type,
            "signal_loop",
            code=stock_code,
            lineage_id=lid,
            payload={
                "summary": rationale[:200] if rationale else action_type,
                "decision": decision,
                "signal_id": signal_id,
            },
        )
    except Exception:
        pass

    entry = {
        "timestamp": datetime.now().isoformat(),
        "date": date.today().isoformat(),
        "stock_code": stock_code,
        "action_type": action_type,
        "signal_id": signal_id,
        "decision": decision,
        "rationale": rationale,
        "cost_tokens": cost_tokens,
        "triggered_by": "auto",
        "lineage_id": lid,
    }
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG_PATH), exist_ok=True)
        with open(AUDIT_LOG_PATH, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return lid


# === CLI ===

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  signal_loop.py auto-generate        → 盘前自动生成信号")
        print("  signal_loop.py quota                → 查看今日配额")
        print("  signal_loop.py handle <signal_id> <code> <price> <pct> → 触发处理")
        print("  signal_loop.py close <code> <old_id> <BUY|SELL|WAIT|DISMISS> → 闭环")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "auto-generate":
        result = auto_generate()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "quota":
        quota = get_daily_quota()
        print(f"Tier A (持仓): {quota['tier_a']}")
        print(f"Tier B (高关注): {quota['tier_b']}")
        print(f"Tier C (低关注): {quota['tier_c']}")
        print(f"全局日上限: {quota['global_limit']}")
        print(f"午休黑名单: {quota['lunch_blackout']}")

    elif cmd == "handle":
        sid = sys.argv[2]
        code = sys.argv[3]
        price = float(sys.argv[4])
        pct = float(sys.argv[5])
        vol = float(sys.argv[6]) if len(sys.argv) > 6 else 0
        result = handle_trigger(sid, code, price, pct, vol)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "close":
        code = sys.argv[2]
        old_id = sys.argv[3]
        decision = sys.argv[4]
        result = close_loop(code, old_id, decision)
        print(json.dumps(result, ensure_ascii=False, indent=2))

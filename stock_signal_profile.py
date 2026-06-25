#!/usr/bin/env python3
"""
stock_signal_profile.py — 每标的个性化盯盘档案

每只股票/ETF独立维护一份学习档案，随时间进化：
- 初始：默认阈值
- 每日审计：根据触发真/假阳性率调优阈值
- CVRF：发现新模式写入 learned_patterns

档案存储在 signal_profiles/{code}.json
"""

import json
import os
from datetime import datetime, date
from typing import Dict, Optional, List

BASE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE, "signal_profiles")

DEFAULT_PROFILE = {
    "volatility_5d": 2.5,
    "volatility_20d": 2.5,
    "effective_thresholds": {
        "rapid_drop": -3.0,
        "rapid_surge": 3.0,
        "amplitude_pct": 4.0,
        "surge_peak_surge": 2.5,
        "surge_peak_vol_ratio": 2.0,
    },
    "trigger_history": {
        "total_triggers": 0,
        "true_positive": 0,
        "false_positive": 0,
        "false_positive_rate": 0.0,
    },
    "analysis_stats": {
        "total_analyses": 0,
        "cache_hits": 0,
        "tokens_saved_by_cache": 0,
    },
    "learned_patterns": [],
    "recent_trades": 0,
    "last_tuning": None,
    "created": None,
}


def load_profile(code: str) -> dict:
    """加载标的学习档案，不存在则返回默认"""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    path = os.path.join(PROFILE_DIR, f"{code}.json")
    if not os.path.exists(path):
        profile = DEFAULT_PROFILE.copy()
        profile["created"] = datetime.now().isoformat()
        save_profile(code, profile)
        return profile

    try:
        with open(path) as f:
            profile = json.load(f)
        # 补充缺失字段（兼容旧版）
        for k, v in DEFAULT_PROFILE.items():
            if k not in profile:
                profile[k] = v
        return profile
    except Exception:
        return DEFAULT_PROFILE.copy()


def save_profile(code: str, profile: dict):
    """保存标的学习档案"""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    path = os.path.join(PROFILE_DIR, f"{code}.json")
    with open(path, "w") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
    os.chmod(path, 0o644)


def update_volatility(code: str, vol_5d: float, vol_20d: float = None):
    """更新波动率数据"""
    profile = load_profile(code)
    profile["volatility_5d"] = round(vol_5d, 2)
    if vol_20d is not None:
        profile["volatility_20d"] = round(vol_20d, 2)

    # 自动调整阈值：波动率变了，阈值跟随
    t = profile["effective_thresholds"]
    t["rapid_drop"] = round(-max(3.0, vol_5d * 0.8), 1)
    t["rapid_surge"] = round(max(3.0, vol_5d * 0.8), 1)
    t["amplitude_pct"] = round(max(4.0, vol_5d * 1.2), 1)
    t["surge_peak_surge"] = round(max(2.5, vol_5d * 0.7), 1)

    save_profile(code, profile)


def record_trigger(code: str, was_true_positive: bool = None):
    """记录一次信号触发"""
    profile = load_profile(code)
    h = profile["trigger_history"]
    h["total_triggers"] += 1

    if was_true_positive is True:
        h["true_positive"] += 1
    elif was_true_positive is False:
        h["false_positive"] += 1

    if h["total_triggers"] > 0:
        h["false_positive_rate"] = round(h["false_positive"] / h["total_triggers"], 2)

    save_profile(code, profile)


def record_analysis(code: str, cache_hit: bool = False, tokens: int = 0):
    """记录一次分析（含缓存命中）"""
    profile = load_profile(code)
    s = profile["analysis_stats"]
    s["total_analyses"] += 1
    if cache_hit:
        s["cache_hits"] += 1
        s["tokens_saved_by_cache"] += tokens
    save_profile(code, profile)


def record_trade_count(code: str, count: int):
    """更新近期交易次数"""
    profile = load_profile(code)
    profile["recent_trades"] = count
    save_profile(code, profile)


def tune_thresholds(code: str) -> dict:
    """
    根据假阳性率调优阈值。
    假阳性率 > 60% → 调宽阈值（减少触发）
    假阳性率 < 20% + 真阳性 > 0 → 调窄阈值（更多捕捉）
    返回调参结果
    """
    profile = load_profile(code)
    h = profile["trigger_history"]
    t = profile["effective_thresholds"]
    changes = {}

    if h["total_triggers"] < 3:
        return {"status": "skip", "reason": "触发次数不足(<3)，暂不调参"}

    fpr = h["false_positive_rate"]

    if fpr > 0.6:
        # 假触发太多 → 放宽阈值 15%
        factor = 1.15
        for key in ["rapid_drop", "rapid_surge", "amplitude_pct", "surge_peak_surge"]:
            old = t[key]
            t[key] = round(abs(old) * factor, 1) * (-1 if old < 0 else 1)
            if abs(t[key] - old) > 0.1:
                changes[key] = f"{old} → {t[key]}"
        reason = f"假阳性率{fpr:.0%} > 60% → 阈值放宽15%"

    elif fpr < 0.2 and h["true_positive"] > 0:
        # 阈值太宽 → 收紧 10%
        factor = 0.9
        for key in ["rapid_drop", "rapid_surge", "amplitude_pct", "surge_peak_surge"]:
            old = t[key]
            t[key] = round(abs(old) * factor, 1) * (-1 if old < 0 else 1)
            # 不低于默认最小值
            defaults = {"rapid_drop": -3.0, "rapid_surge": 3.0,
                        "amplitude_pct": 4.0, "surge_peak_surge": 2.5}
            if key in defaults:
                if key in ("rapid_drop",):
                    t[key] = min(t[key], defaults[key])
                else:
                    t[key] = max(t[key], defaults[key])
            if abs(t[key] - old) > 0.1:
                changes[key] = f"{old} → {t[key]}"
        reason = f"假阳性率{fpr:.0%} < 20% → 阈值收紧10%"

    else:
        reason = f"假阳性率{fpr:.0%}在20%-60%之间，阈值保持"
        return {"status": "maintain", "reason": reason, "changes": {}}

    profile["effective_thresholds"] = t
    profile["last_tuning"] = datetime.now().isoformat()
    save_profile(code, profile)

    return {"status": "tuned", "reason": reason, "changes": changes}


def add_pattern(code: str, pattern: str):
    """添加学到的交易模式"""
    profile = load_profile(code)
    if pattern not in profile["learned_patterns"]:
        profile["learned_patterns"].append(pattern)
        # 只保留最近的10条
        if len(profile["learned_patterns"]) > 10:
            profile["learned_patterns"] = profile["learned_patterns"][-10:]
        save_profile(code, profile)


def get_summary(code: str) -> str:
    """生成标的盯盘摘要"""
    profile = load_profile(code)
    h = profile["trigger_history"]
    t = profile["effective_thresholds"]
    s = profile["analysis_stats"]

    lines = [
        f"📊 {code} 盯盘档案",
        f"  波动率: 5日{profile['volatility_5d']}% | 20日{profile['volatility_20d']}%",
        f"  个性化阈值: 急跌{t['rapid_drop']}% 急涨{t['rapid_surge']}% 振幅{t['amplitude_pct']}% 冲顶{t['surge_peak_surge']}%",
        f"  触发统计: 总{h['total_triggers']}次 | 真{h['true_positive']}次 假{h['false_positive']}次 (假阳性率{h['false_positive_rate']:.0%})",
        f"  分析效率: 总{s['total_analyses']}次 | 缓存命中{s['cache_hits']}次 | 省token {s['tokens_saved_by_cache']:,}",
    ]
    if profile["learned_patterns"]:
        lines.append(f"  学习模式 ({len(profile['learned_patterns'])}条):")
        for p in profile["learned_patterns"][-3:]:
            lines.append(f"    • {p}")
    if profile["last_tuning"]:
        lines.append(f"  上次调参: {profile['last_tuning'][:10]}")

    return "\n".join(lines)


# === CLI ===

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  stock_signal_profile.py summary <code>     → 盯盘摘要")
        print("  stock_signal_profile.py tune <code>         → 调优阈值")
        print("  stock_signal_profile.py record <code> <tp|fp> → 记录触发")
        print("  stock_signal_profile.py add-pattern <code> <pattern> → 添加模式")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "summary":
        code = sys.argv[2]
        print(get_summary(code))

    elif cmd == "tune":
        code = sys.argv[2]
        result = tune_thresholds(code)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "record":
        code = sys.argv[2]
        tp = sys.argv[3] == "tp" if len(sys.argv) > 3 else None
        record_trigger(code, tp)
        print(f"✓ {code} 触发已记录 ({sys.argv[3]})")

    elif cmd == "add-pattern":
        code = sys.argv[2]
        pattern = " ".join(sys.argv[3:])
        add_pattern(code, pattern)
        print(f"✓ {code} 模式已添加")

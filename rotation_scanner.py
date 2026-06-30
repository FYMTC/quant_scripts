#!/usr/bin/env python3
"""
rotation_scanner.py — T1.10 三期 行业轮动扫描器

§8.2 周末跑行业轮动扫描 → 上升行业 TOP3 + 下降行业 TOP3 → Tier C 选龙头 → Tier B
§8.3 bear/danger 态下暂停一切轮动布局，只做持仓防御

设计（见 .trae/documents/T1.10-phase3-implementation.md 决策 1+4）：
  - 行业数据：复用 stock_screener._fetch_industry_map() + baostock 个股 K 线聚合到行业级
  - market_regime gating：bear → status: paused
  - 串联 backtest_rotation：scan_and_export 内部 subprocess 调 backtest_rotation.py 拿门槛

输出：cfg.path.rotation_scan JSON
下游：signal_loop.auto_generate 读取 → Tier B 提升；backtest_rotation 验证门槛
"""

import sys
import os
import json
import io
import contextlib
import subprocess
import argparse
import tempfile
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from system_config import cfg

# 数据不足时行业最小成分数
MIN_INDUSTRY_CONSTITUENTS = 3
# TOP3 上升/下降行业
TOP_N = 3
# 默认回看天数
DEFAULT_LOOKBACK_DAYS = 20


def load_rotation_scan() -> dict:
    """读取最近一次轮动扫描结果。文件不存在返回 {}。"""
    path = cfg.path.rotation_scan
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def fetch_universe_codes() -> List[str]:
    """收集轮动扫描的标的池：持仓 + watch_list + screener_top15。

    去重，过滤 ST/退市（名称含 ST/*ST/退）。
    """
    codes = set()

    # 1. 持仓
    try:
        from stock_kb import StockKB
        pt = StockKB().read_portfolio_truth()
        for code, info in (pt.get("positions") or {}).items():
            codes.add(str(code))
    except Exception:
        pass

    # 2. guard_config watch_list / monitored_codes
    try:
        config_path = os.path.join(cfg.root, "guard_config.json")
        if os.path.exists(config_path):
            with open(config_path) as f:
                gc = json.load(f)
            watch = gc.get("watch_list") or gc.get("monitored_codes") or {}
            if isinstance(watch, dict):
                for code in watch.keys():
                    codes.add(str(code))
            elif isinstance(watch, list):
                for code in watch:
                    codes.add(str(code))
    except Exception:
        pass

    # 3. screener_top15
    try:
        top15_path = cfg.path.screener_top15
        if os.path.exists(top15_path):
            with open(top15_path) as f:
                top15 = json.load(f)
            # 兼容 list[dict] 或 dict
            if isinstance(top15, list):
                for item in top15:
                    code = str(item.get("code") or item.get("symbol") or "").strip()
                    if code:
                        codes.add(code)
            elif isinstance(top15, dict):
                for code in (top15.get("codes") or top15.keys()):
                    codes.add(str(code))
    except Exception:
        pass

    # 过滤 ST/退市（这里只能按代码过滤，名称过滤留给后续）
    return sorted(c for c in codes if c and len(c) == 6)


def _fetch_kline(code: str, days: int = 30, end_date: Optional[str] = None) -> Optional[List[dict]]:
    """baostock 取个股近 N 天 K 线。返回 [{date, close, volume, amount}, ...]。

    end_date (YYYY-MM-DD)：历史锚定日期，缺省=今天。供 backtest_rotation 重算历史周信号复用。
    """
    try:
        import baostock as bs
        market = 'sz' if code.startswith('00') or code.startswith('30') or code.startswith('15') else 'sh'
        anchor = datetime.strptime(end_date, "%Y-%m-%d") if end_date else datetime.now()
        end = anchor.strftime('%Y-%m-%d')
        start = (anchor - timedelta(days=days * 2)).strftime('%Y-%m-%d')
        with contextlib.redirect_stdout(io.StringIO()):
            bs.login()
            rs = bs.query_history_k_data_plus(
                f'{market}.{code}',
                'date,close,volume,amount',
                start_date=start, end_date=end,
                frequency='d', adjustflag='2'
            )
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            bs.logout()
        if not rows:
            return None
        result = []
        for r in rows:
            if len(r) < 4 or not r[1]:
                continue
            try:
                result.append({
                    "date": r[0],
                    "close": float(r[1]),
                    "volume": float(r[2]) if r[2] else 0.0,
                    "amount": float(r[3]) if r[3] else 0.0,
                })
            except (ValueError, IndexError):
                continue
        return result if len(result) >= 5 else None
    except Exception:
        return None


def compute_industry_metrics(codes: List[str], lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                             end_date: Optional[str] = None) -> Dict[str, dict]:
    """聚合个股数据到行业级。

    返回 {industry: {return_5d, return_20d, volume_ratio, constituent_count, top_stocks}}

    end_date (YYYY-MM-DD)：历史锚定日期，缺省=今天。供 backtest_rotation 重算历史周信号复用。
    """
    from stock_screener import _fetch_industry_map

    # 1. 拿 stock→industry 映射
    industry_map = _fetch_industry_map(codes)

    # 2. 按行业分组
    industry_codes: Dict[str, List[str]] = {}
    for code in codes:
        industry = industry_map.get(code, "")
        if not industry:
            continue
        industry_codes.setdefault(industry, []).append(code)

    # 3. 对每只 code 取 K 线，计算 5d/20d 涨幅 + 量比
    stock_metrics: Dict[str, dict] = {}
    for code in codes:
        kline = _fetch_kline(code, lookback_days + 10, end_date=end_date)
        if not kline or len(kline) < 6:
            continue
        closes = [k["close"] for k in kline]
        volumes = [k["volume"] for k in kline]

        # 5 日涨幅：今收 / 5 日前收 - 1
        ret_5d = None
        if len(closes) >= 6:
            ret_5d = (closes[-1] / closes[-6] - 1) * 100

        # 20 日涨幅
        ret_20d = None
        if len(closes) >= 21:
            ret_20d = (closes[-1] / closes[-21] - 1) * 100
        elif len(closes) >= 6:
            ret_20d = (closes[-1] / closes[0] - 1) * 100

        # 量比：近 5 日均量 / 近 20 日均量
        vol_ratio = None
        if len(volumes) >= 21 and sum(volumes[-21:-5]) > 0:
            vol_ratio = (sum(volumes[-5:]) / 5) / (sum(volumes[-21:-5]) / 16)
        elif len(volumes) >= 6 and sum(volumes[:-5]) > 0:
            vol_ratio = (sum(volumes[-5:]) / 5) / (sum(volumes[:-5]) / max(len(volumes) - 5, 1))

        stock_metrics[code] = {
            "return_5d": round(ret_5d, 2) if ret_5d is not None else None,
            "return_20d": round(ret_20d, 2) if ret_20d is not None else None,
            "volume_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
        }

    # 4. 聚合到行业级（取成分股中位数）
    import statistics
    result: Dict[str, dict] = {}
    for industry, ind_codes in industry_codes.items():
        valid = [stock_metrics[c] for c in ind_codes if c in stock_metrics]
        if len(valid) < MIN_INDUSTRY_CONSTITUENTS:
            # 成分不足，标记低置信
            result[industry] = {
                "return_5d": None,
                "return_20d": None,
                "volume_ratio": None,
                "constituent_count": len(valid),
                "low_confidence": True,
                "top_stocks": [],
            }
            continue

        rets_5d = [v["return_5d"] for v in valid if v["return_5d"] is not None]
        rets_20d = [v["return_20d"] for v in valid if v["return_20d"] is not None]
        vols = [v["volume_ratio"] for v in valid if v["volume_ratio"] is not None]

        # top_stocks：按 5d 涨幅排序取前 3
        top_stocks = sorted(
            [{"code": c, **stock_metrics[c]} for c in ind_codes if c in stock_metrics],
            key=lambda x: x.get("return_5d") or -999,
            reverse=True
        )[:3]

        result[industry] = {
            "return_5d": round(statistics.median(rets_5d), 2) if rets_5d else None,
            "return_20d": round(statistics.median(rets_20d), 2) if rets_20d else None,
            "volume_ratio": round(statistics.median(vols), 2) if vols else None,
            "constituent_count": len(valid),
            "top_stocks": top_stocks,
        }

    return result


def rank_industries(metrics: Dict[str, dict]) -> Dict:
    """按 5d 涨幅排序，输出 TOP3 up + TOP3 down。"""
    from stock_screener import is_blacklisted

    # 过滤黑名单 + 低置信 + 无 return_5d 的行业
    valid = []
    for industry, m in metrics.items():
        if is_blacklisted(industry):
            continue
        if m.get("low_confidence"):
            continue
        if m.get("return_5d") is None:
            continue
        valid.append((industry, m))

    # 按 return_5d 降序
    valid.sort(key=lambda x: x[1]["return_5d"], reverse=True)

    top3_up = []
    for industry, m in valid[:TOP_N]:
        top3_up.append({
            "industry": industry,
            "return_5d": m["return_5d"],
            "return_20d": m.get("return_20d"),
            "volume_ratio": m.get("volume_ratio"),
            "constituent_count": m.get("constituent_count"),
            "top_stocks": [
                {"code": s["code"], "return_5d": s.get("return_5d")}
                for s in m.get("top_stocks", [])[:3]
            ],
        })

    top3_down = []
    for industry, m in valid[-TOP_N:][::-1]:  # 取末尾 3 个并反转（最大跌幅在前）
        # 避免与 top3_up 重复（小行业池可能不足 6 个）
        if any(t["industry"] == industry for t in top3_up):
            continue
        top3_down.append({
            "industry": industry,
            "return_5d": m["return_5d"],
            "return_20d": m.get("return_20d"),
            "volume_ratio": m.get("volume_ratio"),
            "constituent_count": m.get("constituent_count"),
        })

    return {"top3_up": top3_up, "top3_down": top3_down}


def consult_market_regime() -> Dict:
    """调 market_regime HMM 拿当前大盘态。失败返回 {current_state: unknown}。"""
    try:
        from market_regime import fetch_index_data, fit_hmm
        import numpy as np
        closes = fetch_index_data()
        if closes is None or len(closes) < 60:
            return {"current_state": "unknown", "current_probs": []}
        log_returns = np.diff(np.log(closes))
        result = fit_hmm(log_returns)
        if not result or "error" in result:
            return {"current_state": "unknown", "current_probs": []}
        return {
            "current_state": result.get("current_state", "unknown"),
            "current_probs": result.get("current_probs", []),
        }
    except Exception:
        return {"current_state": "unknown", "current_probs": []}


def _run_backtest_gate() -> dict:
    """subprocess 调 backtest_rotation.py 拿门槛结果。失败返回 {passed: false, status: error}。"""
    try:
        script = os.path.join(cfg.root, "backtest_rotation.py")
        if not os.path.exists(script):
            return {"passed": False, "status": "script_missing"}
        result = subprocess.run(
            [cfg.python, script, "--json"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            return {"passed": False, "status": "error",
                    "stderr": result.stderr[:500] if result.stderr else ""}
        data = json.loads(result.stdout)
        return {
            "passed": bool(data.get("passed", False)),
            "rolling_4wk_hit_rate": data.get("rolling_4wk_hit_rate"),
            "status": data.get("status", "ok"),
        }
    except Exception as e:
        return {"passed": False, "status": "error", "error": str(e)}


def scan_and_export(lookback_days: int = DEFAULT_LOOKBACK_DAYS, run_backtest: bool = True) -> dict:
    """主入口：扫描 + 排名 + regime gating + 回测门槛 + 导出。"""
    result = {
        "run_at": datetime.now().isoformat(),
        "lookback_days": lookback_days,
        "status": "ok",
    }

    # 1. 大盘态 gating
    regime = consult_market_regime()
    result["regime"] = regime["current_state"]
    result["regime_probs"] = regime["current_probs"]

    if regime["current_state"] == "bear":
        # §8.3 bear 态暂停一切轮动布局
        result["status"] = "paused"
        result["reason"] = "market_regime=bear，暂停轮动布局（§8.3）"
        result["top3_up"] = []
        result["top3_down"] = []
        result["backtest_gate"] = {"passed": False, "status": "regime_paused"}
        _save_rotation_scan(result)
        return result

    # 2. 收集标的池
    codes = fetch_universe_codes()
    result["universe_size"] = len(codes)
    if len(codes) < 5:
        result["status"] = "insufficient_universe"
        result["reason"] = f"标的池仅 {len(codes)} 只 (<5)，扫描跳过"
        result["top3_up"] = []
        result["top3_down"] = []
        _save_rotation_scan(result)
        return result

    # 3. 聚合行业指标
    metrics = compute_industry_metrics(codes, lookback_days)
    result["industries_scanned"] = len(metrics)

    # 4. 排名
    ranking = rank_industries(metrics)
    result["top3_up"] = ranking["top3_up"]
    result["top3_down"] = ranking["top3_down"]

    # 5. 回测门槛
    if run_backtest:
        result["backtest_gate"] = _run_backtest_gate()
    else:
        result["backtest_gate"] = {"passed": True, "status": "skipped"}

    # 6. 若回测未通过 → status: paused（但保留 TOP3 供观察）
    if not result["backtest_gate"].get("passed"):
        result["status"] = "paused"
        result["reason"] = "回测门槛未通过（滚动 4 周命中率 < 50%），仅跟踪不布局"

    _save_rotation_scan(result)
    return result


def _save_rotation_scan(result: dict) -> None:
    """原子写入 cfg.path.rotation_scan。"""
    out_path = cfg.path.rotation_scan
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(out_path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, out_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def render_report(result: dict) -> str:
    """人类可读报告。"""
    lines = [
        "=" * 55,
        "  行业轮动扫描 (T1.10 三期)",
        f"  运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"  大盘态: {result.get('regime', 'unknown')}",
        f"  状态: {result.get('status', 'unknown')}",
        "=" * 55,
    ]
    if result.get("reason"):
        lines.append(f"\n⚠️  {result['reason']}")

    if result.get("top3_up"):
        lines.append(f"\n📈 上升行业 TOP3:")
        for i, ind in enumerate(result["top3_up"], 1):
            lines.append(
                f"  {i}. {ind['industry']:20s} "
                f"5d={ind['return_5d']:+.1f}% 20d={ind.get('return_20d', 0):+.1f}% "
                f"量比={ind.get('volume_ratio', 0):.2f}"
            )
            for s in ind.get("top_stocks", []):
                lines.append(f"      {s['code']} 5d={s.get('return_5d', 0):+.1f}%")

    if result.get("top3_down"):
        lines.append(f"\n📉 下降行业 TOP3:")
        for i, ind in enumerate(result["top3_down"], 1):
            lines.append(
                f"  {i}. {ind['industry']:20s} "
                f"5d={ind['return_5d']:+.1f}% 20d={ind.get('return_20d', 0):+.1f}% "
                f"量比={ind.get('volume_ratio', 0):.2f}"
            )

    gate = result.get("backtest_gate", {})
    lines.append(f"\n🚦 回测门槛: passed={gate.get('passed')} status={gate.get('status')}")
    if gate.get("rolling_4wk_hit_rate") is not None:
        lines.append(f"   滚动 4 周命中率: {gate['rolling_4wk_hit_rate']:.1%}")

    return "\n".join(lines)


def cli():
    p = argparse.ArgumentParser(description="行业轮动扫描器")
    p.add_argument("--json", action="store_true", help="输出 JSON（默认人类可读）")
    p.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                   help=f"回看天数（默认 {DEFAULT_LOOKBACK_DAYS}）")
    p.add_argument("--no-backtest", action="store_true",
                   help="跳过 backtest_rotation 门槛调用")
    args = p.parse_args()

    result = scan_and_export(
        lookback_days=args.lookback_days,
        run_backtest=not args.no_backtest,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_report(result))


if __name__ == "__main__":
    cli()

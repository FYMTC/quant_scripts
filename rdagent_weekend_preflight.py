#!python3
"""
rdagent_weekend_preflight.py — 周末RD-Agent因子挖掘前置
=====================================================
在周末周报前运行 rd_agent_quant.py --mode full，结果注入周报上下文。

运行时间：约2-5分钟（视股票池大小）
输出：因子挖掘摘要 + 因子库状态
"""

import subprocess, os, json, sys
from datetime import datetime
from system_config import cfg

SCRIPTS_DIR = cfg.root
PYTHON = cfg.python
RDAGENT_SCRIPT = os.path.join(SCRIPTS_DIR, "rd_agent_quant.py")
FACTOR_LIB = cfg.path.factor_library


def _run_risk_metrics_snapshot():
    """Qlib 不可用时的 fallback：用 risk_metrics 输出量化因子快照。"""
    import sys as _sys
    _sys.path.insert(0, SCRIPTS_DIR)
    from risk_metrics import calc_cvar, calc_multi_momentum, calc_garch_vol, calc_max_drawdown
    from data_converter import fetch_kline_baostock
    import numpy as np

    # 从 screener + positions 构建股票池
    codes = set()
    screener_path = os.path.join(SCRIPTS_DIR, "data", "screener_top15.json")
    if os.path.exists(screener_path):
        with open(screener_path) as f:
            scr = json.load(f)
        for r in scr.get("results", [])[:15]:
            codes.add(r.get("code", ""))
    # 加持仓
    guard_path = os.path.join(SCRIPTS_DIR, "guard_config.json")
    if os.path.exists(guard_path):
        with open(guard_path) as f:
            g = json.load(f)
        for code in (g.get("positions") or {}):
            codes.add(code)
        for code in (g.get("watch_list") or {}):
            codes.add(code)

    codes = {c for c in codes if c and len(c) == 6 and c.isdigit()}
    results = []
    for code in sorted(codes)[:20]:
        try:
            records = fetch_kline_baostock(code, "20260101", datetime.now().strftime("%Y%m%d"))
            if not records or len(records) < 20:
                continue
            closes = np.array([float(r['收盘']) for r in records])
            cvar = calc_cvar(closes)
            mom = calc_multi_momentum(closes)
            garch = calc_garch_vol(closes) if len(closes) >= 60 else None
            mdd = calc_max_drawdown(closes)
            results.append({
                "code": code,
                "n_days": len(closes),
                "cvar_95": round(float(cvar) * 100, 2) if cvar is not None else None,
                "momentum_20d": round(float(mom.get('20d', 0)) * 100, 1) if mom else None,
                "momentum_5d": round(float(mom.get('5d', 0)) * 100, 1) if mom else None,
                "consistency": mom.get('consistency') if mom else None,
                "garch_ann_vol_pct": round(float(garch.get('ann_vol', 0)) * 100, 1) if garch and garch.get('converged') else None,
                "vol_regime": garch.get('vol_regime') if garch and garch.get('converged') else None,
                "max_drawdown_pct": round(float(mdd) * 100, 2) if mdd is not None else None,
            })
        except Exception:
            continue

    if results:
        cvars = [r['cvar_95'] for r in results if r['cvar_95'] is not None]
        garchs = [r['garch_ann_vol_pct'] for r in results if r['garch_ann_vol_pct'] is not None]
        mdd_s = [r['max_drawdown_pct'] for r in results if r['max_drawdown_pct'] is not None]
        print(f"📊 risk_metrics 因子快照 ({len(results)} 标的)")
        print(f"  CVaR(95%): 平均={np.mean(cvars):.2f}% 最差={min(cvars):.2f}% 最佳={max(cvars):.2f}%")
        if garchs:
            print(f"  GARCH年化波动: 平均={np.mean(garchs):.1f}% 标的覆盖={len(garchs)}/{len(results)}")
        if mdd_s:
            print(f"  最大回撤: 平均={np.mean(mdd_s):.2f}% 最差={min(mdd_s):.2f}%")
        print()
        # Top/bottom by CVaR
        sorted_r = sorted(results, key=lambda r: r['cvar_95'] or -999, reverse=True)
        print("  Top 5 最低风险 (CVaR最接近0):")
        for r in sorted_r[:5]:
            print(f"    {r['code']}: CVaR={r['cvar_95']}% 动量20d={r['momentum_20d']}% 回撤={r['max_drawdown_pct']}%")
        print("  Bottom 5 最高风险:")
        for r in sorted_r[-5:]:
            print(f"    {r['code']}: CVaR={r['cvar_95']}% 动量20d={r['momentum_20d']}% 回撤={r['max_drawdown_pct']}%")

    print(f"\n📦 Qlib 不可用，未生成因子库。安装 Qlib 后重跑自动激活 RD-Agent。")

def main():
    print(f"# RD-Agent周末因子挖掘 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print()

    # ── 快速检查：Qlib 是否可用 ──
    qlib_available = False
    try:
        import qlib
        qlib.init(provider_uri=cfg.path.qlib_data_dir, region='cn')
        qlib_available = True
    except Exception:
        pass

    if not qlib_available:
        print("⚠️ Qlib 未安装或数据不可用，跳过 RD-Agent 因子挖掘。使用 risk_metrics 生成替代快照。")
        print()
        _run_risk_metrics_snapshot()
        return

    # ── Qlib 可用：正常运行 RD-Agent ──    
    # 检查上次因子库状态
    if os.path.exists(FACTOR_LIB):
        with open(FACTOR_LIB) as f:
            old = json.load(f)
        old_factors = old.get("stable_new_factors", [])
        print(f"📦 当前因子库: {len(old_factors)}个稳定因子")
        for f_ in old_factors:
            print(f"  {f_['name']}: IC={f_['avg_ic']}, Sharpe={f_['sharpe']}")
        print(f"  上次更新: {old.get('updated_at', '未知')}")
    else:
        print("📦 因子库为空，将首次运行")
    
    print(f"\n🚀 启动 rd_agent_quant.py --mode full ...")
    print()
    
    # 运行因子挖掘（最长10分钟）
    try:
        result = subprocess.run(
            [PYTHON, RDAGENT_SCRIPT, "--mode", "full"],
            cwd=SCRIPTS_DIR,
            capture_output=True, text=True, timeout=600
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        
        if result.returncode != 0:
            print(f"[ERROR] rd_agent_quant.py exit_code={result.returncode}")
            if err:
                print(f"[stderr] {err[:1000]}")
            # 只输出关键行
            for line in out.split("\n"):
                if any(kw in line for kw in ["📋", "✅", "🟡", "❌", "🎯", "稳定因子", "新稳定", "方向", "Bandit"]):
                    print(line)
        else:
            # 提取关键输出（去除非关键日志）
            for line in out.split("\n"):
                if any(kw in line for kw in ["📋", "✅", "🟡", "❌", "🎯", "🎰", "📊", 
                                               "稳定因子", "新稳定", "方向", "Bandit",
                                               "种子", "股票池", "时间"]):
                    print(line)
        
        print(f"\n✅ 因子挖掘完成 (exit_code={result.returncode})")
        
    except subprocess.TimeoutExpired:
        print("[ERROR] rd_agent_quant.py 超时（600秒），因子挖掘未完成")
        print("建议：周六手动跑一次 rd_agent_quant.py --mode full")
    except Exception as e:
        print(f"[ERROR] 因子挖掘异常: {e}")
    
    # 输出最终因子库状态
    if os.path.exists(FACTOR_LIB):
        with open(FACTOR_LIB) as f:
            new = json.load(f)
        new_factors = new.get("stable_new_factors", [])
        print(f"\n📦 因子库更新后: {len(new_factors)}个稳定因子")
        for f_ in new_factors:
            print(f"  ✅ {f_['name']}: IC={f_['avg_ic']}, Sharpe={f_['sharpe']}, {f_['n_stocks']}只")
    
    # ========== 第二步：LightGBM因子挖掘 ==========
    LGBM_WRAPPER = os.path.join(SCRIPTS_DIR, "lgbm_weekend_miner.py")
    if os.path.exists(LGBM_WRAPPER):
        print(f"\n{'='*60}")
        print(f"  🤖 LightGBM AI因子挖掘")
        print(f"{'='*60}")
        try:
            r2 = subprocess.run(
                [PYTHON, LGBM_WRAPPER],
                cwd=SCRIPTS_DIR,
                capture_output=True, text=True, timeout=600
            )
            lgbm_out = r2.stdout.strip()
            lgbm_err = r2.stderr.strip()
            if lgbm_out:
                print(lgbm_out)
            if lgbm_err:
                print(f"[lgbm stderr] {lgbm_err[:500]}", file=sys.stderr)
        except subprocess.TimeoutExpired:
            print("[ERROR] LightGBM因子挖掘超时")
        except Exception as e:
            print(f"[ERROR] LightGBM: {e}")

if __name__ == "__main__":
    main()

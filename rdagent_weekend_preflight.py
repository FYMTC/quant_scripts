#!/config/quant_env/bin/python3
"""
rdagent_weekend_preflight.py — 周末RD-Agent因子挖掘前置
=====================================================
在周末周报前运行 rd_agent_quant.py --mode full，结果注入周报上下文。

运行时间：约2-5分钟（视股票池大小）
输出：因子挖掘摘要 + 因子库状态
"""

import subprocess, os, json, sys
from datetime import datetime

SCRIPTS_DIR = "/config/quant_scripts"
PYTHON = "/config/quant_env/bin/python3"
RDAGENT_SCRIPT = os.path.join(SCRIPTS_DIR, "rd_agent_quant.py")
FACTOR_LIB = "/config/qlib_data/factor_library.json"

def main():
    print(f"# RD-Agent周末因子挖掘 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print()
    
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

#!/config/quant_env/bin/python3
"""
lgbm_weekend_miner.py — LightGBM因子挖掘包装器
==============================================
读取 guard_config.json 的所有持仓+自选标的，
逐只运行 ai_factor_miner.py 进行LightGBM因子挖掘。
输出摘要供周末周报上下文使用。

用法:
  /config/quant_env/bin/python3 lgbm_weekend_miner.py
"""

import subprocess, json, os, sys
from datetime import datetime

SCRIPTS_DIR = "/config/quant_scripts"
PYTHON = "/config/quant_env/bin/python3"
MINER = os.path.join(SCRIPTS_DIR, "ai_factor_miner.py")

def load_watchlist():
    """加载所有需分析的标的"""
    config_path = "/config/quant_scripts/guard_config.json"
    if not os.path.exists(config_path):
        print("[ERROR] guard_config.json not found", file=sys.stderr)
        return []
    
    with open(config_path) as f:
        config = json.load(f)
    
    # 合并持仓 + 自选（去重）
    codes = set()
    for code in config.get("positions", {}).keys():
        codes.add(code)
    for code in config.get("watch_list", {}).keys():
        codes.add(code)
    
    return sorted(codes)

def ensure_qlib_data(code):
    """检查Qlib CSV是否存在，不存在则尝试用Baostock生成"""
    csv_path = f"/config/qlib_data/features/{code}.csv"
    if os.path.exists(csv_path):
        return csv_path
    
    # 方法1: 用 data_converter (Baostock引擎v2)
    print(f"  ⏳ 无Qlib数据，尝试生成 {code}...", file=sys.stderr)
    converter = os.path.join(SCRIPTS_DIR, "data_converter.py")
    if os.path.exists(converter):
        try:
            r = subprocess.run(
                [PYTHON, converter, "--codes", code, "--end", datetime.now().strftime("%Y%m%d")],
                capture_output=True, text=True, timeout=60, cwd=SCRIPTS_DIR
            )
            if os.path.exists(csv_path):
                return csv_path
        except (subprocess.TimeoutExpired, Exception) as e:
            print(f"  ⚠️ converter失败: {e}", file=sys.stderr)
    
    # 方法2: 直接用 qlib_data_fetcher (Baostock直连)
    fetcher = os.path.join(SCRIPTS_DIR, "qlib_data_fetcher.py")
    if os.path.exists(fetcher):
        try:
            r = subprocess.run(
                [PYTHON, fetcher, "--codes", code, "--end", datetime.now().strftime("%Y%m%d")],
                capture_output=True, text=True, timeout=60, cwd=SCRIPTS_DIR
            )
            if os.path.exists(csv_path):
                return csv_path
        except Exception as e:
            print(f"  ⚠️ fetcher失败: {e}", file=sys.stderr)
    
    return None

def run_miner(code, timeout=120):
    """对单只标的运行因子挖掘"""
    # 先确保数据存在
    csv_path = ensure_qlib_data(code)
    if not csv_path:
        return -3, "", f"[SKIP] {code}: Qlib数据不可用，跳过"
    
    cmd = [PYTHON, MINER, "--code", code, "--csv", csv_path,
           "--output", "/config/qlib_data/lgbm_results"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          cwd=SCRIPTS_DIR)
        stdout = r.stdout.strip()
        stderr = r.stderr.strip()
        # 检查是否因数据不足导致训练失败
        if "0 样本" in stderr:
            return -3, "", f"[SKIP] {code}: 训练数据不足(0样本)"
        # 检查是否有未捕获异常
        if "Traceback" in stderr:
            err_line = [l for l in stderr.split("\n") if "Error" in l or "Exception" in l]
            detail = err_line[0][:80] if err_line else "脚本异常"
            return -3, "", f"[SKIP] {code}: {detail}"
        return r.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"[TIMEOUT] {code} 超过{timeout}秒"
    except Exception as e:
        return -2, "", str(e)

def main():
    codes = load_watchlist()
    print(f"# LightGBM因子挖掘 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print(f"\n📋 分析标的: {len(codes)}只: {', '.join(codes)}")
    print()
    
    results = []
    for i, code in enumerate(codes):
        print(f"  [{i+1}/{len(codes)}] {code}...", end=" ", flush=True)
        rc, out, err = run_miner(code)
        
        if rc == 0:
            result_line = ""
            for line in out.split("\n"):
                if "[DONE]" in line:
                    result_line = line.replace("[DONE] Result: ", "")[:200]
                    break
            print(f"✅ (acc={result_line[:40] if result_line else '?'})")
            results.append({"code": code, "status": "ok", "detail": result_line})
        elif rc == -3:
            # 数据不可用，SKIP不是失败
            print(f"⏭️ SKIP")
            results.append({"code": code, "status": "skip", "detail": err[:60]})
        elif rc == -1:
            print(f"⏰ 超时")
            results.append({"code": code, "status": "timeout"})
        else:
            err_short = err[:60] if err else "unknown"
            print(f"❌ {err_short}")
            results.append({"code": code, "status": "fail", "error": err_short})
    
    # 输出摘要
    ok_count = sum(1 for r in results if r["status"] == "ok")
    skip_count = sum(1 for r in results if r["status"] == "skip")
    fail_count = sum(1 for r in results if r["status"] not in ("ok", "skip"))
    print(f"\n### LightGBM因子挖掘总结")
    print(f"  标的数: {len(codes)}")
    print(f"  完成: {ok_count} | 跳过(无数据): {skip_count} | 失败: {fail_count}")
    print()

if __name__ == "__main__":
    main()

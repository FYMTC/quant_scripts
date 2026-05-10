#!/config/quant_env/bin/python3
"""
自动回测管线 - Phase 1 执行层骨架
将 Qlib/Agent 生成的 pending / pending_sim 信号拿来，跑历史/未来的一定天数的回测，
算出真实的历史/模拟夏普和回撤，写入 trade_db.signal_log 中
"""
import sys
import json
import sqlite3
from datetime import datetime, timedelta
import pandas as pd

sys.path.insert(0, '/config/quant_scripts')
from trade_db import SignalLog, DB_PATH
from backtest_strategy import fetch_data_and_features, run_backtest

def backtest_signal(code, entry_date, signal_type, target_price=None, stop_loss=None):
    """
    针对单个信号跑历史评估（从入场日起，持有一段时间，根据止盈止损或固定时间平仓）
    """
    start_date = entry_date.replace('-', '')
    # 往前多取几天算技术指标，往后取到最新用来评估结果
    sd_dt = datetime.strptime(start_date, "%Y%m%d") - timedelta(days=30)
    sd_str = sd_dt.strftime("%Y%m%d")
    
    # 调取 Baostock K线
    df = fetch_data_and_features(code, sd_str, end_date=datetime.now().strftime("%Y%m%d"))
    if df is None or df.empty:
        return None

    # 将买入点置入 DataFrame
    df["trade_signal"] = 0
    # 在 entry_date 那天的次日（或当天，如果有的话）产生买入信号
    entry_mask = df['date'] >= pd.to_datetime(entry_date)
    if not entry_mask.any():
        return None # 该日期后没有K线（比如当天刚出的信号，盘还没跑）
        
    entry_idx = df[entry_mask].index[0]
    
    # 模拟：买入
    if signal_type == "BUY":
        df.at[entry_idx, "trade_signal"] = 1
        
        # 找个平仓点：如果触发了目标价、止损价，或者持有满 N 天(例如5天)
        exit_idx = None
        for i in range(entry_idx + 1, len(df)):
            row = df.iloc[i]
            if stop_loss and row['low'] <= stop_loss:
                exit_idx = i
                break
            if target_price and row['high'] >= target_price:
                exit_idx = i
                break
            if i - entry_idx >= 5: # 默认最多持有5天看看
                exit_idx = i
                break
                
        if exit_idx is not None:
            df.at[exit_idx, "trade_signal"] = -1
    else:
        # SELL or HOLD 暂时不做持仓回测
        return None

    result = run_backtest(df, initial_capital=100000)
    return result

def run_all_pending_backtests():
    slog = SignalLog()
    with sqlite3.connect(slog.db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # 把 pending 的拿出来跑
        cursor.execute("SELECT * FROM signal_log WHERE status = 'pending'")
        signals = [dict(row) for row in cursor.fetchall()]

    if not signals:
        print("[INFO] 没有处于 pending 状态的信号需要回测。")
        return

    print(f"[INFO] 找到 {len(signals)} 个待回测信号...")
    updated_count = 0
    
    for sig in signals:
        code = sig['code']
        entry_date = sig['date']
        
        print(f"  - 回测信号: {code} [{sig['signal_type']}] on {entry_date}")
        res = backtest_signal(code, entry_date, sig['signal_type'], sig['target_price'], sig['stop_loss'])
        
        if res:
            # 判断通过标准：胜率大于0 或 最终收益为正 (作为基础测试)
            # 因为 run_backtest 返回的是 {"total_return_pct": ..., "max_drawdown_pct": ..., "win_rate": ...}
            is_passed = res.get("total_return_pct", 0) > 0
            new_status = "passed" if is_passed else "failed"
            
            slog.update_status(
                sig['id'],
                status=new_status,
                backtest_result=res
            )
            updated_count += 1
            print(f"    -> 结果: {new_status} (收益: {res.get('total_return_pct')}%)")
        else:
            print(f"    -> 结果: 无法回测 (可能无后续K线或不支持的信号类型)")
            
    print(f"[OK] 完成回测。更新了 {updated_count} 个信号状态。")

if __name__ == "__main__":
    run_all_pending_backtests()
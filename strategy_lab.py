#!/usr/bin/env python3
"""
策略实验室 — 尾盘策略回测+候选筛选
====================================
每天收盘后:
  1. 检查昨日候选 → 计算胜率/期望值
  2. 按尾盘过滤筛选今日候选
  3. 输出报告供夜报cron引用

尾盘过滤条件:
  ① 尾盘强势: close_pos > 0.7 (收盘价在日内高位)
  ② 放量: vol_ratio > 1.0 (大于5日均量)
  ③ 收阳: close > open
  ④ 板块共振: 涨幅大于所属板块中位数 (需要板块数据,暂用全市场涨幅代理)

用法:
  python strategy_lab.py check-yesterday    # 结算昨日候选
  python strategy_lab.py pick-today         # 筛选今日候选
  python strategy_lab.py report             # 输出累计统计
  python strategy_lab.py full               # 全流程: check + pick + report
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta

import baostock as bs

DB_PATH = "/config/quant_scripts/trade_log.db"
STOCKS = [
    ("sz.000938", "紫光股份"), ("sz.000063", "中兴通讯"),
    ("sh.512480", "半导体ETF"), ("sh.600522", "中天科技"),
    ("sz.002475", "立讯精密"), ("sh.600487", "亨通光电"),
    ("sh.600105", "永鼎股份"), ("sh.600900", "长江电力"),
    ("sz.002594", "比亚迪"), ("sh.603179", "新泉股份"),
    ("sz.300042", "朗科科技"), ("sh.600150", "中国船舶"),
    ("sz.002466", "天齐锂业"), ("sz.002297", "博云新材"),
]
STRATEGY = "tail_buy"


def get_db():
    return sqlite3.connect(DB_PATH)


def get_today():
    return datetime.now().strftime("%Y-%m-%d")


def get_yesterday():
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def fetch_daily(stock_codes, start_date, end_date):
    """获取多只股票的日线数据"""
    bs.login()
    all_data = {}
    for code, name in stock_codes:
        rs = bs.query_history_k_data_plus(
            code, "date,open,high,low,close,volume",
            start_date=start_date, end_date=end_date, frequency="d")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if rows:
            all_data[code] = {"name": name, "rows": rows}
    bs.logout()
    return all_data


def check_yesterday():
    """结算昨日候选: 用今日开盘价计算收益"""
    today = get_today()
    yesterday = get_yesterday()
    
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, stock_code, close_price FROM strategy_trials "
        "WHERE strategy_name=? AND pick_date=? AND check_date IS NULL",
        [STRATEGY, yesterday])
    pending = cur.fetchall()
    
    if not pending:
        print(f"无昨日({yesterday})候选需结算")
        db.close()
        return []
    
    codes = [(f"sz.{r[1]}" if r[1].startswith("0") or r[1].startswith("3") 
              else f"sh.{r[1]}", r[1]) for r in pending]
    
    data = fetch_daily(codes, today, today)
    
    results = []
    for trial_id, stock_code, close_price in pending:
        bs_code = f"sz.{stock_code}" if stock_code.startswith(("0", "3")) else f"sh.{stock_code}"
        rows = data.get(bs_code, {}).get("rows", [])
        
        if not rows:
            cur.execute(
                "UPDATE strategy_trials SET check_date=?, return_pct=NULL, win=NULL "
                "WHERE id=?", [today, trial_id])
            results.append({"code": stock_code, "status": "skip", "reason": "今日无数据(停牌?)"})
            continue
        
        today_open = float(rows[0][1])  # open (index 1, skip date at 0)
        ret = (today_open - close_price) / close_price * 100
        win = 1 if ret > 0 else 0
        
        cur.execute(
            "UPDATE strategy_trials SET check_date=?, next_open=?, return_pct=?, win=? "
            "WHERE id=?",
            [today, round(today_open, 3), round(ret, 2), win, trial_id])
        
        name = data.get(bs_code, {}).get("name", stock_code)
        results.append({
            "code": stock_code, "name": name,
            "buy": close_price, "sell": today_open,
            "ret": round(ret, 2), "win": win
        })
        print(f"  {'✅' if win else '❌'} {name} {close_price:.2f}→{today_open:.2f} {ret:+.2f}%")
    
    db.commit()
    db.close()
    return results


def pick_today(verbose=True):
    """按尾盘过滤筛选今日候选"""
    today = get_today()
    
    # 获取最近10天数据(用于均量计算)
    start = (datetime.now() - timedelta(days=15)).strftime("%Y-%m-%d")
    data = fetch_daily(STOCKS, start, today)
    
    candidates = []
    
    for bs_code, name in STOCKS:
        stock_data = data.get(bs_code, {}).get("rows", [])
        if len(stock_data) < 6:
            continue
        
        # 最近一行是今天
        row = stock_data[-1]
        date_str = row[0]
        vals = [float(x) for x in row[1:]]  # skip date
        if len(vals) < 5:
            continue
        
        open_p, high, low, close, volume = vals[:5]
        
        if high == low:
            continue  # 一字板跳过
        
        # 计算指标
        close_pos = (close - low) / (high - low)  # 收盘位置
        vols = [float(r[5]) for r in stock_data[-6:-1]]  # volume at index 5, skip date
        vol_ma5 = sum(vols) / len(vols) if vols else volume
        vol_ratio = volume / vol_ma5 if vol_ma5 else 1
        is_up = close > open_p
        day_return = (close - open_p) / open_p * 100
        
        # 过滤条件
        if close_pos < 0.7:
            continue
        if vol_ratio < 1.0:
            continue
        if not is_up:
            continue
        
        # 评分
        score = close_pos * 0.4 + min(vol_ratio, 3) / 3 * 0.3 + (1 if is_up else 0) * 0.3
        score = round(score, 2)
        
        details = json.dumps({
            "close_pos": round(close_pos, 2),
            "vol_ratio": round(vol_ratio, 2),
            "day_return": round(day_return, 2),
            "close": close
        }, ensure_ascii=False)
        
        stock_code = bs_code.split(".")[1]
        candidates.append((stock_code, name, close, score, details))
        
        if verbose:
            print(f"  ✅ {name}({stock_code}) 收盘{close:.2f} "
                  f"位置{close_pos:.0%} 量比{vol_ratio:.1f}x 评分{score:.2f}")
    
    # 按评分排序
    candidates.sort(key=lambda x: x[3], reverse=True)
    
    # 存入DB
    db = get_db()
    cur = db.cursor()
    saved = 0
    for code, name, close, score, details in candidates:
        # 检查今天是否已存
        cur.execute(
            "SELECT id FROM strategy_trials WHERE strategy_name=? AND pick_date=? AND stock_code=?",
            [STRATEGY, today, code])
        if cur.fetchone():
            continue
        cur.execute(
            "INSERT INTO strategy_trials (strategy_name, pick_date, stock_code, stock_name, "
            "close_price, filter_score, filter_details) VALUES (?,?,?,?,?,?,?)",
            [STRATEGY, today, code, name, close, score, details])
        saved += 1
    
    db.commit()
    db.close()
    
    if verbose:
        print(f"\n共 {len(candidates)} 只候选，新增 {saved} 条记录")
    
    return candidates


def report():
    """输出累计统计"""
    db = get_db()
    cur = db.cursor()
    
    # 总体统计
    cur.execute(
        "SELECT COUNT(*), SUM(win), AVG(return_pct), SUM(return_pct) "
        "FROM strategy_trials WHERE strategy_name=? AND win IS NOT NULL",
        [STRATEGY])
    total, wins, avg_ret, cum_ret = cur.fetchone()
    
    # 最近10笔
    cur.execute(
        "SELECT pick_date, stock_name, close_price, next_open, return_pct, win "
        "FROM strategy_trials WHERE strategy_name=? AND win IS NOT NULL "
        "ORDER BY pick_date DESC LIMIT 10",
        [STRATEGY])
    recent = cur.fetchall()
    
    # 今日候选
    today = get_today()
    cur.execute(
        "SELECT stock_name, stock_code, close_price, filter_score "
        "FROM strategy_trials WHERE strategy_name=? AND pick_date=? AND check_date IS NULL "
        "ORDER BY filter_score DESC",
        [STRATEGY, today])
    today_picks = cur.fetchall()
    
    db.close()
    
    print("=" * 55)
    print("  策略实验室 — 尾盘策略 (tail_buy)")
    print("=" * 55)
    
    if total:
        print(f"\n📊 累计统计")
        print(f"  交易次数: {total}")
        print(f"  胜率:     {wins}/{total} = {wins/total*100:.1f}%" if wins else "  胜率: N/A")
        print(f"  平均收益: {avg_ret:+.2f}%" if avg_ret else "  平均收益: N/A")
        print(f"  累计收益: {cum_ret:+.1f}%" if cum_ret else "  累计收益: N/A")
    
    if recent:
        print(f"\n📋 最近10笔")
        for d, n, buy, sell, ret, w in recent:
            print(f"  {'✅' if w else '❌'} {d} {n} {buy:.2f}→{sell:.2f} {ret:+.2f}%")
    
    if today_picks:
        print(f"\n🎯 今日候选({today})")
        for n, c, p, s in today_picks:
            print(f"  {n}({c}) 收盘{p:.2f} 评分{s:.2f}")
    else:
        print(f"\n🎯 今日({today})无候选(无标的通过尾盘过滤)")
    
    print()


def main():
    if len(sys.argv) < 2:
        print("Usage: strategy_lab.py [check-yesterday|pick-today|report|full]")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == "check-yesterday":
        check_yesterday()
    elif cmd == "pick-today":
        pick_today()
    elif cmd == "report":
        report()
    elif cmd == "full":
        print("=== 1. 结算昨日候选 ===")
        check_yesterday()
        print("\n=== 2. 筛选今日候选 ===")
        pick_today()
        print("\n=== 3. 累计统计 ===")
        report()
    else:
        print(f"未知命令: {cmd}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
回测：大跌尾盘买入 -> 第二天大涨中盘卖出 -> 否则持有
v2 - 加入趋势过滤(20日均线上方才开仓)

用法:
  python3 backtest_drop_buy_rally_sell.py \
    --codes 601919,002262,600211,601128
"""

import argparse, sys
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import io, contextlib

def fetch_kline(code, months=6):
    import baostock as bs
    end = datetime.now().strftime('%Y-%m-%d')
    start = (datetime.now() - timedelta(days=months*31)).strftime('%Y-%m-%d')
    with contextlib.redirect_stdout(io.StringIO()):
        lg = bs.login()
    market = 'sz' if code.startswith('00') or code.startswith('30') or code.startswith('15') else 'sh'
    rs = bs.query_history_k_data_plus(f'{market}.{code}',
        'date,open,high,low,close,volume,amount',
        start_date=start, end_date=end, frequency='d', adjustflag='2')
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    with contextlib.redirect_stdout(io.StringIO()):
        bs.logout()
    df = pd.DataFrame(rows, columns=['date','open','high','low','close','volume','amount'])
    df = df[df['close']!=''].copy()
    for c in ['open','high','low','close','volume','amount']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)
    df['ma20'] = df['close'].rolling(20).mean()
    return df

def backtest_single(code, name, df, drop_pct, rally_pct, hold_max, gap_pct):
    trades = []
    flips = []
    pos = 0
    entry_price = 0
    entry_date = None
    hold_days = 0

    for i in range(1, len(df)):
        t = df.iloc[i]
        p = df.iloc[i-1]
        tdate = t['date']
        above_ma20 = t['close'] > t['ma20'] if not pd.isna(t['ma20']) else True

        if pos == 1:
            hold_days += 1

            # 高开低走 → 开盘卖、尾盘买（同一天做T）
            gap_up = (t['open'] / p['close'] - 1) * 100
            ir = t['high'] - t['low']
            cp = (t['close'] - t['low']) / ir if ir > 0 else 0.5
            if gap_up >= gap_pct and t['close'] < t['open'] and cp < 0.3:
                flip_profit = (t['open'] - t['close']) * 100
                flip_pct = (t['open'] / t['close'] - 1) * 100
                flips.append({'date':tdate.strftime('%Y-%m-%d'),
                    'sell_price':round(t['open'],2),'buy_price':round(t['close'],2),
                    'profit':round(flip_profit,2),'pct':round(flip_pct,2),
                    'gap':round(gap_up,2)})
                entry_price = round(t['close'], 2)
                entry_date = tdate
                hold_days = 0
                continue

            # 持有超上限强制平仓
            if hold_days >= hold_max:
                ep = round(t['close'], 2)
                pnl = (ep - entry_price) * 100
                pnl_pct = (ep / entry_price - 1) * 100
                trades.append({'entry_date':entry_date.strftime('%Y-%m-%d'),'entry_price':entry_price,
                    'exit_date':tdate.strftime('%Y-%m-%d'),'exit_price':ep,
                    'pnl':round(pnl,2),'pnl_pct':round(pnl_pct,2),'hold_days':hold_days,
                    'reason':f'持有上限{hold_max}d强制平仓'})
                pos = 0; hold_days = 0; continue

            # 中盘涨幅达标自动止盈
            mid_price = (t['open'] + t['high']) / 2
            gain = (mid_price / entry_price - 1) * 100
            if gain >= rally_pct:
                ep = round(mid_price, 2)
                pnl = (ep - entry_price) * 100
                pnl_pct = (ep / entry_price - 1) * 100
                trades.append({'entry_date':entry_date.strftime('%Y-%m-%d'),'entry_price':entry_price,
                    'exit_date':tdate.strftime('%Y-%m-%d'),'exit_price':ep,
                    'pnl':round(pnl,2),'pnl_pct':round(pnl_pct,2),'hold_days':hold_days,
                    'reason':f'中盘涨{gain:.1f}%>={rally_pct}%'})
                pos = 0; hold_days = 0; continue

        if pos == 0:
            drop = (t['close'] / p['close'] - 1) * 100
            ir = t['high'] - t['low']
            cp = (t['close'] - t['low']) / ir if ir > 0 else 0.5
            if drop <= drop_pct and cp < 0.4 and above_ma20:
                entry_price = round(t['close'], 2)
                entry_date = tdate
                pos = 1; hold_days = 0

    if pos == 1:
        last = df.iloc[-1]
        ep = round(last['close'], 2)
        pnl = (ep - entry_price) * 100
        pnl_pct = (ep / entry_price - 1) * 100
        trades.append({'entry_date':entry_date.strftime('%Y-%m-%d'),'entry_price':entry_price,
            'exit_date':last['date'].strftime('%Y-%m-%d'),'exit_price':ep,
            'pnl':round(pnl,2),'pnl_pct':round(pnl_pct,2),'hold_days':hold_days,
            'reason':'期末强制平仓'})
    return trades, flips

def report(name, trades):
    if not trades:
        print(f'  {name:12s} 无交易信号'); return
    df = pd.DataFrame(trades)
    wins = df[df['pnl_pct']>0]
    loss = df[df['pnl_pct']<=0]
    total = round(df['pnl'].sum(), 2)
    wr = round(len(wins)/len(df)*100,1)
    avg_w = round(wins['pnl_pct'].mean(),2) if len(wins)>0 else 0
    avg_l = round(loss['pnl_pct'].mean(),2) if len(loss)>0 else 0
    avg_h = round(df['hold_days'].mean(),1)
    best = round(df['pnl_pct'].max(),2)
    worst = round(df['pnl_pct'].min(),2)
    print(f'  {name:12s} {len(df):>3d}笔 {wr:>5.1f}% {total:>+8.0f}元 '
          f'均盈{avg_w:+.2f}% 均亏{avg_l:+.2f}% 最亏{worst:+.2f}% 均持{avg_h:.1f}d')
    print(f'  最近3笔:')
    for t in trades[-3:]:
        print(f'    {t["entry_date"]}->{t["exit_date"]} '
              f'{t["entry_price"]:.2f}->{t["exit_price"]:.2f} '
              f'{t["pnl_pct"]:+.2f}% ({t["hold_days"]}d) {t["reason"]}')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', required=True)
    ap.add_argument('--months', type=int, default=6)
    ap.add_argument('--drop', type=float, default=-3.0)
    ap.add_argument('--rally', type=float, default=2.0)
    ap.add_argument('--hold', type=int, default=5)
    ap.add_argument('--gap', type=float, default=1.5, help='高开≥此值%+日内走低→开盘卖尾盘买')
    ap.add_argument('--names', default='')
    args = ap.parse_args()

    codes = args.codes.split(',')
    names = args.names.split(',') if args.names else codes
    while len(names) < len(codes):
        names.append(codes[len(names)])

    filter_str = ' + MA20趋势过滤'
    gap_str = f' + 高开≥{args.gap}%做T(开盘卖→尾盘买)'
    print(f'策略: 尾盘跌<={args.drop}%买入 -> 中盘涨>={args.rally}%卖出 -> 最多{args.hold}d{filter_str}{gap_str}')
    print(f'回测: 近{args.months}个月\n')
    print(f'{"标的":12s} {"笔数":>4s} {"胜率":>6s} {"总盈亏":>9s} {"做T":>5s} {"T盈利":>7s} {"均盈":>7s} {"均亏":>7s} {"最亏":>7s} {"均持":>5s}')
    print('-' * 80)

    all_t = []
    all_f = []
    total_flip_profit = 0
    for code, name in zip(codes, names):
        df = fetch_kline(code, months=args.months)
        if len(df) < 30:
            print(f'  {name:12s} ⚠️ 数据不足({len(df)}条)')
            continue
        trades, flips = backtest_single(code, name, df, args.drop, args.rally, args.hold, args.gap)
        f_profit = round(sum(f['profit'] for f in flips), 2)
        total_flip_profit += f_profit
        report(name, trades)
        if flips:
            f_pnl = f_profit
            print(f'  {"":4s}做T{len(flips)}次 +{f_pnl:+.0f}元 最近:{flips[-1]["date"]} 卖{flips[-1]["sell_price"]}→买{flips[-1]["buy_price"]}')
        else:
            print(f'  {"":4s}做T: 无')
        all_t.extend(trades)
        all_f.extend(flips)

    if all_t or all_f:
        df_a = pd.DataFrame(all_t) if all_t else pd.DataFrame()
        wins = df_a[df_a['pnl_pct']>0] if len(df_a) > 0 else pd.DataFrame()
        loss = df_a[df_a['pnl_pct']<=0] if len(df_a) > 0 else pd.DataFrame()
        total_pnl = round(sum(t['pnl'] for t in all_t) + total_flip_profit, 2)
        trade_pnl = round(sum(t['pnl'] for t in all_t), 2)
        wr = round(len(wins)/len(df_a)*100, 1) if len(df_a) > 0 else 0
        print()
        print('=' * 80)
        print(f'合计: {len(all_t)}笔完整交易, 胜率{wr}%, 交易盈亏{trade_pnl:+.0f}元')
        print(f'      做T{len(all_f)}次, T盈利{total_flip_profit:+.0f}元')
        print(f'      总计盈亏{total_pnl:+.0f}元(每100股)')
        if len(wins) > 0 and len(loss) > 0:
            print(f'      盈/亏比: {round(wins["pnl_pct"].mean(),2)}% / {round(loss["pnl_pct"].mean(),2)}%')

main()

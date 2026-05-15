#!/usr/bin/env python3
"""DeepSeek API审计 — 实时统计调用频率和token消耗，超标告警

用法:
  python api_audit.py                          # 一次性统计今日
  python api_audit.py --watch                   # 实时监控(5秒刷新)
  python api_audit.py --once                    # 静默，仅超标时输出

阈值(default):
  - 日Token上限: 10M
  - 瞬时速率: 30 calls/min
  - 单cron连续: 10 calls (防止失控循环)
"""

import os
import re
import sys
import time
import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict

HERMES_LOG = os.path.expanduser("~/.hermes/logs/agent.log")
CST = timezone(timedelta(hours=8))

# 阈值配置
DAILY_TOKEN_LIMIT = 10_000_000   # 日Token上限
INSTANT_CALLS_LIMIT = 30         # 每分钟调用上限
CRON_CONSECUTIVE_LIMIT = 10      # 单个cron连续调用上限
COST_PER_M_INPUT = 0.2           # flash: $0.2/M input
COST_PER_M_OUTPUT = 2.0          # flash: $2/M output
COST_PER_M_CACHE = 0.05          # flash: $0.05/M cache hit


def parse_log_line(line: str) -> dict | None:
    """解析单行agent.log，提取API调用信息"""
    if 'API call' not in line or 'deepseek' not in line:
        return None
    
    ts_match = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
    job_match = re.search(r'\[(cron_\w+)_\d+\]', line)
    session_match = re.search(r'session=([a-f0-9-]+)', line)
    model_match = re.search(r'model=([a-z0-9._-]+)', line)
    
    api = re.search(r'in=(\d+) out=(\d+) total=(\d+)', line)
    cache = re.search(r'cache=(\d+)/(\d+)', line)
    latency = re.search(r'latency=([\d.]+)s', line)
    
    if not api:
        return None
    
    return {
        'ts': ts_match.group(1) if ts_match else '?',
        'job': job_match.group(1) if job_match else ('session:' + session_match.group(1)[:8] if session_match else '?'),
        'model': model_match.group(1) if model_match else '?',
        'in_tokens': int(api.group(1)),
        'out_tokens': int(api.group(2)),
        'total_tokens': int(api.group(3)),
        'cache_hit': int(cache.group(1)) if cache else 0,
        'cache_total': int(cache.group(2)) if cache else 0,
        'latency': float(latency.group(1)) if latency else 0,
    }


def compute_cost(in_tokens: int, out_tokens: int, cache_hit: int) -> float:
    """计算单次API调用成本(美元)"""
    uncached_input = max(0, in_tokens - cache_hit)
    return (uncached_input / 1_000_000 * COST_PER_M_INPUT
            + out_tokens / 1_000_000 * COST_PER_M_OUTPUT
            + cache_hit / 1_000_000 * COST_PER_M_CACHE)


def audit_once():
    """一次性统计今日调用"""
    today = datetime.now(CST).strftime('%Y-%m-%d')
    
    calls = []
    with open(HERMES_LOG) as f:
        for line in f:
            if today not in line[:20]:
                continue
            parsed = parse_log_line(line)
            if parsed:
                calls.append(parsed)
    
    if not calls:
        print(f"[{today}] 今日无API调用记录")
        return
    
    total_in = sum(c['in_tokens'] for c in calls)
    total_out = sum(c['out_tokens'] for c in calls)
    total_cache = sum(c['cache_hit'] for c in calls)
    cost = sum(compute_cost(c['in_tokens'], c['out_tokens'], c['cache_hit']) for c in calls)
    
    # 按job分组
    job_groups = defaultdict(lambda: {'count': 0, 'tokens': 0})
    for c in calls:
        job_groups[c['job']]['count'] += 1
        job_groups[c['job']]['tokens'] += c['total_tokens']
    
    print(f"\n{'='*60}")
    print(f"  DeepSeek API 审计报告 — {today}")
    print(f"{'='*60}")
    print(f"  总调用次数: {len(calls)}")
    print(f"  总Token:    {(total_in+total_out)/1e6:.1f}M (in:{total_in/1e6:.1f}M out:{total_out/1e6:.1f}M)")
    print(f"  缓存命中:   {total_cache/1e6:.1f}M ({total_cache*100//max(1,total_in)}%)")
    print(f"  估算成本:   ${cost:.2f}")
    print(f"  峰值速率:   {max(job_groups[j]['count'] for j in job_groups)} calls/min")
    
    # 超标告警
    alerts = []
    if total_in + total_out > DAILY_TOKEN_LIMIT:
        alerts.append(f"⚠️ 日Token {total_in+total_out:,} 超限 {DAILY_TOKEN_LIMIT:,}")
    
    # 检查单个cron是否失控
    for job, stats in sorted(job_groups.items(), key=lambda x: -x[1]['count']):
        if stats['count'] > 50:
            marker = '🔴' if stats['count'] > 500 else '🟡' if stats['count'] > 100 else ''
            pct = stats['tokens'] * 100 // max(1, total_in + total_out)
            print(f"  {marker} {job}: {stats['count']}次 {stats['tokens']/1e6:.1f}M ({pct}%)")
    
    if alerts:
        print(f"\n{'🚨 告警':-^60}")
        for a in alerts:
            print(f"  {a}")
    else:
        print(f"\n{'✅ 正常':-^60}")
    
    # JSON输出供程序调用
    result = {
        'date': today,
        'total_calls': len(calls),
        'total_tokens': total_in + total_out,
        'cost_usd': round(cost, 4),
    }
    return result


def watch():
    """实时监控 — 每5秒刷新"""
    today = datetime.now(CST).strftime('%Y-%m-%d')
    last_pos = 0
    
    print(f"🔍 实时监控 API 调用 (5s刷新) — {today}")
    print(f"   阈值: {DAILY_TOKEN_LIMIT/1e6:.0f}M token/日, {INSTANT_CALLS_LIMIT}calls/min")
    print(f"{'':-^60}")
    
    while True:
        time.sleep(5)
        
        # 读取增量
        calls = []
        with open(HERMES_LOG) as f:
            f.seek(0, 2)  # end
            current_pos = f.tell()
            if current_pos > last_pos:
                f.seek(last_pos)
                for line in f:
                    if today in line[:20]:
                        parsed = parse_log_line(line)
                        if parsed:
                            calls.append(parsed)
                last_pos = current_pos
        
        if not calls:
            continue
        
        now_str = datetime.now(CST).strftime('%H:%M:%S')
        total_tok = sum(c['total_tokens'] for c in calls)
        cost = sum(compute_cost(c['in_tokens'], c['out_tokens'], c['cache_hit']) for c in calls)
        job_counts = defaultdict(int)
        for c in calls:
            job_counts[c['job']] += 1
        
        # 告警判断
        alerts = []
        if len(calls) > INSTANT_CALLS_LIMIT:
            alerts.append(f"🔴 瞬时速率 {len(calls)}calls/min > {INSTANT_CALLS_LIMIT}")
        for job, cnt in job_counts.items():
            if cnt > CRON_CONSECUTIVE_LIMIT:
                alerts.append(f"🔴 {job} 连续{cnt}次调用")

        status = f"[{now_str}] {len(calls):>3}calls {total_tok/1000:>6.0f}K ${cost:.4f}"
        if alerts:
            print(status + "  " + " | ".join(alerts))
        else:
            print(status)


if __name__ == '__main__':
    if '--watch' in sys.argv:
        try:
            watch()
        except KeyboardInterrupt:
            print("\n退出监控")
    elif '--once' in sys.argv:
        audit_once()
    else:
        audit_once()

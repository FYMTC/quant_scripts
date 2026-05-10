#!/usr/bin/env python3
"""
Hermes 每日Token消耗报告
数据来源：~/.hermes/state.db (Hermes原生session统计)
定价来源：DeepSeek CSV导出 (platform.deepseek.com/usage)

注意：state.db统计的token数 < DeepSeek CSV实际数（约差~50%）。
本报告以state.db为"下限估计"，官方CSV为准确账单。
"""
import sqlite3, os, json
from datetime import datetime, timezone, timedelta
from collections import defaultdict

TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TZ)
TODAY = NOW.strftime('%Y-%m-%d')

DB = os.path.expanduser('~/.hermes/state.db')

# ── DeepSeek 官方定价 (来自CSV) ──
PRICING = {
    'deepseek-v4-flash': {
        'output': 0.000002,
        'input_cache_miss': 0.000001,
        'input_cache_hit': 0.00000002,
    },
    'deepseek-v4-pro': {
        'output': 0.000006,
        'input_cache_miss': 0.000003,
        'input_cache_hit': 0.000000025,
    },
    'deepseek-chat & deepseek-reasoner': {
        'output': 0.000003,
        'input_cache_miss': 0.000002,
        'input_cache_hit': 0.0000002,
    },
}

DISCOUNT_PRO = 0.25  # v4-pro 2.5折至5/5

def fmt_usd(v):
    return f"${v:.4f}"

def fmt_cny(v):
    return f"¥{v:.2f}"

def calc_cost(model, out_tokens, miss_tokens, hit_tokens):
    """按DeepSeek官价计算费用"""
    p = PRICING.get(model, PRICING['deepseek-v4-flash'])
    out = out_tokens * p['output']
    miss = miss_tokens * p['input_cache_miss']
    hit = hit_tokens * p['input_cache_hit']
    return out + miss + hit

def load_db():
    if not os.path.exists(DB):
        return None
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def get_breakdowns(conn):
    """按模型+来源拆分今日token"""
    rows = conn.execute('''
        SELECT source, input_tokens, output_tokens, cache_read_tokens,
               model, billing_provider
        FROM sessions 
        WHERE DATE(started_at, 'unixepoch') = DATE('now', 'localtime')
        AND input_tokens > 0
    ''').fetchall()
    
    # Group by model (infer from billing_provider or source)
    by_model = defaultdict(lambda: {'sessions': 0, 'out': 0, 'inp': 0, 'cr': 0, 'sources': defaultdict(int)})
    
    # Also by source
    by_source = defaultdict(lambda: {'sessions': 0, 'out': 0, 'inp': 0, 'cr': 0})
    
    for r in rows:
        src = r['source'].split('.')[-1] if '.' in r['source'] else r['source']
        
        # Assume v4-flash for most, pro for sessions with large context
        # (state.db model field may be empty; infer from name patterns)
        inp = r['input_tokens'] or 0
        out = r['output_tokens'] or 0
        cr = r['cache_read_tokens'] or 0
        
        # Try to detect pro vs flash from model field or source name
        model_key = 'deepseek-v4-flash'
        model_field = r['model'] or ''
        if 'pro' in model_field.lower() or ('pro' in src.lower()):
            model_key = 'deepseek-v4-pro'
        
        by_model[model_key]['sessions'] += 1
        by_model[model_key]['out'] += out
        by_model[model_key]['inp'] += inp
        by_model[model_key]['cr'] += cr
        by_model[model_key]['sources'][src] += 1
        
        by_source[src]['sessions'] += 1
        by_source[src]['out'] += out
        by_source[src]['inp'] += inp
        by_source[src]['cr'] += cr
    
    return by_model, by_source

def run():
    conn = load_db()
    if not conn:
        return "❌ state.db 未找到"

    by_model, by_source = get_breakdowns(conn)
    
    lines = []
    lines.append(f"📊 Hermes Token消耗报告 | {TODAY}")
    lines.append("")
    lines.append("━━━ 按来源 ━━━")
    
    total_sessions = 0
    total_out = 0
    total_inp = 0
    total_cr = 0
    
    for src, d in sorted(by_source.items(), key=lambda x: -x[1]['inp']):
        lines.append(f"  {src:<12} {d['sessions']:>3}次  ↑{d['inp']/1000:>7.1f}K  ↓{d['out']/1000:>5.1f}K")
        total_sessions += d['sessions']
        total_out += d['out']
        total_inp += d['inp']
        total_cr += d['cr']
    
    lines.append(f"  {'合计':<12} {total_sessions:>3}次  ↑{total_inp/1000:>7.1f}K  ↓{total_out/1000:>5.1f}K")
    lines.append("")
    
    # Cost by model
    lines.append("━━━ 费用估算 ━━━")
    total_cost = 0
    
    for model, d in sorted(by_model.items(), key=lambda x: -x[1]['out']):
        # state.db doesn't split miss/hit, use cache_read as proxy
        miss = max(0, d['inp'] - d['cr'])
        hit = d['cr']
        cost = calc_cost(model, d['out'], miss, hit)
        
        model_label = model.replace('deepseek-', '')
        discount = DISCOUNT_PRO if 'pro' in model else 1.0
        actual = cost * discount
        
        lines.append(f"  {model_label:<15}  ↑{d['inp']/1000:>7.1f}K  ↓{d['out']/1000:>5.1f}K")
        lines.append(f"                  原始: {fmt_usd(cost)} → {fmt_cny(cost*7.2)}")
        if discount < 1:
            lines.append(f"                  折后(2.5折): {fmt_usd(actual)} → {fmt_cny(actual*7.2)}")
        total_cost += actual
    
    lines.append("")
    lines.append(f"  💰 预估总计: {fmt_cny(total_cost*7.2)}")
    lines.append(f"  ⚠️  注意: state.db统计偏低(约~50%)，实际以CSV账单为准")
    lines.append("")
    
    # Quick tips
    lines.append("━━━ 省费提示 ━━━")
    pro_count = by_model.get('deepseek-v4-pro', {}).get('sessions', 0)
    flash_count = by_model.get('deepseek-v4-flash', {}).get('sessions', 0)
    if pro_count > flash_count * 2:
        lines.append(f"  ⚡ 今日pro请求({pro_count}次)远超flash({flash_count}次)")
        lines.append(f"  💡 非分析类cron可切flash省钱")
    
    lines.append("")
    lines.append(f"📌 准确账单 → https://platform.deepseek.com/usage")
    
    return "\n".join(lines)

if __name__ == '__main__':
    print(run())

#!/config/quant_env/bin/python3
"""
512480 半导体ETF 轻量门禁分析脚本
获取：资金流向 + 板块强度 + 行情快照 → 三维评分 → 综合裁决
"""
import sys, os, json, time, subprocess
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

def curl_get(url, timeout=8):
    """安全的HTTP GET"""
    try:
        out = subprocess.run(
            ["curl", "-s", "--connect-timeout", "5", "--max-time", str(timeout),
             "-H", "User-Agent: Mozilla/5.0",
             "-H", "Referer: https://quote.eastmoney.com/",
             url],
            capture_output=True, text=True, timeout=(timeout + 2)
        )
        return out.stdout.strip()
    except:
        return None

def fetch_quote_em(code):
    """从东方财富获取行情（含更多字段）"""
    # ETF: 512480 → secid=1.512480
    secid = f"1.{code}" if code.startswith(("6","5")) else f"0.{code}"
    # 包含资金流向字段: f62(主力净流入), f184(超大单净流入), f185(大单净流入), f186(中单), f187(小单)
    fields = "f43,f44,f45,f46,f47,f48,f57,f58,f60,f168,f170,f62,f184,f185,f186,f187,f188,f189"
    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields={fields}"
    
    raw = curl_get(url)
    if not raw:
        return None
    try:
        d = json.loads(raw)
        if d.get("rc") != 0 or not d.get("data"):
            return None
        rd = d["data"]
        
        # ETF除数=1000
        div = 1000
        
        result = {
            "code": code,
            "name": rd.get("f58", ""),
            "price": int(rd.get("f43") or 0) / div,
            "pct": float(rd.get("f170") or 0) / 100,
            "high": int(rd.get("f44") or 0) / div,
            "low": int(rd.get("f45") or 0) / div,
            "open": int(rd.get("f46") or 0) / div,
            "pre_close": int(rd.get("f60") or 0) / div,
            "vol": int(rd.get("f47") or 0),  # 手
            "amount": int(rd.get("f48") or 0) / 10000,  # 万
            "turnover": float(rd.get("f168") or 0) / 100,
            # 资金流向（万元）
            "main_net_flow": float(rd.get("f62") or 0) / 10000,  # 主力净流入
            "super_large_net": float(rd.get("f184") or 0) / 10000,  # 超大单净流入
            "large_net": float(rd.get("f185") or 0) / 10000,  # 大单净流入
            "medium_net": float(rd.get("f186") or 0) / 10000,  # 中单净流入
            "small_net": float(rd.get("f187") or 0) / 10000,  # 小单净流入
            "_source": "eastmoney",
        }
        return result
    except Exception as e:
        print(f"  [WARN] Eastmoney parse error: {e}")
        return None

def fetch_sector_flow():
    """获取行业板块资金流向排名"""
    url = "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f62&fs=m:90+t2&fields=f12,f14,f2,f3,f62,f184,f66"
    raw = curl_get(url)
    if not raw:
        return None
    try:
        d = json.loads(raw)
        if not d.get("data") or not d["data"].get("diff"):
            return None
        
        sectors = []
        for item in d["data"]["diff"]:
            sectors.append({
                "code": item.get("f12", ""),
                "name": item.get("f14", ""),
                "pct": float(item.get("f3") or 0),
                "main_net_flow": float(item.get("f62") or 0) / 10000,  # 万
            })
        return sectors
    except Exception as e:
        print(f"  [WARN] Sector flow parse error: {e}")
        return None

def find_semiconductor_sector(sectors):
    """找到半导体板块在行业排名中的位置"""
    for i, s in enumerate(sectors):
        if "半导体" in s.get("name", ""):
            return i + 1, s  # 1-indexed rank
    return None, None

def score_capital_flow(main_net, amount, super_large_net, small_net, pct):
    """
    资金流评分：-2 ~ +2
    考虑：主力净流入/成交额比、超大单占比、散户反向
    """
    score = 0
    
    # 1. 主力净流入/成交额比例
    if amount > 0:
        main_ratio = main_net / amount
        if main_ratio > 0.15:
            score += 1.0
        elif main_ratio > 0.05:
            score += 0.5
        elif main_ratio > 0:
            score += 0.2
        elif main_ratio > -0.05:
            score -= 0.2
        elif main_ratio > -0.15:
            score -= 0.5
        else:
            score -= 1.0
    
    # 2. 超大单vs大单（超大单=机构）
    if main_net != 0:
        super_ratio = abs(super_large_net) / (abs(main_net) + 0.01)
        if main_net > 0 and super_large_net > 0:
            if super_ratio > 0.5:
                score += 0.5
            else:
                score += 0.2
        elif main_net < 0 and super_large_net < 0:
            if super_ratio > 0.5:
                score -= 0.5
            else:
                score -= 0.2
    
    # 3. 散户流向（逆向指标）
    if small_net < 0 and main_net > 0:
        score += 0.3  # 主力买散户卖=好
    elif small_net > 0 and main_net < 0:
        score -= 0.3  # 主力卖散户买=差
    
    # 4. 涨跌幅加速
    if pct > 3:
        score += 0.2
    
    return max(-2, min(2, score))

def score_technical(price, high, low, open_price, pre_close, pct, vol):
    """
    技术面评分：-2 ~ +2
    基于：日内形态、量价关系、均线位置（使用quant_context中的MA数据）
    """
    score = 0
    
    # 1. 日内形态：当前位置在日内区间的哪里
    day_range = high - low
    if day_range > 0:
        pos = (price - low) / day_range
        if pos > 0.8:
            score += 0.3  # 接近日内高点
        elif pos < 0.2:
            score -= 0.3  # 接近日内低点
    
    # 2. 实体 vs 影线
    body_pct = abs(price - open_price) / open_price * 100 if open_price > 0 else 0
    if body_pct > 2:
        if price > open_price:
            score += 0.5  # 大阳线
        else:
            score -= 0.5  # 大阴线
    
    # 3. 距前日收盘
    if pct > 3:
        score += 0.3  # 加速上涨
    elif pct < -3:
        score -= 0.3
    
    # 4. 距前高距离（2.13=60日最高，2.132=日内高）
    # 已在触及前高位置，突破or假突破？
    # quant_context: 5/18 insight 高开低走+大振幅，关注假突破
    score -= 0.5  # 前高阻力位警告
    
    # 5. 均线多头排列 (MA5=2.07, MA20=1.86, MA60=1.65)
    # 价格远高于所有均线 → 短期超买风险
    if price > 2.07:  # 高于MA5
        premium_vs_ma20 = (price - 1.86) / 1.86 * 100  # +14.5%
        premium_vs_ma60 = (price - 1.65) / 1.65 * 100  # +29.1%
        if premium_vs_ma20 > 10:
            score -= 0.3  # 20日均线乖离过大
        if premium_vs_ma60 > 25:
            score -= 0.3  # 60日均线乖离过大
    
    return max(-2, min(2, score))

def score_sector(sector_rank, sector_main_flow, sector_pct):
    """
    板块评分：-2 ~ +2
    """
    score = 0
    
    # 1. 行业排名
    if sector_rank is not None:
        if sector_rank <= 3:
            score += 1.0  # Top3
        elif sector_rank <= 10:
            score += 0.5
        elif sector_rank <= 20:
            score += 0.2
        elif sector_rank >= 50:
            score -= 0.5
    
    # 2. 板块主力净流入
    if sector_main_flow is not None:
        if sector_main_flow > 50000:
            score += 1.0  # 超5亿
        elif sector_main_flow > 10000:
            score += 0.5
        elif sector_main_flow > 0:
            score += 0.2
        elif sector_main_flow > -10000:
            score -= 0.2
        else:
            score -= 0.5
    
    # 3. 板块涨跌幅
    if sector_pct is not None:
        if sector_pct > 3:
            score += 0.3
        elif sector_pct < -2:
            score -= 0.3
    
    return max(-2, min(2, score))


# ==================== MAIN ====================

print("=" * 70)
print("  512480 半导体ETF国联安 — 轻量门禁分析")
print(f"  分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 70)

# Step 1: 获取行情数据
print("\n[1/4] 获取行情数据...")
# 先尝试东方财富，失败则用新浪
quote = fetch_quote_em("512480")
if not quote:
    from market_data import fetch_quote
    quote = fetch_quote("512480")

if quote:
    print(f"  ✅ 数据源: {quote.get('_source', 'unknown')}")
    print(f"  价格: {quote['price']:.3f}  |  涨跌幅: {quote['pct']:+.2f}%")
    print(f"  日内: 高{quote['high']:.3f} / 低{quote['low']:.3f} / 开{quote['open']:.3f}")
    print(f"  昨收: {quote['pre_close']:.3f}")
    print(f"  成交: {quote['vol']:.0f}手 / {quote['amount']:.0f}万")
    if 'turnover' in quote and quote['turnover'] > 0:
        print(f"  换手: {quote['turnover']:.2f}%")
else:
    print("  ❌ 行情获取失败！")
    sys.exit(1)

# Step 2: 获取板块数据
print("\n[2/4] 获取行业板块资金流向...")
sectors = fetch_sector_flow()
semiconductor_info = None
semiconductor_rank = None

if sectors:
    rank, info = find_semiconductor_sector(sectors)
    semiconductor_rank = rank
    semiconductor_info = info
    if info:
        print(f"  ✅ 半导体板块: 排名 #{rank}/{len(sectors)}")
        print(f"  涨跌幅: {info['pct']:+.2f}%")
        print(f"  主力净流入: {info['main_net_flow']:.0f}万")
    else:
        print(f"  ⚠️ 未找到半导体板块（共{len(sectors)}个板块）")
else:
    print(f"  ⚠️ 板块数据获取失败，使用默认值")

# 如果东方财富返回了资金流向数据，使用它
# 否则估算
main_net = quote.get('main_net_flow', 0)
super_large = quote.get('super_large_net', 0)
small_net = quote.get('small_net', 0)
amount = quote['amount']

# 如果没有资金流向字段（sina fallback），尝试估算
if abs(main_net) < 0.001 and abs(super_large) < 0.001:
    # 基于涨跌幅+成交额估算主力方向
    if quote['pct'] > 2:
        # 大涨，假设主力净流入约占成交额3-5%
        main_net = amount * 0.04
        super_large = main_net * 0.4
        small_net = -main_net * 0.3
    elif quote['pct'] < -2:
        main_net = -amount * 0.04
        super_large = main_net * 0.5
        small_net = -main_net * 0.3
    print(f"  ⚠️ 资金流向为估算值（数据源无此字段）")

print(f"\n  📊 资金流向（估算/实际）:")
print(f"  主力净流入: {main_net:+.0f}万 ({main_net/amount*100:+.1f}% 成交额)" if amount > 0 else f"  主力净流入: {main_net:+.0f}万")
print(f"  超大单净流入: {super_large:+.0f}万")
print(f"  散户(小单)净流入: {small_net:+.0f}万")

# Step 3: 三维评分
print("\n[3/4] 三维评分...")

flow_score = score_capital_flow(main_net, amount, super_large, small_net, quote['pct'])
tech_score = score_technical(
    quote['price'], quote['high'], quote['low'],
    quote['open'], quote['pre_close'], quote['pct'], quote['vol']
)
sector_score = score_sector(
    semiconductor_rank,
    semiconductor_info.get('main_net_flow') if semiconductor_info else None,
    semiconductor_info.get('pct') if semiconductor_info else None
)

avg_score = (flow_score + tech_score + sector_score) / 3

print(f"  资金流评分: {flow_score:+.1f}")
print(f"  技术面评分: {tech_score:+.1f}")
print(f"  板块评分:   {sector_score:+.1f}")
print(f"  ─────────────────")
print(f"  等权平均:   {avg_score:+.2f}")

# Step 4: 综合裁决
print("\n[4/4] 综合裁决...")

# 映射表
if avg_score >= 1.5:
    verdict = "STRONG_BUY"
elif avg_score >= 0.8:
    verdict = "BUY"
elif avg_score >= 0.3:
    verdict = "OVERWEIGHT"
elif avg_score >= -0.3:
    verdict = "HOLD"
elif avg_score >= -0.8:
    verdict = "UNDERWEIGHT"
else:
    verdict = "SELL"

print(f"\n  ╔════════════════════════════════╗")
print(f"  ║  综合裁决: {verdict:^16s} ║")
print(f"  ╚════════════════════════════════╝")

# 风险提示
print(f"\n  ⚠️ 风险提示:")
print(f"  • CVaR约束: CVaR(95%)=-5.07% < -5%, 禁止新开/加仓")
print(f"  • HMM状态: sideways/bear 53.7%")
print(f"  • 盘中急涨+3.55%, 已触及60日前高2.13")
print(f"  • 5/18 insight: 高开低走+大振幅, 关注假突破风险")
print(f"  • 5/18 CVRF: 振幅暴增模式(0.1%→4.9%)")
print(f"  • GARCH波动率: 43.1% (高位)")
print(f"  • 持仓: 2300股 @1.82, 市值{2300*quote['price']:.0f}元")
print(f"  • 30日涨幅+46.2%, 短期超买严重")

# 最终建议
print(f"\n{'='*70}")
print(f"  最终建议:")
if 'CVaR' in str(avg_score) or avg_score > 0:
    print(f"  虽然评分{avg_score:+.2f} → {verdict}，但CVaR约束禁止新开/加仓")
    print(f"  建议: 持有观望，不追高。关注前高2.13突破确认/假突破回踩")
    print(f"  如突破确认(HOLD→可考虑轻仓)，如假突破→UNDERWEIGHT/SELL")
else:
    print(f"  评分{avg_score:+.2f} → {verdict}")
print(f"{'='*70}")

# 输出 JSON
result = {
    "ticker": "512480",
    "name": "半导体ETF国联安",
    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "quote": {
        "price": quote['price'],
        "pct": quote['pct'],
        "high": quote['high'],
        "low": quote['low'],
        "open": quote['open'],
        "pre_close": quote['pre_close'],
        "vol": quote['vol'],
        "amount": quote['amount'],
        "turnover": quote.get('turnover', 0),
    },
    "capital_flow": {
        "main_net_flow_wan": main_net,
        "super_large_wan": super_large,
        "small_net_wan": small_net,
    },
    "sector": {
        "rank": semiconductor_rank,
        "name": semiconductor_info.get('name') if semiconductor_info else None,
        "pct": semiconductor_info.get('pct') if semiconductor_info else None,
        "main_net_flow_wan": semiconductor_info.get('main_net_flow') if semiconductor_info else None,
    },
    "scores": {
        "capital_flow": round(flow_score, 2),
        "technical": round(tech_score, 2),
        "sector": round(sector_score, 2),
        "average": round(avg_score, 2),
    },
    "verdict": verdict,
    "constraints": {
        "CVaR_95pct": "-5.07%",
        "CVaR_violation": True,
        "HMM_state": "sideways/bear 53.7%",
        "touching_60d_high": True,
        "CVRF_volatility_breakout": True,
    }
}

print(f"\n[JSON]")
print(json.dumps(result, ensure_ascii=False, indent=2))

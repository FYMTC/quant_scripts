#!/config/quant_env/bin/python3
"""
512480 半导体ETF 轻量门禁 — 收盘前分析 (14:55)
使用 urllib 避免 curl subprocess
"""
import sys, os, json, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

# Beijing timezone
CST = timezone(timedelta(hours=8))

def http_get_json(url, timeout=10):
    """Safe HTTP GET using urllib, returns parsed JSON or None."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except Exception as e:
        print(f"  [WARN] HTTP error for {url[:60]}: {e}")
        return None

def fetch_quote_512480():
    """Get 512480 quote + fund flow from eastmoney."""
    fields = "f43,f44,f45,f46,f47,f48,f57,f58,f60,f168,f170,f62,f184,f185,f186,f187"
    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid=1.512480&fields={fields}"
    d = http_get_json(url)
    if not d or d.get("rc") != 0 or not d.get("data"):
        return None
    
    rd = d["data"]
    div = 1000
    result = {
        "code": "512480",
        "name": rd.get("f58", ""),
        "price": int(rd.get("f43") or 0) / div,
        "pct": float(rd.get("f170") or 0) / 100,
        "high": int(rd.get("f44") or 0) / div,
        "low": int(rd.get("f45") or 0) / div,
        "open": int(rd.get("f46") or 0) / div,
        "pre_close": int(rd.get("f60") or 0) / div,
        "vol": int(rd.get("f47") or 0),
        "amount": int(rd.get("f48") or 0) / 10000,  # 万元
        "turnover": float(rd.get("f168") or 0) / 100,
        "main_net_flow": float(rd.get("f62") or 0) / 10000,
        "super_large_net": float(rd.get("f184") or 0) / 10000,
        "large_net": float(rd.get("f185") or 0) / 10000,
        "medium_net": float(rd.get("f186") or 0) / 10000,
        "small_net": float(rd.get("f187") or 0) / 10000,
        "_source": "eastmoney",
    }
    return result

def fetch_sector_ranking():
    """Get sector fund flow ranking."""
    url = "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f62&fs=m:90+t2&fields=f12,f14,f2,f3,f62,f184"
    d = http_get_json(url)
    if not d or not d.get("data") or not d["data"].get("diff"):
        return None
    
    sectors = []
    for item in d["data"]["diff"]:
        sectors.append({
            "code": item.get("f12", ""),
            "name": item.get("f14", ""),
            "pct": float(item.get("f3") or 0),
            "main_net_flow": float(item.get("f62") or 0) / 10000,
        })
    return sectors

def find_semiconductor(sectors):
    for i, s in enumerate(sectors):
        if "半导体" in s.get("name", ""):
            return i + 1, s
    return None, None

# Given intraday context from task
INTRADAY = {
    "price": 2.20,
    "open": 2.27,
    "high": 2.30,
    "low": 2.20,
    "pre_close": 2.23,
    "pct": -0.99,
    "pct_vs_open": -2.69,
    "amplitude_pct": 4.4,
    "direction": "REVERSAL_DOWN",
    "volatility_surge": True,
    "ma5": 2.10,
    "ma20": 1.89,
    "ma60": 1.66,
    "ma_state": "多头",
    "cvar_95": -5.07,
    "high_60d": 2.23,
    "momentum_20d": 45.6,
    "hmm": "sideways (熊58% 震42%)",
    "pnl": 885.5,
    "pnl_pct": 21.15,
    "shares": 2300,
    "cost": 1.82,
    "signal_context": "rapid_drop_bounce → actual: high-open reversal",
}

CONSTRAINTS = {
    "cvar_block": True,
    "cvar_value": -5.07,
    "cvar_desc": "CVaR -5.07% 劣于 -5% → 禁止新开/加仓",
    "reversal_block": True,
    "reversal_desc": "高开后转跌，禁止买入",
    "event_risk": "WATCH",
    "event_desc": "CAUTION override, buy_score_threshold=1.0",
}

# ===== MAIN =====
print("=" * 70)
print("  512480 半导体ETF国联安 — 轻量门禁 (收盘前 14:55)")
print(f"  分析时间: {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')} CST")
print("=" * 70)

# Step 1: Fetch live data
print("\n[1/3] 获取实时行情 + 资金流...")
quote = fetch_quote_512480()
if quote:
    print(f"  ✅ 数据源: eastmoney API")
    print(f"  实时价: ¥{quote['price']:.3f}  涨跌: {quote['pct']:+.2f}%")
    print(f"  开盘: {quote['open']:.3f}  最高: {quote['high']:.3f}  最低: {quote['low']:.3f}")
    print(f"  昨收: {quote['pre_close']:.3f}")
    print(f"  成交额: {quote['amount']:.0f}万  成交量: {quote['vol']:.0f}手")
    print(f"  主力净流入: {quote['main_net_flow']:+.0f}万")
    print(f"  超大单: {quote['super_large_net']:+.0f}万  大单: {quote['large_net']:+.0f}万")
    print(f"  中单: {quote['medium_net']:+.0f}万  小单: {quote['small_net']:+.0f}万")
else:
    print("  ⚠️ 实时数据获取失败，使用盘中上下文数据")
    quote = None

# Step 2: Sector ranking
print("\n[2/3] 获取半导体板块排名...")
sectors = fetch_sector_ranking()
semiconductor_rank = None
semiconductor_info = None

if sectors:
    rank, info = find_semiconductor(sectors)
    semiconductor_rank = rank
    semiconductor_info = info
    if info:
        print(f"  ✅ 半导体板块: 排名 #{rank}/{len(sectors)}")
        print(f"  涨跌幅: {info['pct']:+.2f}%")
        print(f"  主力净流入: {info['main_net_flow']:.0f}万")
    else:
        print(f"  ⚠️ 未找到半导体板块（共{len(sectors)}个板块）")
else:
    print(f"  ⚠️ 板块数据获取失败")

# Step 3: Three-dimensional scoring
print("\n[3/3] 三维评分...")
print()

# ============================================================
# 资金流评分
# ============================================================
flow_score = 0.0
flow_breakdown = []

if quote:
    main_net = quote['main_net_flow']  # 万元
    amount = quote['amount']  # 万元
    super_large = quote['super_large_net']
    large = quote['large_net']
    small = quote['small_net']
    medium = quote['medium_net']
    pct = quote['pct']
    
    # 1. 主力净流入/成交额比
    if amount > 0:
        main_ratio = main_net / amount
        flow_breakdown.append(f"主力/成交额比: {main_ratio:+.1%}")
        if main_ratio > 0.15:
            flow_score += 1.0
            flow_breakdown.append("→ +1.0 (强流入 >15%)")
        elif main_ratio > 0.05:
            flow_score += 0.5
            flow_breakdown.append("→ +0.5 (流入 5-15%)")
        elif main_ratio > 0:
            flow_score += 0.2
            flow_breakdown.append("→ +0.2 (小幅流入)")
        elif main_ratio > -0.05:
            flow_score -= 0.2
            flow_breakdown.append("→ -0.2 (小幅流出)")
        elif main_ratio > -0.15:
            flow_score -= 0.5
            flow_breakdown.append("→ -0.5 (流出 5-15%)")
        else:
            flow_score -= 1.0
            flow_breakdown.append("→ -1.0 (强流出 >15%)")
    
    # 2. 超大单 vs 大单 机构行为
    total_inst = super_large + large
    flow_breakdown.append(f"机构(超大+大单): {total_inst:+.0f}万")
    if main_net != 0:
        if main_net > 0 and total_inst > 0:
            inst_weight = total_inst / (abs(main_net) + 0.01)
            if inst_weight > 0.5:
                flow_score += 0.5
                flow_breakdown.append("→ +0.5 (机构主导流入)")
            else:
                flow_score += 0.2
                flow_breakdown.append("→ +0.2 (机构参与流入)")
        elif main_net < 0 and total_inst < 0:
            inst_weight = abs(total_inst) / (abs(main_net) + 0.01)
            if inst_weight > 0.5:
                flow_score -= 0.5
                flow_breakdown.append("→ -0.5 (机构主导流出)")
            else:
                flow_score -= 0.2
                flow_breakdown.append("→ -0.2 (机构参与流出)")
    
    # 3. 散户逆向指标
    if small < 0 and main_net > 0:
        flow_score += 0.3
        flow_breakdown.append("→ +0.3 (主力买散户卖=好)")
    elif small > 0 and main_net < 0:
        flow_score -= 0.3
        flow_breakdown.append("→ -0.3 (主力卖散户买=差)")
    
    # 4. 价格确认
    if pct > 2:
        flow_score += 0.2
    elif pct < -2:
        flow_score -= 0.2
        flow_breakdown.append(f"→ {'+0.2' if pct>2 else '-0.2'} (价格确认)")
else:
    # Fallback: use morning data + intraday reversal context
    # Morning: main net -12.3M in first 2 min, divergence
    # Now at close: high-open reversal, price dropped from 2.27→2.20
    # Likely main outflow accelerated during the day
    flow_breakdown.append("⚠️ 实时数据不可用，基于盘中形态推演")
    flow_breakdown.append("上午高开2.27→午后回落到2.20，高开低走形态")
    flow_breakdown.append("推测: 主力午后加速流出，全天净流出")
    flow_score -= 0.5  # Conservative: assume outflow
    flow_breakdown.append("→ -0.5 (推测净流出)")

flow_score = max(-2, min(2, flow_score))
print(f"  💰 资金流评分: {flow_score:+.1f}")
for b in flow_breakdown:
    print(f"     {b}")

# ============================================================
# 技术面评分
# ============================================================
tech_score = 0.0
tech_breakdown = []

# Use provided intraday data (more reliable at closing)
price = INTRADAY['price']
open_p = INTRADAY['open']
high = INTRADAY['high']
low = INTRADAY['low']
pct = INTRADAY['pct']
ma5 = INTRADAY['ma5']
ma20 = INTRADAY['ma20']
ma60 = INTRADAY['ma60']
high_60d = INTRADAY['high_60d']

# 1. 日内位置
day_range = high - low
if day_range > 0:
    pos = (price - low) / day_range
    tech_breakdown.append(f"日内位置: {pos:.0%} (low={low} high={high} price={price})")
    if pos > 0.8:
        tech_score += 0.3
        tech_breakdown.append("→ +0.3 (接近日内高点)")
    elif pos < 0.2:
        tech_score -= 0.3
        tech_breakdown.append("→ -0.3 (接近日内低点)")
    else:
        tech_breakdown.append("→ 0 (中间位置)")

# 2. 实体 vs 影线 (REVERSAL_DOWN)
body_pct = abs(price - open_p) / open_p * 100  # ≈2.69%
if price < open_p:
    # Red candle (阴线)
    if body_pct > 2:
        tech_score -= 0.5
        tech_breakdown.append(f"→ -0.5 (大阴线, 实体{body_pct:.1f}%)")
    elif body_pct > 1:
        tech_score -= 0.3
        tech_breakdown.append(f"→ -0.3 (中阴线)")
else:
    if body_pct > 2:
        tech_score += 0.5

# 3. 高开低走形态 (REVERSAL specific penalty)
upper_wick = high - max(open_p, price)
if open_p > INTRADAY['pre_close'] and price < open_p and pct < 0:
    # High open, lower close, negative day = reversal pattern
    tech_score -= 0.5
    tech_breakdown.append(f"→ -0.5 (高开低走反转形态: 开{open_p}→收{price})")

# 4. 振幅暴增
if INTRADAY['volatility_surge']:
    tech_score -= 0.3
    tech_breakdown.append(f"→ -0.3 (振幅暴增 0.7%→4.4%, 异常波动)")

# 5. 距60日前高
if high > high_60d and price < high_60d:
    tech_score -= 0.5
    tech_breakdown.append(f"→ -0.5 (盘中突破60日前高{high_60d}但回落, 假突破确认)")
elif price > high_60d:
    tech_score += 0.5
    tech_breakdown.append(f"→ +0.5 (收盘站上60日前高{high_60d})")

# 6. 均线乖离
premium_ma20 = (price - ma20) / ma20 * 100
premium_ma60 = (price - ma60) / ma60 * 100
tech_breakdown.append(f"MA20乖离: {premium_ma20:+.1f}%  MA60乖离: {premium_ma60:+.1f}%")

if premium_ma20 > 10:
    tech_score -= 0.3
    tech_breakdown.append("→ -0.3 (MA20乖离>10%)")
if premium_ma60 > 25:
    tech_score -= 0.3
    tech_breakdown.append("→ -0.3 (MA60乖离>25%)")

# 7. MA多头排列 (still bullish overall)
if ma5 > ma20 > ma60:
    tech_score += 0.2
    tech_breakdown.append("→ +0.2 (MA多头排列)")

# 8. 20日动量过热
if INTRADAY['momentum_20d'] > 40:
    tech_score -= 0.3
    tech_breakdown.append(f"→ -0.3 (20日动量{INTRADAY['momentum_20d']}%过热)")

# 9. HMM risk
tech_score -= 0.2
tech_breakdown.append(f"→ -0.2 (HMM: {INTRADAY['hmm']})")

tech_score = max(-2, min(2, tech_score))
print(f"\n  📈 技术面评分: {tech_score:+.1f}")
for b in tech_breakdown:
    print(f"     {b}")

# ============================================================
# 板块评分
# ============================================================
sector_score = 0.0
sector_breakdown = []

if semiconductor_rank is not None:
    if semiconductor_rank <= 3:
        sector_score += 1.0
        sector_breakdown.append(f"→ +1.0 (排名 #{semiconductor_rank}, Top 3)")
    elif semiconductor_rank <= 10:
        sector_score += 0.5
        sector_breakdown.append(f"→ +0.5 (排名 #{semiconductor_rank}, Top 10)")
    elif semiconductor_rank <= 20:
        sector_score += 0.2
        sector_breakdown.append(f"→ +0.2 (排名 #{semiconductor_rank})")
    elif semiconductor_rank >= 50:
        sector_score -= 0.5
        sector_breakdown.append(f"→ -0.5 (排名 #{semiconductor_rank}, 后50%)")
    else:
        sector_breakdown.append(f"排名 #{semiconductor_rank}, 中性")

if semiconductor_info:
    mf = semiconductor_info['main_net_flow']
    sector_breakdown.append(f"板块主力净流入: {mf:+.0f}万")
    if mf > 50000:
        sector_score += 1.0
        sector_breakdown.append("→ +1.0 (>5亿)")
    elif mf > 10000:
        sector_score += 0.5
        sector_breakdown.append("→ +0.5 (1-5亿)")
    elif mf > 0:
        sector_score += 0.2
        sector_breakdown.append("→ +0.2 (正流入)")
    elif mf > -10000:
        sector_score -= 0.2
        sector_breakdown.append("→ -0.2 (小幅流出)")
    else:
        sector_score -= 0.5
        sector_breakdown.append("→ -0.5 (大幅流出)")

    pct_s = semiconductor_info['pct']
    sector_breakdown.append(f"板块涨跌: {pct_s:+.2f}%")
    if pct_s > 3:
        sector_score += 0.3
    elif pct_s < -2:
        sector_score -= 0.3
else:
    # Fallback: 半导体板块仍是近期主线
    sector_breakdown.append("⚠️ 无实时板块数据，使用定性判断")
    sector_breakdown.append("半导体板块近30日+45-57%动量，市场主线")
    sector_score += 0.5
    sector_breakdown.append("→ +0.5 (定性：主线板块)")

sector_score = max(-2, min(2, sector_score))
print(f"\n  🏭 板块评分: {sector_score:+.1f}")
for b in sector_breakdown:
    print(f"     {b}")

# ============================================================
# Composite
# ============================================================
composite = (flow_score + tech_score + sector_score) / 3

print(f"\n{'='*60}")
print(f"  三维综合评分")
print(f"{'='*60}")
print(f"  资金流: {flow_score:+.1f}")
print(f"  技术面: {tech_score:+.1f}")
print(f"  板块:   {sector_score:+.1f}")
print(f"  ─────────────")
print(f"  等权平均: {composite:+.2f}")
print()

# Action mapping
if composite >= 1.5:
    raw_verdict = "STRONG_BUY"
elif composite >= 0.8:
    raw_verdict = "BUY"
elif composite >= 0.3:
    raw_verdict = "OVERWEIGHT"
elif composite >= -0.3:
    raw_verdict = "HOLD"
elif composite >= -0.8:
    raw_verdict = "UNDERWEIGHT"
else:
    raw_verdict = "SELL"

print(f"  Raw verdict (基于评分): {raw_verdict}")

# ============================================================
# Constraint checking
# ============================================================
print(f"\n{'='*60}")
print(f"  约束条件检查")
print(f"{'='*60}")

effective_verdict = raw_verdict
vetoes = []
warnings = []

# CVaR block
cvar_violated = CONSTRAINTS['cvar_block']
print(f"  🔴 CVaR(95%): {CONSTRAINTS['cvar_value']}% (< -5%阈值)")
print(f"     → 禁止新开/加仓")
if cvar_violated and raw_verdict in ("STRONG_BUY", "BUY", "OVERWEIGHT"):
    vetoes.append("CVaR -5.07% → 升级为最高HOLD")
    effective_verdict = "HOLD"

# Reversal block
print(f"  🔴 日内反转: 高开{open_p}→回落{price} ({INTRADAY['pct_vs_open']:+.1f}% vs open)")
print(f"     → 禁止买入")
if raw_verdict in ("STRONG_BUY", "BUY", "OVERWEIGHT"):
    vetoes.append("反转形态 → 买入信号无效")
    if effective_verdict not in ("HOLD", "UNDERWEIGHT", "SELL"):
        effective_verdict = "HOLD"

# Event risk CAUTION
print(f"  🟡 事件风险: {CONSTRAINTS['event_risk']} (CAUTION)")
print(f"     → buy_score_threshold = 1.0 (当前{composite:+.2f})")
if CONSTRAINTS['event_risk'] == "WATCH" and composite < 1.0:
    warnings.append(f"CAUTION模式: 评分{composite:+.2f} < 1.0阈值，买入需更高确认")
    if raw_verdict in ("BUY", "OVERWEIGHT"):
        if effective_verdict not in ("HOLD", "UNDERWEIGHT", "SELL"):
            effective_verdict = "HOLD"

# Existing sell orders
print(f"\n  📋 已有卖出挂单:")
print(f"     • 500股 @2.21 (pending)")
print(f"     • 2300股 @2.144 (pending)")

# Position context
print(f"\n  📊 持仓: {INTRADAY['shares']}股 @成本{INTRADAY['cost']}")
print(f"     浮动盈亏: +{INTRADAY['pnl']}元 (+{INTRADAY['pnl_pct']}%)")
print(f"     当前市值: {INTRADAY['shares'] * INTRADAY['price']:.0f}元")

# Signal mismatch
print(f"\n  ⚠️ 信号上下文不匹配:")
print(f"     rapid_drop_bounce预期: 跌后反弹")
print(f"     实际日内: 高开低走反转")
print(f"     → 信号逻辑与实际走势矛盾，置信度降低")

# ============================================================
# Final Verdict
# ============================================================
print(f"\n{'='*60}")
print(f"  🏁 最终裁决")
print(f"{'='*60}")

# With all constraints, the verdict is HOLD at best
# But we need to consider: price has already dropped below the sell limits
# 500 shares at 2.21 — price is 2.20, this could fill soon
# 2300 shares at 2.144 — price is above, unlikely to fill without further drop

# Given CVaR block + reversal block + CAUTION + existing sell orders
# The only reasonable action is HOLD or potentially lower sell prices

if composite >= 0.3:
    # Scores say overweight but constraints block
    effective_verdict = "HOLD"
    reasoning = (
        f"评分{composite:+.2f}支持OVERWEIGHT，但被多重约束否决:\n"
        f"  1. CVaR -5.07% (< -5%) 禁止新开/加仓\n"
        f"  2. 高开低走反转 禁止买入\n"
        f"  3. CAUTION事件风险 需>1.0评分\n"
        f"  → 维持HOLD，等待卖出挂单成交\n"
        f"  如2.21卖出500股 → 剩余1800股继续持有\n"
        f"  如2.144卖出2300股 → 清仓获利+885元(+21.15%)"
    )
elif composite >= -0.3:
    effective_verdict = "HOLD"
    reasoning = (
        f"评分{composite:+.2f}中性:\n"
        f"  • 反转形态+CVaR超标+振幅异常 → 不宜操作\n"
        f"  • 已有卖出挂单在2.21和2.144\n"
        f"  • 建议：持仓等待挂单成交，不追卖"
    )
elif composite >= -0.8:
    effective_verdict = "UNDERWEIGHT"
    reasoning = (
        f"评分{composite:+.2f}偏弱:\n"
        f"  • 技术面恶化（反转+假突破+振幅暴增）\n"
        f"  • 但MA多头排列+板块主线提供支撑\n"
        f"  • 建议：降低2.144卖出价至2.15-2.18区间加快成交\n"
        f"  • 或市价卖出500股（当前2.20），留1800股底仓"
    )
else:
    effective_verdict = "SELL"
    reasoning = (
        f"评分{composite:+.2f}明确看空:\n"
        f"  • 资金流出+技术破位+板块转弱 三重共振\n"
        f"  • 建议市价清仓，锁定+21%利润"
    )

for v in vetoes:
    print(f"  🛡️ {v}")
for w in warnings:
    print(f"  ⚠️ {w}")

print(f"\n  ╔══════════════════════════════════════╗")
print(f"  ║  EFFECTIVE VERDICT: {effective_verdict:^16s} ║")
print(f"  ╚══════════════════════════════════════╝")

print(f"\n  {reasoning}")

# JSON output
output = {
    "light_gate_close": {
        "ticker": "512480",
        "name": "半导体ETF国联安",
        "timestamp": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S") + " CST",
        "rule": "ETF轻量门禁 (收盘前14:55)",
        "input": {
            "price": INTRADAY['price'],
            "open": INTRADAY['open'],
            "high": INTRADAY['high'],
            "low": INTRADAY['low'],
            "pre_close": INTRADAY['pre_close'],
            "pct": INTRADAY['pct'],
            "pct_vs_open": INTRADAY['pct_vs_open'],
            "amplitude_pct": INTRADAY['amplitude_pct'],
            "direction": INTRADAY['direction'],
            "ma5": ma5, "ma20": ma20, "ma60": ma60,
            "high_60d": high_60d,
            "momentum_20d": INTRADAY['momentum_20d'],
            "hmm": INTRADAY['hmm'],
        },
        "live_data": {
            "source": "eastmoney" if quote else "intraday_context",
            "quote": {
                "price": quote['price'] if quote else INTRADAY['price'],
                "pct": quote['pct'] if quote else INTRADAY['pct'],
                "amount": quote['amount'] if quote else None,
            } if quote else {"price": INTRADAY['price'], "pct": INTRADAY['pct']},
            "fund_flow": {
                "main_net_wan": quote['main_net_flow'] if quote else None,
                "super_large_wan": quote['super_large_net'] if quote else None,
                "large_wan": quote['large_net'] if quote else None,
                "medium_wan": quote['medium_net'] if quote else None,
                "small_wan": quote['small_net'] if quote else None,
            } if quote else None,
        },
        "sector": {
            "rank": semiconductor_rank,
            "name": semiconductor_info['name'] if semiconductor_info else "半导体",
            "pct": semiconductor_info['pct'] if semiconductor_info else None,
            "main_net_flow_wan": semiconductor_info['main_net_flow'] if semiconductor_info else None,
        },
        "scores": {
            "fund_flow": round(flow_score, 2),
            "technical": round(tech_score, 2),
            "sector": round(sector_score, 2),
            "composite": round(composite, 2),
        },
        "raw_verdict": raw_verdict,
        "effective_verdict": effective_verdict,
        "constraints": {
            "cvar_block": True,
            "cvar_value": -5.07,
            "reversal_block": True,
            "event_risk": CONSTRAINTS['event_risk'],
            "buy_score_threshold": 1.0,
            "existing_sell_orders": [
                {"shares": 500, "price": 2.21, "status": "pending"},
                {"shares": 2300, "price": 2.144, "status": "pending"},
            ],
        },
        "position": {
            "shares": INTRADAY['shares'],
            "cost_basis": INTRADAY['cost'],
            "unrealized_pnl": INTRADAY['pnl'],
            "unrealized_pnl_pct": INTRADAY['pnl_pct'],
        },
        "reasoning": reasoning,
        "scenario_outlook": {
            "sell_fill_221": "500股@2.21成交 → +195元, 剩余1800股继续观察",
            "sell_fill_2144": "2300股@2.144成交 → 清仓+745元, 总利润+885元(+21.15%)",
            "market_drop_below_2144": "跌破2.144 → 全仓卖出成交, 锁定利润",
            "rebound_above_223": "回升至2.23以上 → 考虑撤2.144卖单, 保留2.21卖单, HOLD观望",
        },
    }
}

print(f"\n[JSON_OUTPUT]")
print(json.dumps(output, ensure_ascii=False, indent=2))

#!/config/quant_env/bin/python3
"""
📊 实盘信号输出 — RL模型 + RD因子 + 技术指标 三位一体
每日交易时段调用，输出持仓股的买卖信号

用法:
  实时信号:  python3 live_signals.py
  自定义:    python3 live_signals.py --codes 002594 518880 --model ./models/sb3_ppo_rdfactors_final.zip
"""
import sys, os, json, warnings, argparse
import numpy as np
import pandas as pd
warnings.filterwarnings("ignore")

os.chdir('/config/quant_scripts')
sys.path.insert(0, '.')

# ===== 配置（与smart_guard保持一致）=====
POSITIONS = {
    '002594': {'name': '比亚迪', 'shares': 100, 'cost': 99.531},
    '518880': {'name': '黄金ETF华安', 'shares': 300, 'cost': 10.131},
}
WATCHLIST = [
    '002594', '518880', '600522', '600487', '600105', '002415', '600150',
    '300589', '600711', '002466', '002297', '002300', '002560', '920111',
    '688015', '688616', '600900', '603778', '588730', '002475', '000938', '512480'
]

def get_live_prices(codes):
    """获取实时行情（腾讯API，免限流）"""
    import urllib.request
    
    # 构建腾讯API code格式（前缀决定市场）
    qs_list = []
    for c in codes:
        if c.startswith('5') or c.startswith('6'):
            qs_list.append(f'sh{c}')
        elif c.startswith('0') or c.startswith('3'):
            qs_list.append(f'sz{c}')
        elif c.startswith('9'):
            qs_list.append(f'bj{c}')
        else:
            continue
    
    # 腾讯API每次最多15只，但实测20只也可以
    url = f'https://qt.gtimg.cn/q={",".join(qs_list)}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    results = {}
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode('gbk')
        for line in raw.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split('~')
            if len(parts) > 34:
                code = parts[2]  # 腾讯返回纯代码（如002594），用代码字典匹配
                name = parts[1]
                price = float(parts[3]) if parts[3] else 0
                results[code] = {
                    'name': name,
                    'price': price,
                    'change_pct': float(parts[32]) if parts[32] else 0,
                    'change_amt': float(parts[31]) if parts[31] else 0,
                    'open': float(parts[5]) if parts[5] else 0,
                    'high': float(parts[33]) if parts[33] else 0,
                    'low': float(parts[34]) if parts[34] else 0,
                    'volume': float(parts[6]) / 100 if parts[6] else 0,
                    'amount': float(parts[37]) / 10000 if parts[37] else 0,
                }
    except Exception as e:
        print(f"  ⚠️ 腾讯API查询失败: {str(e)[:60]}")
    
    return results


def compute_rd_scores(code, df):
    """计算当前RD因子分数（归一化到0-1）"""
    if df is None or len(df) < 30:
        return {}
    
    close = df['close'].values
    high = df['high'].values
    low = df['low'].values
    volume = df['volume'].values
    
    scores = {}
    
    # RD_Ret5_Range10
    if len(close) > 15:
        ret5 = close[-1] / close[-6] - 1 if close[-6] > 0 else 0
        range10 = (max(high[-10:]) - min(low[-10:])) / close[-1] if close[-1] > 0 else 0
        scores['rd_ret5_range10'] = ret5 + range10
    
    # RD_MADev5_MADev20
    if len(close) > 20:
        ma5 = np.mean(close[-5:])
        ma20 = np.mean(close[-20:])
        dev5 = (close[-1] - ma5) / ma5 if ma5 > 0 else 0
        dev20 = (close[-1] - ma20) / ma20 if ma20 > 0 else 0
        scores['rd_madev5_madev20'] = dev5 - dev20
    
    # RD_Divergence
    if len(close) > 5:
        vol_ma5 = np.mean(volume[-5:])
        vol_ratio = volume[-1] / vol_ma5 if vol_ma5 > 0 else 1
        scores['rd_divergence'] = ret5 - vol_ratio * 0.5
    
    # RD_Momentum_Accel
    if len(close) > 20:
        ret20 = close[-1] / close[-21] - 1 if close[-21] > 0 else 0
        scores['rd_momentum_accel'] = ret5 - ret20 * 0.3
    
    return scores


def rd_signal_from_scores(scores):
    """从RD因子分数生成信号"""
    if not scores:
        return "中性", 0.5
    
    # 各因子权重
    weights = {
        'rd_ret5_range10': 0.35,
        'rd_madev5_madev20': 0.25,
        'rd_divergence': 0.20,
        'rd_momentum_accel': 0.20,
    }
    
    weighted = 0
    total_w = 0
    for k, w in weights.items():
        if k in scores:
            # 归一化到[-1, 1]
            v = np.clip(scores[k] / 5, -1, 1)
            weighted += v * w
            total_w += w
    
    if total_w > 0:
        composite = weighted / total_w
    else:
        composite = 0
    
    # 映射到信号
    if composite > 0.15:
        signal = "📈 看多"
        strength = min(1.0, 0.5 + composite)
    elif composite < -0.15:
        signal = "📉 看空"
        strength = max(0.0, 0.5 + composite)
    else:
        signal = "➡️ 中性"
        strength = 0.5
    
    return signal, round(composite, 4)


def technical_signal(code, prices_30d=None):
    """技术面信号（MA趋势+MACD+RVI）"""
    if prices_30d is None or len(prices_30d) < 20:
        return "中性", 0.5
    
    close = prices_30d
    ma5 = np.mean(close[-5:])
    ma10 = np.mean(close[-10:])
    ma20 = np.mean(close[-20:])
    
    # 均线排列
    trend = 0
    if ma5 > ma10 > ma20:
        trend = 1  # 多头
    elif ma5 < ma10 < ma20:
        trend = -1  # 空头
    
    # 价格相对20日线位置
    pos = (close[-1] - ma20) / ma20 if ma20 > 0 else 0
    
    # RSI近似
    gains = sum(d for d in np.diff(close[-15:]) if d > 0)
    losses = abs(sum(d for d in np.diff(close[-15:]) if d < 0))
    rsi = 50
    if gains + losses > 0:
        rsi = 50 + (gains - losses) / (gains + losses) * 50
    
    composite = trend * 0.4 + np.clip(pos * 2, -1, 1) * 0.3 + (50 - rsi) / 100 * 0.3
    
    if composite > 0.2:
        signal = "📈 看多"
    elif composite < -0.2:
        signal = "📉 看空"
    else:
        signal = "➡️ 中性"
    
    return signal, round(composite, 3)


def load_rl_model(model_path):
    """加载训练好的RL模型"""
    from stable_baselines3 import PPO
    
    if not os.path.exists(model_path):
        # 尝试找best model
        best_path = model_path.replace('_final.zip', '/best_model.zip')
        if os.path.exists(best_path):
            model_path = best_path
        else:
            return None
    
    try:
        model = PPO.load(model_path, device='cpu')
        return model
    except Exception as e:
        print(f"  ⚠️ 模型加载失败: {str(e)[:60]}")
        return None


def live_signal_report(codes=None, model_path='./models/sb3_ppo_rdfactors_final.zip'):
    """生成实盘信号报告"""
    if codes is None:
        codes = WATCHLIST
    
    print(f"\n{'='*60}")
    print(f"  📊 实盘信号报告 | {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    
    # 1. 获取实时行情
    print("\n📡 获取实时行情...")
    prices = get_live_prices(codes)
    print(f"  获取到 {len(prices)} 只行情")
    
    # 2. 获取K线数据（用于RD因子计算）
    # 改用本地Qlib CSV作为K线来源（最稳定，无需API调用）
    print("\n📦 加载K线数据（本地缓存）...")
    kline_cache = {}
    qlib_dir = '/config/qlib_data/features'
    for code in codes:
        csv_path = os.path.join(qlib_dir, f"{code}.csv")
        if os.path.exists(csv_path):
            try:
                kdf = pd.read_csv(csv_path)
                kdf['date'] = pd.to_datetime(kdf['date'].astype(str), errors='coerce')
                kdf = kdf.dropna(subset=['date']).sort_values('date')
                if len(kdf) > 20:
                    kline_cache[code] = kdf
            except:
                pass
    
    # 3. 加载RL模型
    model = load_rl_model(model_path)
    if model:
        print(f"  ✅ RL模型已加载: {model.__class__.__name__}")
    else:
        print(f"  ⚠️ RL模型未加载（无预训练模型），仅输出RD因子+技术信号")
    
    # ===== 持仓分析 =====
    print(f"\n{'='*60}")
    print(f"  📋 持仓分析")
    print(f"{'='*60}")
    
    for code, pos in POSITIONS.items():
        if code not in prices:
            continue
        
        p = prices[code]
        cost = pos['cost']
        shares = pos['shares']
        market_val = shares * p['price']
        cost_val = shares * cost
        profit = market_val - cost_val
        profit_pct = (p['price'] / cost - 1) * 100 if cost > 0 else 0
        
        print(f"\n  {pos['name']}({code})")
        print(f"  现价: {p['price']:.3f} | 成本: {cost:.3f} | 盈亏: {profit:+.2f} ({profit_pct:+.2f}%)")
        print(f"  持仓: {shares}股 | 市值: {market_val:.2f} | 仓位: {market_val/max(market_val+cost_val, 1)*100:.1f}%")
        print(f"  日内: {p['change_pct']:+.2f}% | 最高: {p['high']:.3f} | 最低: {p['low']:.3f}")
        
        # RD因子信号
        klines = kline_cache.get(code)
        rd_scores = compute_rd_scores(code, klines)
        rd_sig, rd_strength = rd_signal_from_scores(rd_scores)
        
        # 技术信号
        close_prices = klines['close'].values if klines is not None and 'close' in klines.columns else None
        tech_sig, tech_strength = technical_signal(code, close_prices)
        
        print(f"  🧬 RD因子: {rd_sig} (强度={rd_strength:+.3f})")
        print(f"  📉 技术面: {tech_sig} (强度={tech_strength:+.3f})")
        
        # 综合建议
        composite = rd_strength * 0.5 + tech_strength * 0.5
        if composite > 0.2:
            action = "✅ **建议加仓/买入**"
        elif composite < -0.2:
            action = "🔴 **建议减仓/卖出**"
        else:
            action = "➡️ **建议持有**"
        
        print(f"  🎯 综合: {action} (评分={composite:+.3f})")
    
    # ===== 自选股雷达 =====
    print(f"\n{'='*60}")
    print(f"  📋 自选股雷达")
    print(f"{'='*60}")
    
    # 按涨跌幅排序
    gainers = [(code, p) for code, p in prices.items() if p['change_pct'] >= 0]
    losers = [(code, p) for code, p in prices.items() if p['change_pct'] < 0]
    gainers.sort(key=lambda x: x[1]['change_pct'], reverse=True)
    losers.sort(key=lambda x: x[1]['change_pct'])
    
    print(f"\n  ▶️ 涨幅榜（Top 5）")
    for code, p in gainers[:5]:
        name = POSITIONS.get(code, {}).get('name', p['name'])
        print(f"  {name}({code}): {p['change_pct']:+.2f}% → {p['price']:.2f}")
    
    if losers:
        print(f"\n  ◀️ 跌幅榜（Top 5）")
        for code, p in losers[:5]:
            name = POSITIONS.get(code, {}).get('name', p['name'])
            print(f"  {name}({code}): {p['change_pct']:+.2f}% → {p['price']:.2f}")
    else:
        print(f"\n  ◀️ 跌幅榜 — 无下跌股票 📈")
    
    # ===== 操作建议汇总 =====
    print(f"\n{'='*60}")
    print(f"  🎯 今日操盘建议")
    print(f"{'='*60}")
    
    for code, pos in POSITIONS.items():
        if code not in prices:
            continue
        p = prices[code]
        shares = pos['shares']
        klines = kline_cache.get(code)
        rd_scores = compute_rd_scores(code, klines)
        _, rd_strength = rd_signal_from_scores(rd_scores)
        close_prices = klines['close'].values if klines is not None and 'close' in klines.columns else None
        _, tech_strength = technical_signal(code, close_prices)
        composite = rd_strength * 0.5 + tech_strength * 0.5
        
        if composite > 0.2:
            if p['price'] < pos['cost']:
                shortfall = int(max(1, (pos['cost'] - p['price']) / p['price'] * 100 / shares))
                print(f"  {pos['name']}: 成本价下方 → ✅ 信号看多，可加仓 {shortfall}手")
            else:
                print(f"  {pos['name']}: ✅ 持有")
        elif composite < -0.2:
            print(f"  {pos['name']}: 🔴 信号看空，考虑减仓")
        else:
            print(f"  {pos['name']}: ➡️ 观望")
    
    print(f"\n{'='*60}")
    print(f"  📌 说明")
    print(f"  信号来源: RD因子(50%) + 技术面(50%)")
    print(f"  建议仅供参考，不构成投资建议。")
    print(f"{'='*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='A股实盘信号')
    parser.add_argument('--codes', nargs='+', help='指定股票代码')
    parser.add_argument('--model', default='./models/sb3_ppo_rdfactors_final.zip', help='RL模型路径')
    args = parser.parse_args()
    
    live_signal_report(args.codes, args.model)

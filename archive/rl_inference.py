#!/config/quant_env/bin/python3
"""
🎯 PPO模型盘中实时推理 — 构建当前市场状态 → 输出买卖信号
用于live_signals.py和signal_push.py的RL信号注入

用法:
  from rl_inference import get_rl_signal
  signal = get_rl_signal()  # {'002594': float, '518880': float}
"""
import sys, os, json, warnings
import numpy as np
import pandas as pd
import urllib.request
warnings.filterwarnings("ignore")

os.chdir('/config/quant_scripts')
sys.path.insert(0, '.')

# 持仓配置（与smart_guard保持一致）
POSITION_CODES = ['002594', '518880']

# 模型路径
MODEL_PATH = './models/ppo_position_best.zip'
FAST_MODEL_PATH = './models/ppo_position_fast.zip'


def load_ppo_model():
    """加载训练好的PPO模型"""
    from stable_baselines3 import PPO
    
    for path in [MODEL_PATH, FAST_MODEL_PATH, './models/ppo_super_best.zip', './models/sb3_ppo_rdfactors/best_model.zip']:
        if os.path.exists(path):
            try:
                model = PPO.load(path, device='cpu')
                print(f"  ✅ RL模型加载: {path}")
                return model, path
            except Exception as e:
                print(f"  ⚠️ {path} 加载失败: {str(e)[:50]}")
    return None, None


def get_latest_state():
    """
    构建当前交易日状态向量（与环境训练时同构）
    
    返回: 91维numpy数组（与训练时5股票环境兼容）
         或 37维numpy数组（与2股票环境兼容）
    """
    # 获取实时行情
    codes_qs = ['sz002594','sh518880']  # sz=深市(比亚迪), sh=沪市(黄金ETF)
    url = f'https://qt.gtimg.cn/q={",".join(codes_qs)}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    
    prices = {}
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode('gbk')
    for line in raw.strip().split('\n'):
        parts = line.split('~')
        if len(parts) > 34:
            code = parts[2]
            prices[code] = {
                'open': float(parts[5]) if parts[5] else 0,
                'close': float(parts[3]) if parts[3] else 0,
                'high': float(parts[33]) if parts[33] else 0,
                'low': float(parts[34]) if parts[34] else 0,
                'volume': float(parts[6]) if parts[6] else 0,
            }
    
    # 从本地CSV获取最新技术指标
    tech_data = {}
    rd_data = {}
    
    qlib_dir = '/config/qlib_data/features'
    for code in ['002594', '518880']:
        csv_path = os.path.join(qlib_dir, f"{code}.csv")
        if os.path.exists(csv_path):
            try:
                kdf = pd.read_csv(csv_path)
                if len(kdf) > 60:
                    close = kdf['close'].values
                    high = kdf['high'].values
                    low = kdf['low'].values
                    volume = kdf['volume'].values
                    
                    # 技术指标（同add_technical_indicators）
                    sma30 = np.mean(close[-30:])
                    sma60 = np.mean(close[-60:])
                    rsi = 50
                    gains = sum(d for d in np.diff(close[-15:]) if d > 0)
                    losses = abs(sum(d for d in np.diff(close[-15:]) if d < 0))
                    if gains + losses > 0:
                        rsi = 50 + (gains - losses) / (gains + losses) * 50
                    
                    cci = (close[-1] - sma30) / (np.std(close[-30:]) + 1e-8) / 0.015
                    
                    # Bollinger
                    std20 = np.std(close[-20:])
                    ma20 = np.mean(close[-20:])
                    boll_ub = ma20 + 2 * std20
                    boll_lb = ma20 - 2 * std20
                    
                    # MACD简化
                    ema12 = np.mean(close[-12:])
                    ema26 = np.mean(close[-26:])
                    macd = ema12 - ema26
                    
                    # ADX简化
                    dx = abs(high[-1] - low[-1]) / (close[-1] + 1e-8) * 100
                    
                    tech_data[code] = {
                        'macd': macd,
                        'boll_ub': boll_ub,
                        'boll_lb': boll_lb,
                        'rsi_30': rsi,
                        'cci_30': cci,
                        'dx_30': dx,
                        'close_30_sma': sma30,
                        'close_60_sma': sma60,
                    }
                    
                    # RD因子
                    ret5 = close[-1] / close[-6] - 1 if close[-6] > 0 else 0
                    range10 = (max(high[-10:]) - min(low[-10:])) / close[-1] if close[-1] > 0 else 0
                    ma5 = np.mean(close[-5:])
                    dev5 = (close[-1] - ma5) / ma5 if ma5 > 0 else 0
                    dev20 = (close[-1] - sma30) / sma30 if sma30 > 0 else 0
                    vol_ma5 = np.mean(volume[-5:])
                    vol_ratio = volume[-1] / vol_ma5 if vol_ma5 > 0 else 1
                    ret20 = close[-1] / close[-21] - 1 if close[-21] > 0 else 0
                    
                    rd_data[code] = {
                        'rd_ret5_range10': ret5 + range10,
                        'rd_madev5_madev20': dev5 - dev20,
                        'rd_divergence': ret5 - vol_ratio * 0.5,
                        'rd_momentum_accel': ret5 - ret20 * 0.3,
                    }
            except:
                pass
    
    if not prices or not tech_data:
        return None, None
    
    # 构建状态向量（顺序同环境：5价格+8技术+4RD，然后持仓，然后现金）
    stock_order = ['002594', '518880']
    state_parts = []
    
    for code in stock_order:
        p = prices.get(code, {})
        # 5个价格特征
        for feat in ['open', 'high', 'low', 'close', 'volume']:
            state_parts.append(float(p.get(feat, 0)))
        # 8个技术指标
        td = tech_data.get(code, {})
        for feat in ['macd', 'boll_ub', 'boll_lb', 'rsi_30', 'cci_30', 'dx_30', 'close_30_sma', 'close_60_sma']:
            state_parts.append(float(td.get(feat, 0)))
        # 4个RD因子
        rd = rd_data.get(code, {})
        for feat in ['rd_ret5_range10', 'rd_madev5_madev20', 'rd_divergence', 'rd_momentum_accel']:
            state_parts.append(float(rd.get(feat, 0)))
    
    # 持仓（默认100股比亚迪, 300股黄金ETF）
    state_parts.extend([100.0, 300.0])  # 持仓
    state_parts.append(3409.42)  # 现金
    
    state = np.array(state_parts, dtype=np.float32)
    return state, stock_order


def get_rl_signal():
    """
    获取PPO模型的买卖信号
    
    返回:
        signal: dict {stock_code: action_value}
            action > 0.1 = 建议买入比例
            action < -0.1 = 建议卖出比例
            -0.1 ~ 0.1 = 持有
        meta: dict 额外信息
    """
    model, model_path = load_ppo_model()
    if model is None:
        return None, {"error": "无可用模型"}
    
    state, stock_order = get_latest_state()
    if state is None:
        return None, {"error": "无法构建实时状态"}
    
    # PPO推理
    action, _ = model.predict(state, deterministic=True)
    
    # 映射动作到持仓代码
    signal = {}
    for i, code in enumerate(stock_order):
        if i < len(action):
            signal[code] = round(float(action[i]), 4)
    
    meta = {
        "model": model_path,
        "state_dim": len(state),
        "action_raw": [round(float(a), 4) for a in action],
    }
    
    return signal, meta


def format_rl_signal(signal, meta):
    """格式化RL信号为可读文本"""
    if signal is None:
        return "⚠️ RL模型信号不可用"
    
    lines = ["🧠 **PPO策略信号**"]
    
    for code, action in signal.items():
        names = {'002594': '比亚迪', '518880': '黄金ETF'}
        name = names.get(code, code)
        
        if action > 0.1:
            direction = f"📈 买入 {abs(action)*100:.0f}%仓位"
        elif action < -0.1:
            direction = f"📉 卖出 {abs(action)*100:.0f}%仓位"
        else:
            direction = "➡️ 持有"
        
        lines.append(f"  {name}: {direction} (动作值={action:+.3f})")
    
    return "\n".join(lines)


if __name__ == "__main__":
    print("🎯 PPO实时推理测试")
    signal, meta = get_rl_signal()
    if signal:
        print(format_rl_signal(signal, meta))
        print(f"\n  元信息: 模型={meta['model']}, 状态={meta['state_dim']}维")
    else:
        print(f"  ❌ {meta.get('error', '未知错误')}")

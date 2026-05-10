#!/config/quant_env/bin/python3
"""
持仓专属PPO训练 — 比亚迪+黄金ETF
训练后保存模型 → 盘中实时推理
"""
import sys, os, json, warnings, time
import numpy as np
import pandas as pd
warnings.filterwarnings("ignore")

os.chdir('/config/quant_scripts')
sys.path.insert(0, '.')

from finrl_astock_data import fetch_astock_data, add_technical_indicators, split_train_trade
from finrl_astock_env import create_env_from_data, AStockTradingEnv

print("=" * 60)
print("  持仓专属PPO训练: 比亚迪 + 黄金ETF")
print("=" * 60)

# ===== 第1步：数据准备 =====
print("\n📦 [1/4] 获取数据...")
CODES = ['002594', '518880']
df = fetch_astock_data(CODES, days=500)
df = add_technical_indicators(df)
df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=['close','open','high','low','volume'])
# 填充NaN
for c in df.columns:
    if c not in ['date','tic'] and df[c].isna().any():
        df[c] = df[c].fillna(0)

train_df, trade_df = split_train_trade(df, train_ratio=0.7)
print(f"  训练: {len(train_df)}行, 交易: {len(trade_df)}行")

# ===== 第2步：创建环境 =====
print("\n🏗️ [2/4] 创建环境...")
env_train, env_trade = create_env_from_data(train_df, trade_df, initial_cash=100000)

stock_dim = len(CODES)
n_price = 5
n_tech = 8
n_rd = 4
per_stock = n_price + n_tech + n_rd
expected = stock_dim * per_stock + stock_dim + 1
print(f"  状态空间: {env_train.state_dim}维 (预期={expected})")
print(f"  训练天数: {len(env_train.unique_dates)}, 交易天数: {len(env_trade.unique_dates)}")

# ===== 第3步：基准 =====
print("\n📊 [3/4] 基准对比...")
env_train.reset()
for i in range(min(50, env_train.max_step)):
    _, _, done, _, info = env_train.step(np.zeros(env_train.action_space.shape))
    if done:
        break
print(f"  买入持有 (HOLD): 终值={info['portfolio_value']:.2f}, 回报={info['return']:.2f}%")

# ===== 第4步：PPO训练 =====
print("\n🧠 [4/4] SB3 PPO训练...")
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv

vec_env = DummyVecEnv([lambda: env_train])
eval_env = DummyVecEnv([lambda: env_trade])

# 先试10000步快速验证
model = PPO("MlpPolicy", vec_env, learning_rate=3e-4, n_steps=1024,
            batch_size=64, n_epochs=10, gamma=0.99, gae_lambda=0.95,
            clip_range=0.2, ent_coef=0.01, vf_coef=0.5, verbose=0,
            device='cpu')

t0 = time.time()
model.learn(total_timesteps=10000)
elapsed = time.time() - t0
model.save('./models/ppo_position_fast')
print(f"  10000步快速训练: {elapsed:.0f}秒, 模型保存 ✅")

# 评估
state, _ = env_trade.reset()
done = False
vals = []
while not done:
    action, _ = model.predict(state, deterministic=True)
    state, reward, done, _, info = env_trade.step(action)
    vals.append(info['portfolio_value'])
ppo_return = info['return']
print(f"  PPO策略: 终值={vals[-1]:.2f}, 回报={ppo_return:.2f}%")

# 买入持有对比
env_hold = env_trade
state, _ = env_hold.reset()
done = False
while not done:
    state, _, done, _, info = env_hold.step(np.zeros(env_hold.action_space.shape))
hold_return = info['return']
print(f"  买入持有: 终值={info['portfolio_value']:.2f}, 回报={hold_return:.2f}%")
print(f"  PPO vs HOLD: {'✅ PPO胜' if ppo_return > hold_return else '❌ HOLD胜'}")

# ===== 再跑50000步完整训练 =====
print("\n🧠 [5/5] 完整训练 50000步...")
model2 = PPO("MlpPolicy", vec_env, learning_rate=3e-4, n_steps=2048,
             batch_size=64, n_epochs=10, gamma=0.99, gae_lambda=0.95,
             clip_range=0.2, ent_coef=0.01, vf_coef=0.5, verbose=1,
             device='cpu')

t0 = time.time()
model2.learn(total_timesteps=50000)
elapsed = time.time() - t0
model2.save('./models/ppo_position_best')
print(f"  50000步训练: {elapsed:.0f}秒 ✅")

# 评估
state, _ = env_trade.reset()
done = False
vals2 = []
while not done:
    action, _ = model2.predict(state, deterministic=True)
    state, reward, done, _, info = env_trade.step(action)
    vals2.append(info['portfolio_value'])
ppo2_return = info['return']

env_hold2 = env_trade
state, _ = env_hold2.reset()
done = False
while not done:
    state, _, done, _, info = env_hold2.step(np.zeros(env_hold2.action_space.shape))
hold2_return = info['return']

print(f"\n===== 🏆 最终结果 =====")
print(f"  PPO完整训练: 终值={vals2[-1]:.2f}, 回报={ppo2_return:.2f}%")
print(f"  HOLD:        终值={info['portfolio_value']:.2f}, 回报={hold2_return:.2f}%")
print(f"  {'✅ PPO大幅胜出!' if ppo2_return > hold2_return else '⚠️ HOLD更好'}")
print("=" * 60)

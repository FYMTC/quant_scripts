#!/config/quant_env/bin/python3
"""
SB3 PPO 完整训练 + RD因子注入 + 基准对比
"""
import sys, os, json, warnings
import numpy as np
import pandas as pd
warnings.filterwarnings("ignore")

os.chdir('/config/quant_scripts')
sys.path.insert(0, '.')

from finrl_astock_data import fetch_astock_data, add_technical_indicators, split_train_trade
from finrl_astock_env import create_env_from_data, AStockTradingEnv

print("=" * 60)
print("  SB3 PPO 完整训练 — RD因子注入版")
print("=" * 60)

# ===== 第1步：准备数据（含RD因子）=====
print("\n📦 [1/4] 获取A股数据+计算RD因子...")

CODES = ['002594', '600519', '000001', '000333', '300750']
DAYS = 365  # 用1年数据

df = fetch_astock_data(CODES, days=DAYS)
print(f"  原始: {len(df)}行, {df['tic'].nunique()}只股票")

df = add_technical_indicators(df)
rd_cols = [c for c in df.columns if c.startswith('rd_')]
print(f"  RD因子列: {rd_cols}")
print(f"  RD因子非空行: {df[rd_cols].notna().any(axis=1).sum()}")

# 去空值
before = len(df)
df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=['close', 'open', 'high', 'low', 'volume'])
print(f"  去空: {before} → {len(df)}行")

train_df, trade_df = split_train_trade(df, train_ratio=0.7)
print(f"  训练: {len(train_df)}行, 交易: {len(trade_df)}行")

# ===== 第2步：创建环境 =====
print("\n🏗️ [2/4] 创建交易环境（含RD因子状态空间）...")
env_train, env_trade = create_env_from_data(train_df, trade_df, initial_cash=100000)

# 打印状态空间信息
n_price = 5  # open, high, low, close, volume
n_tech = 8   # macd, boll_ub, boll_lb, rsi_30, cci_30, dx_30, close_30_sma, close_60_sma
n_rd = len(rd_cols)
stock_dim = len(CODES)
expected_state = stock_dim * (n_price + n_tech + n_rd) + stock_dim + 1
print(f"  状态空间: {env_train.state_dim}维 (期望={expected_state})")
print(f"  其中: 价格{n_price} + 技术{n_tech} + RD因子{n_rd} = {n_price+n_tech+n_rd}/股")
print(f"  训练天数: {len(env_train.unique_dates)}, 交易天数: {len(env_trade.unique_dates)}")

# ===== 第3步：基准测试 =====
print("\n📊 [3/4] 基准对比...")

# 基准1: 买入持有（全程不交易）
env_train.reset()
for i in range(min(50, env_train.max_step)):
    _, _, done, _, info = env_train.step(np.zeros(env_train.action_space.shape))
    if done:
        break
print(f"  📈 买入持有 (HOLD): 终值={info['portfolio_value']:.2f}, 回报={info['return']:.2f}%")

# 基准2: 全仓买入（如果允许）
env_train.reset()
if env_train.max_step > 20:
    buy_action = np.ones(env_train.action_space.shape) * 0.5
    state, _, _, _, _ = env_train.step(buy_action)
    total = env_train._get_portfolio_value() if hasattr(env_train, '_get_portfolio_value') else 0
    print(f"  尝试买入后市值约: {total:.2f}")

# ===== 第4步：SB3 PPO训练 =====
print("\n🧠 [4/4] SB3 PPO 训练 (50000步)...")
from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import EvalCallback, StopTrainingOnRewardThreshold
from stable_baselines3.common.vec_env import DummyVecEnv
import time

vec_env = DummyVecEnv([lambda: env_train])
eval_env = DummyVecEnv([lambda: env_trade])

# 评估回调：如果平均回报超过15%就提前停止
callback_on_best = StopTrainingOnRewardThreshold(reward_threshold=15.0, verbose=1)
eval_callback = EvalCallback(
    eval_env, 
    best_model_save_path='./models/sb3_ppo_rdfactors',
    log_path='./logs/sb3_ppo_rdfactors',
    eval_freq=5000,
    deterministic=True,
    render=False,
    callback_on_new_best=callback_on_best,
)

model = PPO(
    "MlpPolicy",
    vec_env,
    learning_rate=3e-4,
    n_steps=2048,
    batch_size=64,
    n_epochs=10,
    gamma=0.99,
    gae_lambda=0.95,
    clip_range=0.2,
    ent_coef=0.01,
    vf_coef=0.5,
    max_grad_norm=0.5,
    verbose=1,
    tensorboard_log='./logs',
    device='cpu',
)

t0 = time.time()
model.learn(
    total_timesteps=50000,
    callback=eval_callback,
)
elapsed = time.time() - t0
model.save('./models/sb3_ppo_rdfactors_final')

print(f"\n✅ 训练完成! 耗时: {elapsed:.0f}s")
print(f"  Best模型: ./models/sb3_ppo_rdfactors/best_model.zip")
print(f"  Final模型: ./models/sb3_ppo_rdfactors_final.zip")

# ===== 最终评估 =====
print("\n📈 最终评估...")

# 在交易集上跑完整episode
env_eval = env_trade
state, _ = env_eval.reset()
done = False
total_reward = 0
portfolio_values = []
while not done:
    action, _ = model.predict(state, deterministic=True)
    state, reward, done, _, info = env_eval.step(action)
    total_reward += reward
    portfolio_values.append(info['portfolio_value'])

print(f"  PPO策略: 终值={portfolio_values[-1]:.2f}, 回报={info['return']:.2f}%")

# 买入持有对比
env_hold = env_trade
state, _ = env_hold.reset()
done = False
hold_final = 0
while not done:
    state, _, done, _, info = env_hold.step(np.zeros(env_hold.action_space.shape))
    hold_final = info['portfolio_value']
print(f"  买入持有: 终值={hold_final:.2f}, 回报={info['return']:.2f}%")

print(f"\n  PPO vs 买入持有: {'✅ PPO胜出' if portfolio_values[-1] > hold_final else '❌ 买入持有胜出'}")
print("=" * 60)

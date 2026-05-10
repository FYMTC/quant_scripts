#!/config/quant_env/bin/python3
"""
FinRL_DeepSeek A股适配 — 全管线一键运行
数据获取 → 技术指标 → 环境创建 → CPPO/PPO训练 → 回测评估
"""
import sys
import os
import numpy as np

os.chdir('/config/quant_scripts')

print("=" * 60)
print("  FinRL_DeepSeek A股适配 — 全管线")
print("=" * 60)

# ====== 第1步：准备数据 ======
print("\n📦 [1/5] 获取A股数据...")
from finrl_astock_data import prepare_finrl_data

# A股核心股票池
CODES = ['002594', '600519', '000001', '000333', '300750']
DAYS = 180

train_df, trade_df = prepare_finrl_data(CODES, days=DAYS)

if train_df is None or len(train_df) < 100:
    print("❌ 数据不足，尝试缩小股票池")
    train_df, trade_df = prepare_finrl_data(['002594', '600519'], days=DAYS)

if train_df is None or len(train_df) < 50:
    print("❌ 数据获取失败，终止")
    sys.exit(1)

# ====== 第2步：创建环境 ======
print("\n🏗️ [2/5] 创建A股交易环境...")
from finrl_astock_env import create_env_from_data, AStockTradingEnv

env_train, env_trade = create_env_from_data(train_df, trade_df, initial_cash=100000)
print(f"  训练环境: {len(env_train.unique_dates)}天, 状态{env_train.state_dim}维")
print(f"  交易环境: {len(env_trade.unique_dates)}天, {env_trade.stock_dim}只股票")

# ====== 第3步：基准测试（不交易）=====
print("\n📊 [3/5] 基准测试（不动策略）...")
env_train.reset()
for i in range(min(50, env_train.max_step)):
    _, _, done, _, info = env_train.step(np.zeros(env_train.action_space.shape))
    if done:
        break
print(f"  买入持有策略: 最终价值={info['portfolio_value']:.2f}, 回报={info['return']:.2f}%")

# ====== 第4步：训练PPO ======
print("\n🧠 [4/5] 训练PPO（稳定基线）...")
from finrl_astock_trainer import train_sb3_ppo, train_cppo_simple, evaluate_model

# 先试简单CPPO
print("  训练CPPO-Simple...")
os.makedirs('./models', exist_ok=True)
actor = train_cppo_simple(env_train, env_trade, total_steps=5000)

# ====== 第5步：评估 ======
print("\n📈 [5/5] 评估模型...")
from finrl_astock_trainer import _evaluate

eval_return = _evaluate(env_trade, actor, episodes=3)
print(f"  CPPO评估: 平均回报={eval_return:.4f}")
print(f"  买入持有回报: {info['return']:.2f}%")

print("\n" + "=" * 60)
print("  全管线运行完成!")
print("=" * 60)

#!/config/quant_env/bin/python3
"""
🏋️ 超级PPO训练 — 多股面板 × 超长历史 × 全量因子
用20只股票的CSV数据构建高维状态空间，训练更强大的PPO

核心创新:
1. 多股面板（20只×560天）→ 模型学到跨股票模式
2. RD因子自动发现结果注入状态空间
3. 基准全面对比：HOLD / 等权 / 最优MA
"""
import sys, os, json, warnings, time
import numpy as np
import pandas as pd
warnings.filterwarnings("ignore")

os.chdir('/config/quant_scripts')
sys.path.insert(0, '.')

from finrl_astock_env import create_env_from_data
from finrl_astock_data import add_technical_indicators

print("=" * 60)
print("  🏋️ 超级PPO训练 — 多股面板 × 全量因子")
print("=" * 60)

# ===== 第1步：加载所有CSV数据 =====
QLIB_DIR = '/config/qlib_data/features'
ALL_CODES = [
    '002594', '600519', '000001', '000333', '300750',
    '002415', '002475', '518880', '000858', '600887',
    '600036', '600276', '600941', '601318', '000066',
    '002049', '600105', '600522', '002297', '300589',
]

print(f"\n📦 [1/5] 加载 {len(ALL_CODES)} 只股票的CSV数据...")

all_dfs = []
missing_codes = []
for code in ALL_CODES:
    csv_path = os.path.join(QLIB_DIR, f"{code}.csv")
    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path)
        if len(df) > 50:
            df['tic'] = code
            all_dfs.append(df)
        else:
            missing_codes.append(code)
    else:
        missing_codes.append(code)

if missing_codes:
    print(f"  ⏭️ 跳过无法加载: {missing_codes}")

df = pd.concat(all_dfs, ignore_index=True)
df['date'] = pd.to_datetime(df['date'].astype(str))
df = df.sort_values(['date', 'tic']).reset_index(drop=True)

print(f"  加载 {len(all_dfs)} 只股票, {len(df)} 条记录")
print(f"  日期范围: {df['date'].min().date()} ~ {df['date'].max().date()}")

# ===== 第2步：计算全量因子 =====
print(f"\n🧮 [2/5] 计算技术指标+RD因子...")

# 重命名列以兼容add_technical_indicators
df = df.rename(columns={
    'pct_change': 'pct_chg',
})

df = add_technical_indicators(df)
df = df.replace([np.inf, -np.inf], np.nan)

# 填充NaN
rd_cols = [c for c in df.columns if c.startswith('rd_')]
indicator_cols = ['macd', 'boll_ub', 'boll_lb', 'rsi_30', 'cci_30', 'dx_30', 'close_30_sma', 'close_60_sma']
for c in rd_cols + indicator_cols:
    if c in df.columns:
        df[c] = df[c].fillna(0)

# 删掉基础价格列的NaN
before = len(df)
df = df.dropna(subset=['close', 'open', 'high', 'low', 'volume'])
print(f"  清理后: {before} → {len(df)} 行")

rd_cols_in_data = [c for c in df.columns if c.startswith('rd_')]
print(f"  RD因子列: {rd_cols_in_data}")

# ===== 第3步：分割数据集 =====
print(f"\n✂️ [3/5] 分割训练/交易集...")
dates = sorted(df['date'].unique())
split_idx = int(len(dates) * 0.7)
train_dates = dates[:split_idx]
trade_dates = dates[split_idx:]

train_df = df[df['date'].isin(train_dates)].copy()
trade_df = df[df['date'].isin(trade_dates)].copy()

print(f"  训练: {train_df['date'].nunique()}天, {train_df['tic'].nunique()}只, {len(train_df)}行")
print(f"  交易: {trade_df['date'].nunique()}天, {trade_df['tic'].nunique()}只, {len(trade_df)}行")

# ===== 第4步：创建环境 =====
print(f"\n🏗️ [4/5] 创建环境...")
env_train, env_trade = create_env_from_data(train_df, trade_df, initial_cash=100000)

n_price = 5
n_tech = 8
n_rd = len(rd_cols_in_data)
per_stock = n_price + n_tech + n_rd
stock_dim = train_df['tic'].nunique()
expected = stock_dim * per_stock + stock_dim + 1

print(f"  状态空间: {env_train.state_dim}维 (预期={expected})")
print(f"  每只股票: 价格{n_price}+技术{n_tech}+RD{n_rd}={per_stock}维")

# ===== 基准测试 =====
print(f"\n📊 基准测试...")
# HOLD（不交易）
env_train.reset()
for i in range(min(60, env_train.max_step)):
    _, _, done, _, info = env_train.step(np.zeros(env_train.action_space.shape))
    if done:
        break
print(f"  HOLD: 终值={info['portfolio_value']:.2f}, 回报={info['return']:.2f}%")

# 全仓买入
env_train.reset()
buy_action = np.ones(env_train.action_space.shape) * 0.3
state, _, _, _, info = env_train.step(buy_action)
val = env_train._get_portfolio_value()
print(f"  买入30%仓位: 市值≈{val:.2f}")

# ===== 第5步：PPO训练 =====
print(f"\n🧠 [5/5] PPO训练 (100000步)...")
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv
from stable_baselines3.common.callbacks import EvalCallback, StopTrainingOnRewardThreshold

vec_env = DummyVecEnv([lambda: env_train])
eval_env = DummyVecEnv([lambda: env_trade])

model = PPO(
    "MlpPolicy", vec_env,
    learning_rate=3e-4, n_steps=2048,
    batch_size=64, n_epochs=10,
    gamma=0.99, gae_lambda=0.95,
    clip_range=0.2, ent_coef=0.02,
    vf_coef=0.5, verbose=1,
    device='cpu',
    tensorboard_log='./logs',
)

t0 = time.time()
model.learn(total_timesteps=100000)
elapsed = time.time() - t0
model.save('./models/ppo_super_best')
print(f"\n✅ 100000步训练完成! 耗时: {elapsed:.0f}秒")

# ===== 最终评估 =====
print(f"\n{'='*60}")
print(f"  📈 最终评估")
print(f"{'='*60}")

# PPO
state, _ = env_trade.reset()
done = False
ppo_vals = []
while not done:
    action, _ = model.predict(state, deterministic=True)
    state, reward, done, _, info = env_trade.step(action)
    ppo_vals.append(info['portfolio_value'])
ppo_return = info['return']
print(f"  PPO:    终值={ppo_vals[-1]:.2f}, 回报={ppo_return:.2f}%")

# HOLD
env_hold = env_trade
state, _ = env_hold.reset()
done = False
while not done:
    state, _, done, _, info = env_hold.step(np.zeros(env_hold.action_space.shape))
hold_return = info['return']
print(f"  HOLD:   终值={info['portfolio_value']:.2f}, 回报={hold_return:.2f}%")

# 等权买入（每只买1/n仓位）
env_equal = env_trade
state, _ = env_equal.reset()
done = False
equal_action = np.ones(env_equal.action_space.shape) * (0.8 / stock_dim)
while not done:
    state, _, done, _, info = env_equal.step(equal_action)
print(f"  等权:   终值={info['portfolio_value']:.2f}, 回报={info['return']:.2f}%")

print(f"\n{'='*60}")
print(f"  🏆 {'PPO胜出!' if ppo_return > max(hold_return, info['return']) else '基准更好'}")
print(f"  📦 模型: ./models/ppo_super_best.zip")
print(f"{'='*60}")

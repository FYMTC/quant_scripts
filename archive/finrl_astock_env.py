#!/config/quant_env/bin/python3
"""
FinRL_DeepSeek A股适配 — 交易环境 (StockTradingEnv)
从FinRL_DeepSeek源码提取并适配A股，保持CPPO兼容

核心修改：
1. 数据格式适配（A股DataFrame）
2. 移除yfinance依赖
3. 可选的RD因子注入接口
"""
import numpy as np
import pandas as pd
from gymnasium import spaces
import gymnasium as gym
from typing import Dict, List


class AStockTradingEnv(gym.Env):
    """
    A股交易环境 — 兼容CPPO/PPO训练
    
    状态空间: [价格指标 + 技术指标 + 持仓 + (可选LLM情绪/RD因子)]
    动作空间: [-1, 1] 连续值，负=卖出比例，正=买入比例，0=持有
    """
    
    metadata = {'render.modes': ['human']}
    
    def __init__(self, 
                 df: pd.DataFrame,
                 stock_dim: int,
                 hmax: int = 100,
                 initial_amount: int = 100000,
                 transaction_cost_pct: float = 0.001,
                 state_space: int = None,
                 action_space: int = None,
                 tech_indicator_list: list = None,
                 reward_scaling: float = 1e-4,
                 **kwargs):
        
        super().__init__()
        
        self.df = df
        self.stock_dim = stock_dim
        self.hmax = hmax
        self.initial_amount = initial_amount
        self.transaction_cost_pct = transaction_cost_pct
        self.reward_scaling = reward_scaling
        self.tech_indicator_list = tech_indicator_list or [
            'macd', 'boll_ub', 'boll_lb', 'rsi_30', 'cci_30', 'dx_30',
            'close_30_sma', 'close_60_sma'
        ]

        # RD因子列（自动检测）
        rd_cols = [c for c in df.columns if c.startswith('rd_')]
        self.rd_factor_list = rd_cols
        
        # 每只股票的额外特征维度（价格+技术指标+RD因子）
        self.price_features = ['open', 'high', 'low', 'close', 'volume']
        self.n_features_per_stock = len(self.price_features) + len(self.tech_indicator_list) + len(self.rd_factor_list)
        
        # 动作空间: 每只股票一个连续动作 [-1, 1]
        self.action_space = spaces.Box(low=-1, high=1, shape=(self.stock_dim,), dtype=np.float32)
        
        # 状态空间: 价格+技术指标 每只股票 + 持仓+现金
        self.state_dim = self.stock_dim * self.n_features_per_stock + self.stock_dim + 1
        self.observation_space = spaces.Box(low=-np.inf, high=np.inf, shape=(self.state_dim,), dtype=np.float32)
        
        # 交易日索引
        self.unique_dates = self.df['date'].unique()
        self.max_step = len(self.unique_dates) - 1
        
        # 缓存按天索引的数据
        self._preprocess_data()
        
        # 状态
        self.reset()
    
    def _preprocess_data(self):
        """按日期和股票代码构建快速查找"""
        self.data_by_date = {}
        for date in self.unique_dates:
            day_data = self.df[self.df['date'] == date]
            records = {}
            for _, row in day_data.iterrows():
                tic = row['tic']
                features = {}
                for col in self.price_features + self.tech_indicator_list + self.rd_factor_list:
                    if col in row:
                        val = row[col]
                        features[col] = 0.0 if (val is None or (isinstance(val, float) and np.isnan(val))) else val
                    else:
                        features[col] = 0.0
                records[tic] = features
            self.data_by_date[date] = records
    
    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        self.day = 0
        self.cash = self.initial_amount
        
        # 持仓: {tic: shares}
        self.stocks = {tic: 0 for tic in self.df['tic'].unique()}
        self.tic_list = list(self.stocks.keys())
        
        return self._get_state(), {}
    
    def step(self, actions):
        """执行动作，返回 next_state, reward, done, info"""
        # 1. 获取当前价格并记录执行前的组合价值
        prev_val = self._get_portfolio_value()
        
        # 2. 执行交易
        self._trade(actions)
        
        # 3. 前进一天（价格变化）
        self.day += 1
        done = self.day >= self.max_step
        
        # 4. 计算奖励（新价格下的组合价值变化）
        curr_val = self._get_portfolio_value()
        reward = (curr_val - prev_val) * self.reward_scaling
        
        # 4. 获取新状态
        state = self._get_state() if not done else np.zeros(self.state_dim)
        
        # 5. 额外信息
        info = {
            'day': self.day,
            'cash': self.cash,
            'portfolio_value': curr_val,
            'return': ((curr_val / self.initial_amount - 1) * 100) if self.initial_amount > 0 else 0,
        }
        
        return state, reward, done, False, info
    
    def _trade(self, actions):
        """执行买卖动作（A股：最小单位100股）"""
        current_prices = self._get_current_prices()
        
        for i, tic in enumerate(self.tic_list):
            action = actions[i]
            price = current_prices.get(tic, 0)
            
            if price <= 0 or np.isnan(price):
                continue
            
            if action > 0:  # 买入
                # 可买数量 = 现金 × 买入比例 / 价格，取整到100股
                buy_value = self.cash * min(abs(action), 1.0)
                shares_to_buy = int(buy_value / price / 100) * 100
                
                if shares_to_buy > 0:
                    cost = shares_to_buy * price * (1 + self.transaction_cost_pct)
                    if cost <= self.cash:
                        self.stocks[tic] += shares_to_buy
                        self.cash -= cost
            
            elif action < 0:  # 卖出
                shares_to_sell = int(self.stocks[tic] * min(abs(action), 1.0) / 100) * 100
                shares_to_sell = min(shares_to_sell, self.stocks[tic])
                
                if shares_to_sell > 0:
                    revenue = shares_to_sell * price * (1 - self.transaction_cost_pct)
                    self.stocks[tic] -= shares_to_sell
                    self.cash += revenue
    
    def _get_current_prices(self):
        """获取当前日期的各股票价格"""
        if self.day >= len(self.unique_dates):
            return {tic: 0 for tic in self.tic_list}
        
        date = self.unique_dates[self.day]
        day_data = self.data_by_date.get(date, {})
        
        prices = {}
        for tic in self.tic_list:
            record = day_data.get(tic, {})
            prices[tic] = record.get('close', 0)
        
        return prices
    
    def _get_portfolio_value(self):
        """计算当前持仓总价值"""
        prices = self._get_current_prices()
        stock_value = sum(self.stocks[tic] * prices.get(tic, 0) for tic in self.tic_list)
        return self.cash + stock_value
    
    def _get_state(self):
        """构建状态向量"""
        if self.day >= len(self.unique_dates):
            return np.zeros(self.state_dim)
        
        date = self.unique_dates[self.day]
        day_data = self.data_by_date.get(date, {})
        
        state_list = []
        for tic in self.tic_list:
            record = day_data.get(tic, {})
            # 价格特征
            for col in self.price_features:
                state_list.append(record.get(col, 0))
            # 技术指标
            for col in self.tech_indicator_list:
                state_list.append(record.get(col, 0))
            # RD因子
            for col in self.rd_factor_list:
                state_list.append(record.get(col, 0))
        
        # 账户状态
        for tic in self.tic_list:
            state_list.append(self.stocks[tic])
        state_list.append(self.cash)
        
        return np.array(state_list, dtype=np.float32)
    
    def render(self, mode='human'):
        val = self._get_portfolio_value()
        profit = val - self.initial_amount
        print(f"Day {self.day}: 价值={val:.2f}, 收益={profit:.2f} ({profit/self.initial_amount*100:.2f}%)")


# =========== 快捷训练接口 ===========
def create_env_from_data(train_df, trade_df, tech_indicators=None, initial_cash=100000):
    """从prepare_finrl_data的输出创建训练和交易环境"""
    stock_dim = train_df['tic'].nunique()
    
    if tech_indicators is None:
        tech_indicators = ['macd', 'rsi_30', 'cci_30', 'dx_30', 
                          'close_30_sma', 'close_60_sma',
                          'boll_ub', 'boll_lb']
    
    # 只保留存在的指标
    tech_indicators = [c for c in tech_indicators if c in train_df.columns]
    
    env_train = AStockTradingEnv(
        df=train_df,
        stock_dim=stock_dim,
        initial_amount=initial_cash,
        tech_indicator_list=tech_indicators,
    )
    
    env_trade = AStockTradingEnv(
        df=trade_df,
        stock_dim=stock_dim,
        initial_amount=initial_cash,
        tech_indicator_list=tech_indicators,
    )
    
    return env_train, env_trade


if __name__ == "__main__":
    # 测试环境
    print("===== 测试A股交易环境 =====")
    
    # 从finrl_astock_data导入数据准备
    from finrl_astock_data import prepare_finrl_data
    
    train_df, trade_df = prepare_finrl_data(['002594', '600522'], days=120)
    
    if train_df is not None:
        env_train, env_trade = create_env_from_data(train_df, trade_df)
        
        print(f"\n状态空间维度: {env_train.state_dim}")
        print(f"动作空间维度: {env_train.action_space.shape}")
        print(f"训练天数: {len(env_train.unique_dates)}")
        
        # 测试一步
        state, _ = env_train.reset()
        print(f"初始状态形状: {state.shape}")
        
        action = np.random.uniform(-0.1, 0.1, size=env_train.action_space.shape)
        next_state, reward, done, _, info = env_train.step(action)
        print(f"执行随机动作后: reward={reward:.6f}, 组合价值={info['portfolio_value']:.2f}")
        
        # 跑完整episode
        env_train.reset()
        total_reward = 0
        for _ in range(min(30, env_train.max_step)):
            action = np.zeros(env_train.action_space.shape)
            _, r, done, _, info = env_train.step(action)
            total_reward += r
            if done:
                break
        
        print(f"全程不交易(hold): 最终价值={info['portfolio_value']:.2f}, 回报={info['return']:.2f}%")
        print("✅ 环境测试通过!")

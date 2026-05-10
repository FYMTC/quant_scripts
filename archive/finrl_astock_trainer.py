#!/config/quant_env/bin/python3
"""
FinRL_DeepSeek A股适配 — CPPO/PPO训练器
从FinRL_DeepSeek源码提取CPPO核心逻辑，适配A股环境

CPPO = Conditional PPO + CVaR风险约束 + LLM情绪调整

简化版：去掉MPI依赖 + 去掉DeepSeek API依赖 + 可注入RD因子
"""
import numpy as np
import torch
import torch.nn as nn
from torch.optim import Adam
from stable_baselines3 import PPO as SB3PPO
from stable_baselines3.common.vec_env import DummyVecEnv
from typing import Optional, Callable

import sys
sys.path.insert(0, '/config/quant_scripts')


class ActorNetwork(nn.Module):
    """策略网络 — 输出动作均值"""
    def __init__(self, input_dim, output_dim, hidden_dim=256):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, output_dim),
            nn.Tanh(),
        )
    
    def forward(self, x):
        return self.net(x)


class CriticNetwork(nn.Module):
    """价值网络 — 输出状态价值V(s)"""
    def __init__(self, input_dim, hidden_dim=256):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, 1),
        )
    
    def forward(self, x):
        return self.net(x)


def train_sb3_ppo(env_train, env_trade, total_timesteps=50000, model_name="astock_ppo"):
    """
    使用Stable-Baselines3 PPO训练（最稳定方案）
    
    这是最快能跑通的方案，也是FinRL官方推荐的训练方式。
    """
    from stable_baselines3 import PPO
    from stable_baselines3.common.callbacks import EvalCallback
    
    # 包装环境
    vec_env = DummyVecEnv([lambda: env_train])
    eval_env = DummyVecEnv([lambda: env_trade])
    
    # 创建PPO模型
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
        tensorboard_log=f"./logs/{model_name}",
    )
    
    # 训练
    print(f"\n===== 训练 PPO (A股) =====")
    print(f"  总步数: {total_timesteps}")
    print(f"  状态维度: {env_train.state_dim}")
    
    model.learn(
        total_timesteps=total_timesteps,
        callback=EvalCallback(eval_env, best_model_save_path=f"./models/{model_name}",
                              log_path=f"./logs/{model_name}", eval_freq=5000,
                              deterministic=True, render=False),
    )
    
    # 保存
    model.save(f"./models/{model_name}_final")
    print(f"✅ 模型已保存: ./models/{model_name}_final")
    
    return model


def train_cppo_simple(env_train, env_trade, total_steps=10000, lr=1e-4):
    """
    CPPO简化版（不含MPI，不含LLM情绪，聚焦CVaR风险约束）
    
    CPPO vs PPO核心差异:
    - 额外维护CVaR风险约束
    - 当 trajectory return < 阈值时，施加额外惩罚
    - 自动区分"好坏轨迹"并差异化优化
    """
    input_dim = env_train.state_dim
    action_dim = env_train.action_space.shape[0]
    
    actor = ActorNetwork(input_dim, action_dim)
    critic = CriticNetwork(input_dim)
    actor_optim = Adam(actor.parameters(), lr=lr)
    critic_optim = Adam(critic.parameters(), lr=lr)
    
    # CPPO特有参数
    nu = 0.0  # CVaR阈值（坏轨迹判定线）
    cvarlam = 0.1  # CVaR惩罚系数
    nu_lr = 1e-4  # 阈值更新率
    
    print(f"\n===== 训练 CPPO-Simple (A股) =====")
    print(f"  总步数: {total_steps}")
    print(f"  CVaR阈值(nu): {nu}, 惩罚系数(cvarlam): {cvarlam}")
    
    step = 0
    episode = 0
    best_return = -np.inf
    
    while step < total_steps:
        episode += 1
        state, _ = env_train.reset()
        done = False
        
        states, actions, rewards, values, dones = [], [], [], [], []
        episode_return = 0
        
        while not done:
            state_t = torch.FloatTensor(state).unsqueeze(0)
            
            with torch.no_grad():
                action = actor(state_t).numpy()[0]
                value = critic(state_t).item()
            
            # 添加噪声探索
            noise = np.random.normal(0, 0.1, size=action.shape)
            action_noisy = np.clip(action + noise, -1, 1)
            
            next_state, reward, done, _, info = env_train.step(action_noisy)
            
            states.append(state)
            actions.append(action_noisy)
            rewards.append(reward)
            values.append(value)
            dones.append(done)
            
            episode_return += reward
            state = next_state
            step += 1
        
        # === CPPO核心：轨迹价值评估与风险约束 ===
        gamma = 0.99
        returns = []
        G = 0
        for r in reversed(rewards):
            G = r + gamma * G
            returns.insert(0, G)
        
        returns_t = torch.FloatTensor(returns).unsqueeze(1)
        values_t = torch.FloatTensor(values).unsqueeze(1)
        
        # 优势函数
        advantages = returns_t - values_t
        
        # CPPO: CVaR风险调整
        trajectory_return = sum(rewards)
        if trajectory_return < nu:
            cvae_weight = 1.0 + cvarlam * (nu - trajectory_return) / (abs(nu) + 1e-8)
            advantages = advantages * cvae_weight
            nu = 0.9 * nu + 0.1 * trajectory_return
        
        advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)
        
        # 更新Critic
        # 重新过一遍critic确保计算图
        states_t = torch.FloatTensor(np.array(states))
        values_pred = critic(states_t)
        critic_loss = nn.MSELoss()(values_pred, returns_t)
        
        critic_optim.zero_grad()
        critic_loss.backward()
        critic_optim.step()
        
        # 更新Actor
        actions_t = torch.FloatTensor(np.array(actions))
        action_pred = actor(states_t)
        actor_loss = -(advantages.detach() * action_pred).mean()
        
        actor_optim.zero_grad()
        actor_loss.backward()
        actor_optim.step()
        
        # 评估
        if episode % 10 == 0:
            eval_return = _evaluate(env_trade, actor)
            if eval_return > best_return:
                best_return = eval_return
                torch.save(actor.state_dict(), f"./models/cppo_astock_best.pth")
            
            print(f"  Episode {episode}: train_return={trajectory_return:.4f}, "
                  f"eval_return={eval_return:.4f}, nu={nu:.4f}, step={step}")
    
    print(f"✅ CPPO训练完成, 最佳评估回报: {best_return:.4f}")
    return actor


def _evaluate(env, actor, episodes=5):
    """评估策略表现"""
    total_returns = []
    for _ in range(episodes):
        state, _ = env.reset()
        done = False
        ep_return = 0
        while not done:
            with torch.no_grad():
                state_t = torch.FloatTensor(state).unsqueeze(0)
                action = actor(state_t).numpy()[0]
            state, reward, done, _, _ = env.step(action)
            ep_return += reward
        total_returns.append(ep_return)
    return np.mean(total_returns)


def evaluate_model(env, model, episodes=2):
    """评估SB3 PPO模型"""
    returns = []
    for ep in range(episodes):
        state, _ = env.reset()
        done = False
        total = 0
        while not done:
            action, _ = model.predict(state, deterministic=True)
            state, reward, done, _, info = env.step(action)
            total += reward
        portfolio_return = info['return']
        returns.append(portfolio_return)
        print(f"  Episode {ep+1}: 回报={portfolio_return:.2f}%")
    
    print(f"  平均回报: {np.mean(returns):.2f}%")
    return np.mean(returns)


if __name__ == "__main__":
    print("===== CPPO/PPO A股训练器 =====")
    print("可用函数:")
    print("  train_sb3_ppo(env_train, env_trade)  — SB3 PPO训练")
    print("  train_cppo_simple(env_train, env_trade)  — CPPO简化版训练")
    print("  evaluate_model(env, model)  — 评估模型")

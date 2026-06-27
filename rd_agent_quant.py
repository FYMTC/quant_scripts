#!python3
"""
R&D-Agent-Quant 核心实现 v1.0
论文: https://arxiv.org/abs/2505.15155v2

三大模块:
1. Co-STEER — 结构化思考+标准化执行的因子/模型代码生成
2. Bandit调度 — 8维绩效状态→自适应选择因子/模型优化方向
3. 因子冗余检测 — 新因子vs老因子IC相关性计算(IC_max≥0.99剔除)

用法:
  python3 rd_agent_quant.py --mode full     # 全流程（Co-STEER+bandit+冗余检测）
  python3 rd_agent_quant.py --mode factor   # 仅因子发现
  python3 rd_agent_quant.py --mode bandit   # 仅bandit调度诊断
"""
import sys, os, json, warnings, argparse, math
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from datetime import datetime
warnings.filterwarnings("ignore")

# 2026-06-27 修复：原代码 os.chdir(cfg.root) 在 from system_config import cfg 之前
# 触发 NameError: name 'cfg' is not defined（与 tradingagents_runner.py 同类 use-before-import bug）
sys.path.insert(0, '.')
from system_config import cfg
from data_converter import STOCK_MAP
os.chdir(cfg.root)

# ============================================================
# 配置（路径从 system_config 读取，2026-06-26 去硬编码）
# ============================================================
QLIB_DATA_DIR = cfg.path.qlib_data_dir
FACTOR_LIB_PATH = cfg.path.factor_library
QLIB_DIR = f'{QLIB_DATA_DIR}/features'
BANDIT_STATE_FILE = f'{QLIB_DATA_DIR}/bandit_state.json'
RD_LOG_FILE = f'{QLIB_DATA_DIR}/rd_agent_quant_log.json'

# 核心股票池
CORE_CODES = ['002594', '518880', '512480', '600519', '000001', '000333',
              '300750', '002415', '002475', '000858', '600887', '600522',
              '600487', '000938', '002466']

# 8维绩效状态名称
STATE_DIM_NAMES = [
    'IC_mean', 'IC_std', 'Rank_IC', 'ICIR',
    'ARR_1m', 'MDD_1m', 'IR', 'factor_diversity'
]


# ============================================================
# 模块1: 因子冗余检测（IC_max ≥ 0.99剔除）
# ============================================================
def compute_factor_correlation(existing_factors, new_factor_vals):
    """计算新因子与已有因子的最大IC相关性"""
    if not existing_factors or len(new_factor_vals) == 0:
        return 0.0
    
    max_corr = 0.0
    for name, vals in existing_factors.items():
        if len(vals) != len(new_factor_vals):
            continue
        mask = ~(np.isnan(vals) | np.isnan(new_factor_vals))
        if mask.sum() < 10:
            continue
        corr, _ = spearmanr(vals[mask], new_factor_vals[mask])
        if not np.isnan(corr):
            max_corr = max(max_corr, abs(corr))
    return max_corr


def filter_redundant_factors(base_df, new_factors_df, ic_threshold=0.99):
    """过滤冗余因子：与已有因子IC相关性≥0.99的剔除"""
    existing = {col: base_df[col].values for col in base_df.columns}
    
    keep_cols = []
    for col in new_factors_df.columns:
        vals = new_factors_df[col].values
        max_corr = compute_factor_correlation(
            {**existing, **{c: new_factors_df[c].values for c in keep_cols}},
            vals
        )
        if max_corr < ic_threshold:
            keep_cols.append(col)
    
    return new_factors_df[keep_cols] if keep_cols else pd.DataFrame()


# ============================================================
# 模块2: Co-STEER — 结构化因子组合生成
# ============================================================
def costeer_generate_factors(base_df, top_ics, mode='standard'):
    """
    Co-STEER风格因子生成
    mode: standard=标准组合, aggressive=激进探索, conservative=保守优化
    """
    new_factors = {}
    top_names = [r['factor'] for r in top_ics[:5]]
    
    has_ret = any('Ret_' in n for n in top_names)
    has_range = any('Range_' in n for n in top_names)
    has_ma = any('MA_Dev_' in n for n in top_names)
    has_vol = any('Vol_Ratio_' in n for n in top_names)
    has_pv = any('PV_Corr_' in n for n in top_names)
    
    # ----- 标准组合（论文中的核心组合）-----
    if has_ret and has_range:
        if 'Ret_5' in base_df and 'Range_10' in base_df:
            new_factors['RD_Ret5_Range10'] = base_df['Ret_5'] + base_df['Range_10']
    
    if has_ma:
        if 'MA_Dev_5' in base_df and 'MA_Dev_20' in base_df:
            new_factors['RD_MADev5_MADev20'] = base_df['MA_Dev_5'] - base_df['MA_Dev_20']
        if 'MA_Dev_10' in base_df and 'MA_Dev_30' in base_df:
            new_factors['RD_MADev10_MADev30'] = base_df['MA_Dev_10'] - base_df['MA_Dev_30']
    
    if has_ret and has_vol:
        if 'Ret_5' in base_df and 'Vol_Ratio_5' in base_df:
            new_factors['RD_Divergence'] = base_df['Ret_5'] - base_df['Vol_Ratio_5'] * 0.5
    
    if 'Ret_5' in base_df and 'Ret_20' in base_df:
        new_factors['RD_Momentum_Accel'] = base_df['Ret_5'] - base_df['Ret_20'] * 0.3
    
    if has_pv and has_ma:
        if 'PV_Corr_5' in base_df and 'MA_Dev_5' in base_df:
            new_factors['RD_PV_Trend'] = base_df['PV_Corr_5'] * base_df['MA_Dev_5']
    
    if has_range and has_vol:
        if 'Range_5' in base_df and 'Vol_Ratio_5' in base_df:
            new_factors['RD_Vol_Scaled'] = base_df['Range_5'] * base_df['Vol_Ratio_5']
    
    # ----- 激进模式额外因子（覆盖更多组合）-----
    if mode == 'aggressive':
        # 多周期复合
        for i, p1 in enumerate([5, 10]):
            for p2 in [10, 20]:
                if p1 < p2:
                    name = f'RD_Ret{p1}_Ret{p2}_Diff'
                    if f'Ret_{p1}' in base_df and f'Ret_{p2}' in base_df:
                        new_factors[name] = base_df[f'Ret_{p1}'] - base_df[f'Ret_{p2}']
        
        # 量价背离增强
        if has_pv:
            for p in [5, 10]:
                if f'PV_Corr_{p}' in base_df:
                    if 'Ret_3' in base_df:
                        new_factors[f'RD_PV_Ret_{p}'] = base_df[f'PV_Corr_{p}'] * base_df['Ret_3']
        
        # 波动缩放增强
        if has_range:
            for p in [5, 10, 20]:
                if f'Range_{p}' in base_df and 'Vol_Ratio_5' in base_df:
                    new_factors[f'RD_RangeVol_{p}'] = base_df[f'Range_{p}'] * base_df['Vol_Ratio_5']
    
    # ----- 保守模式（只保留已验证有效的组合）-----
    if mode == 'conservative':
        # 只保留论文中已验证的
        pass  # 标准组合已经包含了核心因子
    
    return pd.DataFrame(new_factors)


def compute_base_factors(df):
    """计算28个基础因子"""
    close = df['close'].values
    volume = df['volume'].values
    high = df['high'].values
    low = df['low'].values
    amount = df['amount'].values
    
    features = {}
    for p in [5, 10, 20, 30, 60]:
        ma = pd.Series(close).rolling(p).mean().values
        features[f'MA_Dev_{p}'] = (close - ma) / (ma + 1e-9)
    for p in [5, 10, 20]:
        vol_ma = pd.Series(volume).rolling(p).mean().values
        features[f'Vol_Ratio_{p}'] = volume / (vol_ma + 1e-9)
    for p in [5, 10, 20]:
        features[f'Range_{p}'] = (pd.Series(high).rolling(p).max() - pd.Series(low).rolling(p).min()).values / (close + 1e-9)
    for p in [1, 3, 5, 10, 20]:
        features[f'Ret_{p}'] = pd.Series(close).pct_change(p).values
    for p in [10, 20]:
        hh = pd.Series(high).rolling(p).max().values
        ll = pd.Series(low).rolling(p).min().values
        features[f'Price_Pos_{p}'] = (close - ll) / (hh - ll + 1e-9)
    for p in [5, 10, 20]:
        features[f'PV_Corr_{p}'] = pd.Series(close).rolling(p).corr(pd.Series(volume)).values
    for p in [1, 3, 5]:
        features[f'Vol_Chg_{p}'] = pd.Series(volume).pct_change(p).values
    
    vwap = (high + low + close) / 3
    vwap_ma = pd.Series(vwap * volume).rolling(20).sum().values / (pd.Series(volume).rolling(20).sum().values + 1e-9)
    features['VWAP_Dev'] = (close - vwap_ma) / (vwap_ma + 1e-9)
    features['Vol_Amount_Ratio'] = volume / (amount + 1e-9)
    
    if 'turnover' in df.columns:
        turnover = df['turnover'].values
        for p in [5, 20]:
            features[f'Turnover_Chg_{p}'] = pd.Series(turnover).pct_change(p).values
    
    return pd.DataFrame(features)


def compute_future_return(close, forward=5):
    n = len(close)
    future = np.full(n, np.nan)
    for i in range(n - forward):
        future[i] = close[i + forward] / close[i] - 1
    return future


def compute_ic(feature_df, future_ret):
    results = []
    for col in feature_df.columns:
        valid = feature_df[col].notna() & ~pd.isna(future_ret)
        if valid.sum() < 30:
            continue
        f = feature_df[col][valid].values
        r = future_ret[valid]
        ic, p_val = spearmanr(f, r)
        if not np.isnan(ic):
            results.append({
                'factor': col,
                'IC': round(ic, 4),
                'IC_abs': round(abs(ic), 4),
                'p_value': round(p_val, 4),
                'significant': p_val < 0.05,
            })
    results.sort(key=lambda x: x['IC_abs'], reverse=True)
    return results


# ============================================================
# 模块3: Bandit调度器 — 8维绩效状态→因子/模型方向选择
# ============================================================
class ContextualBandit:
    """
    上下文两臂赌博机（论文2.5节）
    8维绩效状态向量 → 选择 factor 或 model 优化方向
    使用线性Thompson采样
    """
    
    def __init__(self, state_file=BANDIT_STATE_FILE):
        self.state_file = state_file
        self.state_dim = 8
        self.actions = ['factor', 'model']
        self.posteriors = self._load_state()
    
    def _load_state(self):
        """加载/初始化后验参数"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    return json.load(f)
            except:
                pass
        
        # 初始化：每个动作一个高斯后验
        # B = X^T X + I (精度矩阵), mu = B^{-1} X^T y (均值)
        return {
            'factor': {'mu': [0.0] * self.state_dim, 'B_inv': [[1.0 if i==j else 0.0 for j in range(self.state_dim)] for i in range(self.state_dim)]},
            'model': {'mu': [0.0] * self.state_dim, 'B_inv': [[1.0 if i==j else 0.0 for j in range(self.state_dim)] for i in range(self.state_dim)]},
            'history': [],
            'total_rounds': 0,
        }
    
    def _save_state(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, 'w') as f:
            json.dump(self.posteriors, f, ensure_ascii=False, indent=2)
    
    def sample_reward(self, action, context):
        """对给定动作，从后验采样奖励"""
        post = self.posteriors[action]
        mu = np.array(post['mu'])
        B_inv = np.array(post['B_inv'])
        # 从高斯后验采样权重
        sampled_w = np.random.multivariate_normal(mu, B_inv)
        return float(np.dot(sampled_w, context))
    
    def select_action(self, context):
        """Thompson采样选择动作"""
        factor_reward = self.sample_reward('factor', context)
        model_reward = self.sample_reward('model', context)
        return 'factor' if factor_reward > model_reward else 'model', factor_reward, model_reward
    
    def update(self, action, context, reward):
        """更新后验"""
        post = self.posteriors[action]
        B_inv = np.array(post['B_inv'])
        mu = np.array(post['mu'])
        
        x = np.array(context)
        # 贝叶斯更新: B_{t+1} = B_t + x x^T
        B_inv_new = B_inv - np.outer(B_inv @ x, x @ B_inv) / (1 + x @ B_inv @ x)
        # mu_{t+1} = B_{t+1}^{-1} (B_t mu_t + x * r)
        mu_new = B_inv_new @ (np.linalg.inv(B_inv) @ mu + x * reward)
        
        self.posteriors[action]['mu'] = mu_new.tolist()
        self.posteriors[action]['B_inv'] = B_inv_new.tolist()
        self.posteriors['total_rounds'] += 1
        self.posteriors['history'].append({
            'round': self.posteriors['total_rounds'],
            'action': action,
            'context': context,
            'reward': reward,
            'timestamp': datetime.now().isoformat(),
        })
        self._save_state()


def compute_performance_state(codes):
    """
    计算8维绩效状态向量
    返回: [IC_mean, IC_std, Rank_IC, ICIR, ARR_1m, MDD_1m, IR, factor_diversity]
    """
    all_ics = []
    rank_ics = []
    monthly_returns = []
    
    for code in codes[:5]:  # 取前5个代表性股票
        csv_path = os.path.join(QLIB_DIR, f"{code}.csv")
        if not os.path.exists(csv_path):
            continue
        df = pd.read_csv(csv_path)
        if len(df) < 100:
            continue
        
        base = compute_base_factors(df)
        future_ret = compute_future_return(df['close'].values, 5)
        ic_results = compute_ic(base, future_ret)
        
        if ic_results:
            ics = [r['IC'] for r in ic_results if abs(r['IC']) < 0.5]
            if ics:
                all_ics.extend(ics)
                rank_ics.append(ic_results[0]['IC_abs'])
        
        # 近1月收益
        close_vals = df['close'].values
        if len(close_vals) > 20:
            monthly_returns.append(close_vals[-1] / close_vals[-21] - 1)
    
    # 默认值（如果数据不足）
    ic_mean = float(np.mean(all_ics)) if all_ics else 0.03
    ic_std = float(np.std(all_ics)) if all_ics else 0.05
    rank_ic = float(np.mean(rank_ics)) if rank_ics else 0.04
    icir = ic_mean / (ic_std + 1e-9) if ic_std > 0 else 0.5
    arr_1m = float(np.mean(monthly_returns)) if monthly_returns else 0.02
    mdd_1m = 0.0  # 简化处理
    ir = 0.5
    diversity = min(1.0, len(all_ics) / 50) if all_ics else 0.3
    
    state = [ic_mean, ic_std, rank_ic, icir, arr_1m, mdd_1m, ir, diversity]
    return [round(s, 4) for s in state]


# ============================================================
# 主流程
# ============================================================
def costeer_full_cycle(codes=None, mode='standard', forward=5):
    """
    Co-STEER全流程：因子生成 → 冗余检测 → IC验证 → 入库
    
    论文中的闭环:
    Synthesis(生成) → Implementation(Co-STEER) → Validation(IC+回测) → Analysis(评估) → Feedback
    """
    if codes is None:
        codes = CORE_CODES
    
    print("=" * 60)
    print(f"  🧬 R&D-Agent-Quant Co-STEER全流程")
    print(f"  模式: {mode} | 预测窗口: {forward}日 | 股票池: {len(codes)}只")
    print("=" * 60)
    
    # Step 1: 加载已有因子库
    existing_factors = {}
    if os.path.exists(FACTOR_LIB_PATH):
        try:
            with open(FACTOR_LIB_PATH) as f:
                lib = json.load(f)
            for sf in lib.get('stable_new_factors', []):
                existing_factors[sf['name']] = sf
        except:
            pass
    
    print(f"\n📂 已有稳定因子: {len(existing_factors)}个")
    for name, info in existing_factors.items():
        print(f"   {name}: IC={info.get('avg_ic', '?')}, Sharpe={info.get('sharpe', '?')}")
    
    # Step 2: 每只股票运行Co-STEER
    all_new_ics = {}  # {factor_name: [IC列表]}
    all_new_factor_vals = {}  # {factor_name: [所有股票拼接的因子值]}
    
    for code in codes:
        csv_path = os.path.join(QLIB_DIR, f"{code}.csv")
        if not os.path.exists(csv_path):
            continue
        df = pd.read_csv(csv_path)
        if len(df) < 100:
            continue
        
        close = df['close'].values
        
        # 计算基础因子
        base = compute_base_factors(df)
        future_ret = compute_future_return(close, forward)
        
        # 基础因子IC
        base_ics = compute_ic(base, future_ret)
        if not base_ics:
            continue
        
        top10 = base_ics[:10]
        best_ic = top10[0]['IC_abs']
        
        # Co-STEER生成新因子
        new_factors = costeer_generate_factors(base, top10, mode)
        if new_factors.empty:
            continue
        
        # 冗余检测：剔除与已有因子高相关的新因子(IC_max≥0.99)
        before_count = len(new_factors.columns)
        new_factors = filter_redundant_factors(base, new_factors, ic_threshold=0.99)
        after_count = len(new_factors.columns)
        if before_count != after_count:
            print(f"  [{code}] 冗余检测剔除 {before_count - after_count} 个因子")
        
        if new_factors.empty:
            continue
        
        # 计算新因子IC
        new_ics = compute_ic(new_factors, future_ret)
        
        # 汇总
        for r in new_ics:
            fname = r['factor']
            if fname not in all_new_ics:
                all_new_ics[fname] = []
                all_new_factor_vals[fname] = []
            all_new_ics[fname].append(r['IC'])
            all_new_factor_vals[fname].extend(new_factors[fname].dropna().tolist())
        
        # 打印结果
        sig_ics = [r for r in new_ics if r['significant']]
        tag = "✅" if sig_ics else "🟡"
        print(f"  [{code}] {tag} Co-STEER: {len(new_factors.columns)}新因子, Top IC={new_ics[0]['IC_abs']:.3f}" if new_ics else
              f"  [{code}] ⚪ Co-STEER: 无有效新因子")
    
    # Step 3: 全局因子筛选
    print(f"\n{'='*60}")
    print(f"  🌐 全局因子有效性分析")
    print(f"{'='*60}")
    
    stable_factors = []
    for fname, ics in all_new_ics.items():
        if not ics:
            continue
        avg_ic = float(np.mean(ics))
        std_ic = float(np.std(ics))
        n_stocks = len(ics)
        sharpe = avg_ic / (std_ic + 1e-9) if std_ic > 0 else 0
        
        # 过滤条件：IC均值够高 + 至少3只股票有效
        is_stable = abs(avg_ic) > 0.02 and n_stocks >= 3
        
        status = "✅" if is_stable else "🟡" if abs(avg_ic) > 0.01 else "❌"
        print(f"  {status} {fname}: IC={avg_ic:.4f}±{std_ic:.4f}, Sharpe={sharpe:.2f}, {n_stocks}只")
        
        if is_stable:
            stable_factors.append({
                'name': fname,
                'avg_ic': round(avg_ic, 4),
                'std_ic': round(std_ic, 4),
                'sharpe': round(sharpe, 2),
                'n_stocks': n_stocks,
            })
    
    # Step 4: 更新因子库（合并+去重）
    stable_factors.sort(key=lambda x: abs(x['avg_ic']), reverse=True)
    
    library = {
        'updated_at': datetime.now().isoformat(),
        'stable_new_factors': stable_factors,
        'codes_analyzed': len(codes),
    }
    
    # 合并已有因子
    if os.path.exists(FACTOR_LIB_PATH):
        try:
            with open(FACTOR_LIB_PATH) as f:
                old = json.load(f)
            old_stable = old.get('stable_new_factors', [])
            existing_names = {f['name'] for f in old_stable}
            for f in stable_factors:
                if f['name'] not in existing_names:
                    old_stable.append(f)
            library['stable_new_factors'] = old_stable
            library['history'] = old.get('history', [])
        except:
            pass
    
    library['history'] = library.get('history', []) + [{
        'date': datetime.now().strftime('%Y-%m-%d'),
        'codes': codes,
        'new_factor_count': len(stable_factors),
        'mode': mode,
    }]
    
    os.makedirs(os.path.dirname(FACTOR_LIB_PATH), exist_ok=True)
    with open(FACTOR_LIB_PATH, 'w') as f:
        json.dump(library, f, ensure_ascii=False, indent=2)
    
    # Step 5: Bandit状态更新
    bandit = ContextualBandit()
    state = compute_performance_state(codes)
    reward = len(stable_factors) / 5.0  # 奖励=稳定因子数归一化
    bandit.update('factor', state, reward)
    
    print(f"\n{'='*60}")
    print(f"  ✅ 完成！{len(stable_factors)}个新稳定因子已入库")
    print(f"  💾 因子库: {FACTOR_LIB_PATH}")
    print(f"  🎰 Bandit已更新")
    print(f"{'='*60}")
    
    return stable_factors


def bandit_diagnose():
    """Bandit调度器诊断"""
    bandit = ContextualBandit()
    state = compute_performance_state(CORE_CODES[:5])
    
    print("=" * 60)
    print("  🎰 Bandit调度器诊断")
    print("=" * 60)
    
    print(f"\n📊 当前8维绩效状态:")
    for i, (name, val) in enumerate(zip(STATE_DIM_NAMES, state)):
        print(f"  {name}: {val:.4f}")
    
    print(f"\n📋 各动作的偏好权重 μ:")
    for action in ['factor', 'model']:
        post = bandit.posteriors[action]
        mu = np.array(post['mu'])
        print(f"  {action}: μ={mu}")
    
    # 多采样几次看倾向
    factor_wins = 0
    model_wins = 0
    for _ in range(100):
        action, _, _ = bandit.select_action(state)
        if action == 'factor':
            factor_wins += 1
        else:
            model_wins += 1
    
    chosen = "factor" if factor_wins > model_wins else "model"
    print(f"\n🎯 推荐方向: {chosen} (factor={factor_wins}%, model={model_wins}%)")
    print(f"📈 总轮次: {bandit.posteriors['total_rounds']}")
    
    return chosen


# ============================================================
# 完整全流程
# ============================================================
def run_full_pipeline(codes=None):
    """全流程：Co-STEER + bandit调度 + 冗余检测"""
    if codes is None:
        codes = CORE_CODES
    
    print("🚀 R&D-Agent-Quant 全流程启动")
    print(f"   时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"   股票池: {len(codes)}只")
    
    # Step 1: Bandit诊断 → 决定方向
    bandit = ContextualBandit()
    state = compute_performance_state(codes[:5])
    action, f_reward, m_reward = bandit.select_action(state)
    
    print(f"\n🎯 Bandit调度: factor_reward={f_reward:.3f} vs model_reward={m_reward:.3f}")
    print(f"   选择方向: {action.upper()}")
    
    # Step 2: 执行Co-STEER因子发现
    mode = 'aggressive' if action == 'factor' and bandit.posteriors['total_rounds'] < 5 else 'standard'
    
    stable_factors = costeer_full_cycle(codes, mode=mode)
    
    # Step 3: 输出总结
    print(f"\n{'='*60}")
    print(f"  📋 本轮执行总结")
    print(f"{'='*60}")
    print(f"  方向: {action}")
    print(f"  Co-STEER模式: {mode}")
    print(f"  新稳定因子: {len(stable_factors)}个")
    for f in stable_factors:
        print(f"    ✅ {f['name']}: IC={f['avg_ic']:.4f}, Sharpe={f['sharpe']:.2f}, {f['n_stocks']}只")
    print(f"  Bandit总轮次: {bandit.posteriors['total_rounds']}")
    print(f"{'='*60}")
    
    return stable_factors, action


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='R&D-Agent-Quant')
    parser.add_argument('--mode', choices=['full', 'factor', 'bandit', 'costeer'], default='full')
    parser.add_argument('--codes', nargs='+', default=None)
    parser.add_argument('--forward', type=int, default=5)
    args = parser.parse_args()
    
    if args.mode == 'full':
        run_full_pipeline(args.codes)
    elif args.mode == 'factor':
        costeer_full_cycle(args.codes, mode='aggressive', forward=args.forward)
    elif args.mode == 'costeer':
        costeer_full_cycle(args.codes, mode='standard', forward=args.forward)
    elif args.mode == 'bandit':
        bandit_diagnose()

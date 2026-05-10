#!/config/quant_env/bin/python3
"""
RD-Agent 因子自动发现循环 v2.0
全自动：LightGBM选Top因子 → 自动组合生成 → IC验证 → 保留有效 → 注入PPO环境

每周运行一次，更新因子库 /config/qlib_data/factor_library.json
"""
import sys, os, json, warnings
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
warnings.filterwarnings("ignore")

os.chdir('/config/quant_scripts')
sys.path.insert(0, '.')

FACTOR_LIBRARY_PATH = '/config/qlib_data/factor_library.json'
QLIB_DIR = '/config/qlib_data/features'

# ===== 基础因子计算器 =====
def compute_base_factors(df):
    """计算28个基础因子，返回DataFrame"""
    df = df.copy()
    close = df['close'].values
    volume = df['volume'].values
    high = df['high'].values
    low = df['low'].values
    amount = df['amount'].values
    n = len(df)
    
    features = {}
    
    # 1. 均线偏离 MA_Dev (5/10/20/30/60)
    for p in [5, 10, 20, 30, 60]:
        ma = pd.Series(close).rolling(p).mean().values
        features[f'MA_Dev_{p}'] = (close - ma) / (ma + 1e-9)
    
    # 2. 量比 Vol_Ratio (5/10/20)
    for p in [5, 10, 20]:
        vol_ma = pd.Series(volume).rolling(p).mean().values
        features[f'Vol_Ratio_{p}'] = volume / (vol_ma + 1e-9)
    
    # 3. 波动幅度 Range (5/10/20)
    for p in [5, 10, 20]:
        features[f'Range_{p}'] = (pd.Series(high).rolling(p).max() - pd.Series(low).rolling(p).min()).values / (close + 1e-9)
    
    # 4. 收益率 Ret (1/3/5/10/20)
    for p in [1, 3, 5, 10, 20]:
        features[f'Ret_{p}'] = pd.Series(close).pct_change(p).values
    
    # 5. 价格位置 Price_Pos (10/20)
    for p in [10, 20]:
        hh = pd.Series(high).rolling(p).max().values
        ll = pd.Series(low).rolling(p).min().values
        features[f'Price_Pos_{p}'] = (close - ll) / (hh - ll + 1e-9)
    
    # 6. 价量相关性 PV_Corr (5/10/20)
    for p in [5, 10, 20]:
        features[f'PV_Corr_{p}'] = pd.Series(close).rolling(p).corr(pd.Series(volume)).values
    
    # 7. 成交量变化率 Vol_Chg (1/3/5)
    for p in [1, 3, 5]:
        features[f'Vol_Chg_{p}'] = pd.Series(volume).pct_change(p).values
    
    # 8. VWAP偏离
    vwap = (high + low + close) / 3
    vwap_ma = pd.Series(vwap * volume).rolling(20).sum().values / (pd.Series(volume).rolling(20).sum().values + 1e-9)
    features['VWAP_Dev'] = (close - vwap_ma) / (vwap_ma + 1e-9)
    
    # 9. 量价比率
    features['Vol_Amount_Ratio'] = volume / (amount + 1e-9)
    
    # 10. 换手率变化（如果有换手率列）
    if 'turnover' in df.columns:
        turnover = df['turnover'].values
        for p in [5, 20]:
            features[f'Turnover_Chg_{p}'] = pd.Series(turnover).pct_change(p).values
    
    return pd.DataFrame(features)


def compute_future_return(close, forward=5):
    """计算未来N日收益（标签）"""
    n = len(close)
    future = np.full(n, np.nan)
    for i in range(n - forward):
        future[i] = close[i + forward] / close[i] - 1
    return future


def compute_ic(feature_df, future_ret):
    """计算所有因子的IC（秩相关系数）"""
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
                'direction': 'positive' if ic > 0 else 'negative',
            })
    results.sort(key=lambda x: x['IC_abs'], reverse=True)
    return results


def auto_generate_factors(base_factors, top_ics):
    """
    自动生成新组合因子（RD-Agent核心逻辑）
    
    基于Top因子的特征，自动组合：
    - 动量+波动: Ret + Range
    - 趋势加速: MA_Dev_5 - MA_Dev_20
    - 量价背离: Ret - Vol_Ratio
    - 动量减速: Ret_5 - Ret_20
    - 波动缩放: Range * Vol_Ratio
    """
    new_factors = {}
    
    # 获取Top因子名（前5）
    top_names = [r['factor'] for r in top_ics[:5]]
    
    # 自动检测可用因子类
    has_ret = any('Ret_' in n for n in top_names)
    has_range = any('Range_' in n for n in top_names)
    has_ma = any('MA_Dev_' in n for n in top_names)
    has_vol = any('Vol_Ratio_' in n for n in top_names)
    has_pv = any('PV_Corr_' in n for n in top_names)
    
    # 组合1: 动量+波动（如果两者都是Top因子）
    if has_ret and has_range:
        ret_cols = [c for c in base_factors.columns if 'Ret_' in c]
        range_cols = [c for c in base_factors.columns if 'Range_' in c]
        if ret_cols and range_cols:
            new_factors['RD_Ret5_Range10'] = base_factors['Ret_5'] + base_factors['Range_10']
    
    # 组合2: 趋势加速（短期-中期偏离）
    if has_ma:
        if 'MA_Dev_5' in base_factors.columns and 'MA_Dev_20' in base_factors.columns:
            new_factors['RD_MADev5_MADev20'] = base_factors['MA_Dev_5'] - base_factors['MA_Dev_20']
    
    # 组合3: 量价背离（价格涨但量缩=背离）
    if has_ret and has_vol:
        if 'Ret_5' in base_factors.columns and 'Vol_Ratio_5' in base_factors.columns:
            new_factors['RD_Divergence'] = base_factors['Ret_5'] - base_factors['Vol_Ratio_5'] * 0.5
    
    # 组合4: 动量加速（5日收益 - 20日收益×权重）
    if 'Ret_5' in base_factors.columns and 'Ret_20' in base_factors.columns:
        new_factors['RD_Momentum_Accel'] = base_factors['Ret_5'] - base_factors['Ret_20'] * 0.3
    
    # 组合5: 价量趋势（PV_Corr × MA_Dev — 价量协同的方向性）
    if has_pv and has_ma:
        if 'PV_Corr_5' in base_factors.columns and 'MA_Dev_5' in base_factors.columns:
            new_factors['RD_PV_Trend'] = base_factors['PV_Corr_5'] * base_factors['MA_Dev_5']
    
    # 组合6: 波动缩放（放量+高波动）
    if has_range and has_vol:
        if 'Range_5' in base_factors.columns and 'Vol_Ratio_5' in base_factors.columns:
            new_factors['RD_Vol_Scaled'] = base_factors['Range_5'] * base_factors['Vol_Ratio_5']
    
    return pd.DataFrame(new_factors)


def run_factor_discovery(codes, forward=5):
    """
    全自动因子发现流程
    
    返回:
        all_results: dict {code: top_ics列表}
        best_new_factors: list of (factor_name, avg_ic) 全局最佳新因子
    """
    print("=" * 60)
    print("  RD-Agent 因子自动发现循环 v2.0")
    print("=" * 60)
    
    all_results = {}
    new_factor_ics = {}  # {factor_name: [IC值列表]}
    
    for code in codes:
        # 读取数据
        csv_path = os.path.join(QLIB_DIR, f"{code}.csv")
        if not os.path.exists(csv_path):
            print(f"  ⏭️ {code}: 无数据")
            continue
        
        df = pd.read_csv(csv_path)
        if len(df) < 100:
            print(f"  ⏭️ {code}: 数据不足({len(df)}行)")
            continue
        
        print(f"\n  📊 {code}: {len(df)}行")
        
        # 计算基础因子
        base = compute_base_factors(df)
        future_ret = compute_future_return(df['close'].values, forward)
        
        # 计算IC
        ic_results = compute_ic(base, future_ret)
        if not ic_results:
            print(f"  ⚠️ {code}: 无有效因子")
            continue
        
        top10 = ic_results[:10]
        best_ic = top10[0]['IC_abs'] if top10 else 0
        
        print(f"  🏆 Top因子: {top10[0]['factor']} (IC={top10[0]['IC_abs']:.3f}, p={top10[0]['p_value']:.3f})")
        
        # 自动生成新因子
        new_factors = auto_generate_factors(base, top10)
        if not new_factors.empty:
            new_ics = compute_ic(new_factors, future_ret)
            print(f"  🧬 生成 {len(new_factors.columns)} 个新因子:")
            for r in new_ics:
                tag = "✅" if abs(r['IC']) > 0.03 else "🟡" if abs(r['IC']) > 0.01 else "❌"
                print(f"    {tag} {r['factor']}: IC={r['IC']:.3f} p={r['p_value']:.3f}")
                
                if r['factor'] not in new_factor_ics:
                    new_factor_ics[r['factor']] = []
                new_factor_ics[r['factor']].append(r['IC'])
        
        all_results[code] = {
            'top_ics': top10,
            'best_ic': best_ic,
            'best_factor': top10[0]['factor'],
            'new_factors': [r['factor'] for r in new_ics] if 'new_ics' in dir() else [],
        }
    
    # ===== 全局分析：哪些新因子在多数股票上有效？ =====
    print(f"\n{'='*60}")
    print("  🌐 全局因子有效性分析")
    print(f"{'='*60}")
    
    stable_factors = []
    for fname, ics in new_factor_ics.items():
        avg_ic = np.mean(ics)
        std_ic = np.std(ics)
        n_stocks = len(ics)
        sharpe = avg_ic / (std_ic + 1e-9)
        
        print(f"  {fname}: IC均值={avg_ic:.3f}±{std_ic:.3f}, Sharpe={sharpe:.2f}, 适用{n_stocks}只")
        
        if abs(avg_ic) > 0.015 and n_stocks >= 3:
            stable_factors.append({
                'name': fname,
                'avg_ic': round(avg_ic, 4),
                'std_ic': round(std_ic, 4),
                'sharpe': round(sharpe, 2),
                'n_stocks': n_stocks,
            })
    
    stable_factors.sort(key=lambda x: abs(x['avg_ic']), reverse=True)
    
    print(f"\n  ✅ 高稳定性新因子 ({len(stable_factors)}个):")
    for f in stable_factors:
        print(f"    {f['name']}: IC={f['avg_ic']:.3f}, Sharpe={f['sharpe']:.2f}, {f['n_stocks']}只")
    
    # ===== 保存因子库 =====
    library = {
        'updated_at': pd.Timestamp.now().isoformat(),
        'stable_new_factors': stable_factors,
        'codes_analyzed': len(codes),
    }
    
    # 合并已有因子库
    if os.path.exists(FACTOR_LIBRARY_PATH):
        try:
            with open(FACTOR_LIBRARY_PATH) as f:
                old = json.load(f)
            old_stable = old.get('stable_new_factors', [])
            # 合并去重
            existing_names = {f['name'] for f in old_stable}
            for f in stable_factors:
                if f['name'] not in existing_names:
                    old_stable.append(f)
            library['stable_new_factors'] = old_stable
            library['history'] = old.get('history', [])
        except:
            pass
    
    library['history'] = library.get('history', []) + [{
        'date': pd.Timestamp.now().strftime('%Y-%m-%d'),
        'codes': codes,
        'new_factor_count': len(stable_factors),
    }]
    
    os.makedirs(os.path.dirname(FACTOR_LIBRARY_PATH), exist_ok=True)
    with open(FACTOR_LIBRARY_PATH, 'w') as f:
        json.dump(library, f, ensure_ascii=False, indent=2)
    print(f"\n  💾 因子库已保存: {FACTOR_LIBRARY_PATH}")
    
    return all_results, stable_factors


def inject_into_environment(stable_factors, train_df):
    """把高稳定性因子注入训练数据（添加列）"""
    # 因子已经在 finrl_astock_data.py 的 add_technical_indicators 中预定义了
    # 这里只需确认新因子是否被包含
    current_rd_cols = ['rd_ret5_range10', 'rd_madev5_madev20', 'rd_divergence', 'rd_momentum_accel']
    stable_names = [f['name'] for f in stable_factors]
    
    missing = [f for f in stable_names if f'rd_{f.lower()}' not in current_rd_cols]
    if missing:
        print(f"\n  ⚠️ 新因子待注入环境: {missing}")
        print(f"  需要在 finrl_astock_data.py 的 add_technical_indicators 中添加计算逻辑")
    else:
        print(f"\n  ✅ 所有高频因子已在环境中")
    
    return missing


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='RD-Agent因子自动发现')
    parser.add_argument('--codes', nargs='+', default=[
        '002594', '600519', '000001', '000333', '300750', 
        '002415', '002475', '518880', '000858', '600887'
    ], help='股票代码列表')
    parser.add_argument('--forward', type=int, default=5, help='预测未来N天')
    args = parser.parse_args()
    
    results, stable = run_factor_discovery(args.codes, args.forward)
    
    print(f"\n{'='*60}")
    print(f"  分析完成! {len(results)}只股票, {len(stable)}个稳定新因子")
    print(f"{'='*60}")
    
    # 打印每只股票的Top3因子
    print(f"\n📋 每只股票Top3因子:")
    for code, r in results.items():
        top3 = r['top_ics'][:3]
        factors_str = ' | '.join(f"{f['factor']}(IC={f['IC_abs']:.3f})" for f in top3)
        print(f"  {code}: {factors_str}")

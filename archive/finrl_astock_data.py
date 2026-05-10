#!/config/quant_env/bin/python3
"""
FinRL_DeepSeek A股适配 — 数据层
批量获取A股历史K线 + 计算技术指标 + 适配FinRL环境格式
"""
import sys
import os
sys.path.insert(0, '/config/quant_scripts')

import pandas as pd
import numpy as np
import pandas_ta as ta
import time
from datetime import datetime, timedelta
from eastmoney_data import get_quote, get_klines, _rate_limit

# ====== FinRL标准技术指标集（从FinRL_DeepSeek提取）======
INDICATORS = ['macd', 'boll_ub', 'boll_lb', 'rsi_30', 'cci_30', 'dx_30',
              'close_30_sma', 'close_60_sma']


def fetch_astock_data(codes, start_date=None, end_date=None, days=365):
    """
    批量获取A股/ETF历史K线（三通道：本地CSV > API直连 > akshare备降）
    """
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")
    if not start_date:
        # 默认取全部可用数据
        start_date = "2023-01-01"
    
    qlib_data_dir = '/config/qlib_data/features'
    
    all_dfs = []
    for code in codes:
        print(f"  获取 {code} K线...")
        klines = None
        
        # 通道1：本地Qlib CSV数据（最优：完整历史+免API）
        csv_path = os.path.join(qlib_data_dir, f"{code}.csv")
        if os.path.exists(csv_path):
            try:
                df_local = pd.read_csv(csv_path)
                df_local['date'] = pd.to_datetime(df_local['date'].astype(str), errors='coerce')
                df_local = df_local.dropna(subset=['date'])
                df_local = df_local.sort_values('date')
                
                if len(df_local) > 20:
                    klines = []
                    for _, row in df_local.iterrows():
                        klines.append({
                            '日期': str(row['date']),
                            '开盘': float(row.get('open', row.get('开盘', 0))),
                            '收盘': float(row.get('close', row.get('收盘', 0))),
                            '最高': float(row.get('high', row.get('最高', 0))),
                            '最低': float(row.get('low', row.get('最低', 0))),
                            '成交量': int(float(row.get('volume', row.get('成交量(手)', 0)))),
                            '成交额': float(row.get('amount', row.get('成交额(万元)', 0))) * 10000,
                            '涨跌幅': float(row.get('pct_change', row.get('涨跌幅(%)', 0))),
                            '换手率': float(row.get('turnover', row.get('换手率', 0))),
                        })
                    print(f"    本地CSV: {len(klines)}条 ({df_local['date'].min().date()} ~ {df_local['date'].max().date()})")
            except Exception as e:
                print(f"    本地CSV读取失败: {str(e)[:60]}")
        
        # 通道2：东方财富API直连（本地无数据时的备降）
        if not klines or len(klines) < 20:
            klines = get_klines(code)
            if klines and len(klines) >= 20:
                print(f"    API直连: {len(klines)}条")
            else:
                print(f"    {code}: 数据不足，跳过")
                continue
        
        
        df = pd.DataFrame(klines)
        df.rename(columns={
            '日期': 'date', '开盘': 'open', '收盘': 'close',
            '最高': 'high', '最低': 'low', '成交量': 'volume',
            '成交额': 'amount', '涨跌幅': 'pct_chg'
        }, inplace=True)
        
        # 添加股票代码列（FinRL要求的tic列）
        df['tic'] = code
        
        # 统一日期格式
        df['date'] = pd.to_datetime(df['date'])
        
        all_dfs.append(df)
        _rate_limit()  # 防限流
    
    if not all_dfs:
        return pd.DataFrame()
    
    result = pd.concat(all_dfs, ignore_index=True)
    result.sort_values(['date', 'tic'], inplace=True)
    result.reset_index(drop=True, inplace=True)
    
    print(f"  共获取 {len(result)} 条记录, {result['tic'].nunique()} 只股票")
    return result


def add_technical_indicators(df):
    """
    添加FinRL标准技术指标集
    
    输入: 必须有 close, high, low, volume 列
    输出: 新增 INDICATORS 中各列
    """
    df = df.copy()
    
    # 1. MACD
    macd = ta.macd(df['close'])
    if macd is not None and len(macd.columns) >= 3:
        df['macd'] = macd.iloc[:, 0]
    
    # 2. Bollinger Bands
    boll = ta.bbands(df['close'], length=20)
    if boll is not None and len(boll.columns) >= 3:
        df['boll_ub'] = boll.iloc[:, 2]  # upper band
        df['boll_lb'] = boll.iloc[:, 0]  # lower band
    
    # 3. RSI
    df['rsi_30'] = ta.rsi(df['close'], length=30)
    
    # 4. CCI
    df['cci_30'] = ta.cci(df['high'], df['low'], df['close'], length=30)
    
    # 5. ADX (代替DX，pandas_ta中用adx)
    adx = ta.adx(df['high'], df['low'], df['close'], length=30)
    if adx is not None:
        df['dx_30'] = adx.iloc[:, 0] if adx.shape[1] >= 1 else adx  # ADX值
    
    # 6. SMA
    df['close_30_sma'] = ta.sma(df['close'], length=30)
    df['close_60_sma'] = ta.sma(df['close'], length=60)
    
    # 7. 成交量相关（可选，增强特征）
    df['volume_ma_20'] = ta.sma(df['volume'], length=20)
    df['volume_ratio'] = df['volume'] / df['volume_ma_20'].replace(0, np.nan)
    
    # 8. 价格位置特征
    for period in [10, 20, 60]:
        roll_max = df['close'].rolling(period).max()
        roll_min = df['close'].rolling(period).min()
        df[f'price_pos_{period}'] = (df['close'] - roll_min) / (roll_max - roll_min + 1e-8)

    # ===== 9. RD-Agent Alpha因子（已验证有效的组合因子）=====
    # RD_Ret5_Range10: 短期动量+波动幅度组合（比亚迪IC=0.088 p=0.05）
    ret5 = df['close'].pct_change(5)
    range5 = (df['high'].rolling(5).max() - df['low'].rolling(5).min()) / df['close'].replace(0, np.nan)
    df['rd_ret5_range10'] = ret5 + range5.rolling(10).mean()
    # RD_MADev5_MADev20: 短期偏离-中期偏离 = 趋势加速/减速
    ma5 = df['close'].rolling(5).mean()
    ma20 = df['close'].rolling(20).mean()
    df['rd_madev5_madev20'] = (df['close'] - ma5) / ma5.replace(0, np.nan) - (df['close'] - ma20) / ma20.replace(0, np.nan)
    # RD_Divergence: 量价背离检测（涨但缩量=背离信号）
    vol_ratio5 = df['volume'] / df['volume'].rolling(5).mean().replace(0, np.nan)
    df['rd_divergence'] = ret5 - vol_ratio5 * 0.5
    # RD_Momentum_Accel: 短期动量加速
    ret20 = df['close'].pct_change(20)
    df['rd_momentum_accel'] = ret5 - ret20 * 0.3

    return df


def split_train_trade(df, train_ratio=0.7):
    """按时间分割训练集和交易（测试）集"""
    df = df.sort_values('date')
    dates = df['date'].unique()
    split_idx = int(len(dates) * train_ratio)
    train_dates = dates[:split_idx]
    trade_dates = dates[split_idx:]
    
    train_df = df[df['date'].isin(train_dates)].copy()
    trade_df = df[df['date'].isin(trade_dates)].copy()
    
    print(f"  分割: 训练 {train_df['date'].nunique()} 天, 交易 {trade_df['date'].nunique()} 天")
    return train_df, trade_df


def prepare_finrl_data(codes, days=365, with_indicators=True):
    """
    一站式准备FinRL可用的A股数据
    
    用法:
        df = prepare_finrl_data(['002594', '600519', '000001'], days=365)
    """
    print(f"\n===== 准备A股数据: {codes} =====")
    
    # 1. 获取K线
    df = fetch_astock_data(codes, days=days)
    if df.empty:
        print("❌ 无数据")
        return None, None
    
    # 2. 添加技术指标
    if with_indicators:
        print("  计算技术指标...")
        df = add_technical_indicators(df)
    
    # 3. 去空值
    before = len(df)
    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.dropna(subset=['close', 'open', 'high', 'low', 'volume'])
    
    # 填充RD因子和技术指标的NaN（前几行rolling计算不出）
    rd_cols = [c for c in df.columns if c.startswith('rd_')]
    indicator_cols = [c for c in INDICATORS if c in df.columns]
    fill_cols = rd_cols + indicator_cols
    for c in fill_cols:
        if c in df.columns:
            df[c] = df[c].fillna(0)
    
    after = len(df)
    if before > after:
        print(f"  删除 {before-after} 行空值")
    
    # 4. 分割
    train_df, trade_df = split_train_trade(df, train_ratio=0.7)
    
    print(f"  完成! 训练集 {len(train_df)} 行, 交易集 {len(trade_df)} 行")
    return train_df, trade_df


if __name__ == "__main__":
    # 测试：获取比亚迪+中天+紫光的年度数据
    train, trade = prepare_finrl_data(['002594', '600522', '000938'], days=180)
    
    if train is not None:
        print(f"\n训练集列: {list(train.columns)}")
        print(f"技术指标示例:")
        for col in INDICATORS:
            if col in train.columns:
                val = train[col].dropna().iloc[-1] if not train[col].dropna().empty else 'N/A'
                print(f"  {col}: {val:.4f}" if isinstance(val, float) else f"  {col}: {val}")
        
        print(f"\n交易集日期范围: {trade['date'].min()} ~ {trade['date'].max()}")
        print(f"交易集股票: {trade['tic'].unique()}")

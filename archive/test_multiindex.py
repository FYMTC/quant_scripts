#!/usr/bin/env python3
"""快速测试vectorbt的MultiIndex列访问方式"""
import sys
sys.path.insert(0, '/config/quant_scripts')
import pandas as pd
import numpy as np
import vectorbt as vbt
import warnings
warnings.filterwarnings("ignore")

# 加载数据
df = pd.read_csv("/config/qlib_data/features/002594.csv")
df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
close = df.set_index("date").sort_index()["close"]

fast_w = np.arange(3, 21, 2)
slow_w = np.arange(20, 61, 5)

fast_ma = vbt.MA.run(close, fast_w)
slow_ma = vbt.MA.run(close, slow_w)
entries = fast_ma.ma_crossed_above(slow_ma)

# 探查列结构
print("列名类型:", type(entries.columns))
print("列名样本:", entries.columns[:3])
print("列名层次:", entries.columns.names)
print("第一层值:", sorted(entries.columns.get_level_values(0).unique()))
print("第二层值:", sorted(entries.columns.get_level_values(1).unique()))
print()

# 访问方式测试
print("entries[40] 测试:", entries[40].shape)  # slow=40
print("entries[40][5] 测试:", entries[40][5].shape)  # slow=40, fast=5

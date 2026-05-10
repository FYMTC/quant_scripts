#!/usr/bin/env python3
"""
RD-Agent 思路实践：LLM驱动新因子发现
用LightGBM验证组合因子是否优于单独的因子
"""
import pandas as pd
import numpy as np
import warnings
import lightgbm as lgb
warnings.filterwarnings("ignore")

print("=" * 55)
print("  RD-Agent思路：LLM驱动新因子发现验证")
print("=" * 55)

# 加载比亚迪数据
df = pd.read_csv("/config/qlib_data/features/002594.csv")
close = df["close"].values
volume = df["volume"].values
high = df["high"].values
low = df["low"].values
n = len(df)

# 计算25个因子
features = {}
for p in [5, 10, 20, 30, 60]:
    ma = pd.Series(close).rolling(p).mean().values
    features[f"MA_Dev_{p}"] = (close - ma) / (ma + 1e-9)
for p in [5, 10, 20]:
    vol_ma = pd.Series(volume).rolling(p).mean().values
    features[f"Vol_Ratio_{p}"] = volume / (vol_ma + 1e-9)
for p in [5, 10, 20]:
    features[f"Range_{p}"] = (pd.Series(high).rolling(p).max() - pd.Series(low).rolling(p).min()).values / (close + 1e-9)
for p in [1, 3, 5, 10, 20]:
    features[f"Ret_{p}"] = pd.Series(close).pct_change(p).values
for p in [10, 20]:
    hh = pd.Series(high).rolling(p).max().values
    ll = pd.Series(low).rolling(p).min().values
    features[f"Price_Pos_{p}"] = (close - ll) / (hh - ll + 1e-9)

X = pd.DataFrame(features).fillna(0).values
names = list(features.keys())

# 标签
y = np.zeros(n)
for i in range(n - 5):
    y[i] = 1 if (close[i + 5] / close[i] - 1) > 0.02 else 0

split = int(n * 0.7)

# 基线模型
model = lgb.LGBMClassifier(n_estimators=100, learning_rate=0.05, max_depth=4, num_leaves=20, random_state=42, verbose=-1)
model.fit(X[:split], y[:split])

imp = model.feature_importances_
top5_idx = np.argsort(imp)[::-1][:5]
print("\n📊 基线模型Top5因子:")
for idx in top5_idx:
    print(f"  {names[idx]}: {imp[idx]}")

baseline_acc = model.score(X[split:], y[split:])
print(f"\n基线准确率: {baseline_acc:.2%}")

# LLM提出的组合因子
print("\n🧪 LLM组合因子验证:")

combos = [
    ("MA_Dev_20 × Vol_Ratio_5", features["MA_Dev_20"] * features["Vol_Ratio_5"]),
    ("Ret_5 + Range_10", features["Ret_5"] + features["Range_10"]),
    ("Price_Pos_20 × MA_Dev_10", features["Price_Pos_20"] * features["MA_Dev_10"]),
    ("MA_Dev_5 - MA_Dev_20", features["MA_Dev_5"] - features["MA_Dev_20"]),
    ("Vol_Ratio_5 × Ret_3", features["Vol_Ratio_5"] * features["Ret_3"]),
]

results = []
for name, combo in combos:
    X2 = np.column_stack([X, combo])
    m = lgb.LGBMClassifier(n_estimators=100, learning_rate=0.05, max_depth=4, num_leaves=20, random_state=42, verbose=-1)
    m.fit(X2[:split], y[:split])
    acc = m.score(X2[split:], y[split:])
    improv = acc - baseline_acc
    results.append((acc, improv, name))
    print(f"  {name}: {acc:.2%} ({improv:+.2%})")

best_name = max(results, key=lambda x: x[0])[2]
best_acc = max(results, key=lambda x: x[0])[0]
print(f"\n🏆 最佳组合因子: {best_name} → {best_acc:.2%} (基线: {baseline_acc:.2%})")
print(f"   提升: {best_acc - baseline_acc:+.2%}")

# 全部组合一起上
print("\n🧪 全部5个组合因子一起加入:")
X_all = np.column_stack([X] + [c[1] for c in combos])
m_all = lgb.LGBMClassifier(n_estimators=100, learning_rate=0.05, max_depth=4, num_leaves=20, random_state=42, verbose=-1)
m_all.fit(X_all[:split], y[:split])
all_acc = m_all.score(X_all[split:], y[split:])
print(f"  基线: {baseline_acc:.2%} → 全部组合: {all_acc:.2%} ({all_acc - baseline_acc:+.2%})")

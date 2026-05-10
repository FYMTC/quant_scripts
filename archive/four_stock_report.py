#!/config/quant_env/bin/python3
import sys, os, json, warnings
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
warnings.filterwarnings("ignore")

os.chdir("/config/quant_scripts")
sys.path.insert(0, ".")

QLIB_DIR = "/config/qlib_data/features"
CODES = {"000938":"紫光股份","512480":"半导体ETF","002594":"比亚迪","518880":"黄金ETF"}
TORDER = ["000938","512480","002594","518880"]

def compute_factors(df):
    c,h,l,v = df["close"].values, df["high"].values, df["low"].values, df["volume"].values
    f = {}
    for p in [5,10,20,30,60]:
        ma = pd.Series(c).rolling(p).mean().values
        f[f"MA_Dev_{p}"] = (c-ma)/(ma+1e-9)
    for p in [5,10,20]:
        vma = pd.Series(v).rolling(p).mean().values
        f[f"Vol_Ratio_{p}"] = v/(vma+1e-9)
        f[f"Range_{p}"] = (pd.Series(h).rolling(p).max()-pd.Series(l).rolling(p).min()).values/(c+1e-9)
    for p in [1,3,5,10,20]:
        f[f"Ret_{p}"] = pd.Series(c).pct_change(p).values
    for p in [10,20]:
        hh = pd.Series(h).rolling(p).max().values
        ll = pd.Series(l).rolling(p).min().values
        f[f"Price_Pos_{p}"] = (c-ll)/(hh-ll+1e-9)
        f[f"PV_Corr_{p}"] = pd.Series(c).rolling(p).corr(pd.Series(v)).values
    vwap = (h+l+c)/3
    vwap_ma = pd.Series(vwap*v).rolling(20).sum().values/(pd.Series(v).rolling(20).sum().values+1e-9)
    f["VWAP_Dev"] = (c-vwap_ma)/(vwap_ma+1e-9)
    return pd.DataFrame(f)

def compute_ic(fd, fr):
    r = []
    for col in fd.columns:
        valid = ~pd.isna(fd[col].values) & ~pd.isna(fr)
        if valid.sum() < 30: continue
        ic, pv = spearmanr(fd[col].values[valid], fr[valid])
        if not np.isnan(ic):
            r.append({"factor":col,"IC":round(ic,4),"IC_abs":round(abs(ic),4),"p":round(pv,4),"sig":pv<0.05})
    r.sort(key=lambda x: x["IC_abs"], reverse=True)
    return r

def ma_bt(c, s, l):
    ms = pd.Series(c).rolling(s).mean().values
    ml = pd.Series(c).rolling(l).mean().values
    cash, shares, pos = 100000, 0, 0
    peak, dd_max = 100000, 0
    vals = [100000]
    trades = 0
    for i in range(l, len(c)):
        if ms[i] > ml[i] and pos == 0:
            n = int(cash/c[i]/100)*100
            if n > 0: cash -= n*c[i]; shares = n; pos = 1; trades += 1
        elif ms[i] < ml[i] and pos == 1:
            cash += shares*c[i]; shares, pos = 0, 0
        val = cash + shares*c[i]
        vals.append(val); peak = max(peak, val)
        dd_max = max(dd_max, (peak-val)/peak*100)
    if pos == 1: cash += shares*c[-1]
    ret = pd.Series(vals).pct_change().dropna()
    sharpe = ret.mean()/max(ret.std(),1e-6)*np.sqrt(252) if len(ret)>0 else 0
    return {"ret":round((cash/100000-1)*100,2),"sharpe":round(sharpe,2),"dd":round(dd_max,2),"trades":trades}

def best_ma(c):
    b = {"ret":-999,"sharpe":-999,"dd":100,"params":"5x20"}
    for s in range(5,41,3):
        for l in range(s+5,81,5):
            r = ma_bt(c,s,l)
            if r["sharpe"] > b["sharpe"]:
                b = {**r,"params":f"{s}x{l}"}
    return b

def risk_an(c):
    r = pd.Series(c).pct_change().dropna()
    av = r.std()*np.sqrt(252)*100
    dd = (c/np.maximum.accumulate(c)-1)*100
    md = dd.min()
    sh = r.mean()/max(r.std(),1e-6)*np.sqrt(252)
    v95 = np.percentile(r,5)*100
    cv95 = r[r<=np.percentile(r,5)].mean()*100
    return {"年化波动":f"{av:.1f}%","最大回撤":f"{md:.1f}%","夏普":f"{sh:.2f}","VaR95":f"{v95:.2f}%","CVaR95":f"{cv95:.2f}%"}

print("="*70)
print("  4只标的完整量化分析")
import datetime as dt
print(f"  {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")
print("="*70)

for code in TORDER:
    name = CODES[code]
    csv_path = os.path.join(QLIB_DIR, f"{code}.csv")
    if not os.path.exists(csv_path):
        print(f"  {name}: 无数据"); continue
    
    df = pd.read_csv(csv_path)
    c = df["close"].values
    
    print(f"\n{'='*70}")
    print(f"  {name}({code})  |  {len(df)}条K线")
    print(f"  {str(df['date'].values[0])[:10]} → {str(df['date'].values[-1])[:10]}")
    print(f"  收盘均价={c.mean():.2f}  最新价={c[-1]:.2f}")
    
    # 因子
    fr = np.full(len(df), np.nan)
    for i in range(len(df)-5): fr[i] = c[i+5]/c[i]-1
    fd = compute_factors(df)
    ics = compute_ic(fd, fr)
    
    print(f"\n  Top3因子 (预测5日)")
    for f in ics[:3]:
        print(f"    {'✨' if f['sig'] else '  '} {f['factor']:<18} IC={f['IC']:+.4f}  p={f['p']:.3f}")
    
    print(f"  Top RD/关键因子")
    keys = ["RD_Ret5_Range10","RD_MADev5_MADev20","RD_Divergence","RD_Momentum_Accel","Ret_5","Range_5","Range_10"]
    kf = [r for r in ics if r["factor"] in keys]
    for r in sorted(kf, key=lambda x: x["IC_abs"], reverse=True)[:4]:
        print(f"    {'✅' if abs(r['IC'])>0.05 else '🟡'} {r['factor']:<20} IC={r['IC']:+.4f}")
    
    # MA回测
    best = best_ma(c)
    dflt = ma_bt(c,5,20)
    print(f"\n  最优MA({best['params']}): +{best['ret']:.1f}%  夏普{best['sharpe']:.2f}  回撤{best['dd']:.1f}%")
    print(f"  默认MA(5x20):  +{dflt['ret']:.1f}%  夏普{dflt['sharpe']:.2f}")
    
    # 风险
    rk = risk_an(c)
    print(f"\n  风险: {' | '.join(f'{k}={v}' for k,v in rk.items())}")
    
    # PPO信号（如果可用）
    try:
        sys.path.insert(0, "/config/quant_scripts")
        from rl_inference import get_rl_signal
        sig, meta = get_rl_signal()
        if sig and (code == "002594" or code == "518880"):
            ac = sig.get(code, 0)
            d = "买入" if ac > 0.1 else ("卖出" if ac < -0.1 else "持有")
            print(f"  PPO: {d} (动作值={ac:+.3f})")
    except:
        pass

print(f"\n{'='*70}")
print("  分析完成")
print("="*70)

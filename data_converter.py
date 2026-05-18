#!/config/quant_env/bin/python3
"""
data_converter.py — Qlib数据生成器（Baostock引擎 v2）
=========================================================
根因修复：将数据源从 OmniData MCP（依赖push2his.eastmoney.com，已被环境网络阻断）
切换为 Baostock（盘后K线数据，不受push阻断）。

用法:
  # 生成指定标的
  /config/quant_env/bin/python3 data_converter.py --codes 000063 002466
  
  # 全量刷新（覆盖所有现有CSV）
  /config/quant_env/bin/python3 data_converter.py --all
  
  # 只补缺失的
  /config/quant_env/bin/python3 data_converter.py
"""

import json, os, sys, time
from pathlib import Path
from datetime import datetime



# ── 管你是什么前缀，都能映射 ──
def stock_code_to_exchange(code):
    if code.startswith(("5", "6")):
        return "sh"
    return "sz"


def fetch_kline_baostock(code, start_date="20230101", end_date=None, max_retries=3):
    """Baostock获取K线（主数据源）"""
    if end_date is None:
        end_date = datetime.now().strftime("%Y%m%d")
    import baostock as bs
    import io, contextlib
    bs_code = f"{stock_code_to_exchange(code)}.{code}"
    start = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
    end = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"
    for attempt in range(max_retries):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                lg = bs.login()
            if lg.error_code != "0":
                time.sleep(2)
                continue
            rs = bs.query_history_k_data_plus(
                bs_code, "date,open,high,low,close,volume,amount,pctChg",
                start_date=start, end_date=end, frequency="d", adjustflag="2"
            )
            if rs.error_code != "0":
                with contextlib.redirect_stdout(io.StringIO()):
                    bs.logout()
                return None
            records = []
            while rs.next():
                row = rs.get_row_data()
                if row[0] is None or row[0] == "":
                    continue
                records.append({
                    "日期": row[0].replace("-", ""),
                    "开盘": float(row[1]) if row[1] else 0,
                    "最高": float(row[2]) if row[2] else 0,
                    "最低": float(row[3]) if row[3] else 0,
                    "收盘": float(row[4]) if row[4] else 0,
                    "成交量(手)": float(row[5]) if row[5] else 0,
                    "成交额(万元)": round(float(row[6]) / 10000, 2) if row[6] else 0,
                    "涨跌幅(%)": float(row[7]) if row[7] else 0,
                })
            with contextlib.redirect_stdout(io.StringIO()):
                bs.logout()
            if not records:
                return None
            return records
        except Exception as e:
            with contextlib.redirect_stdout(io.StringIO()):
                try: bs.logout()
                except: pass
            if attempt < max_retries - 1:
                time.sleep((attempt + 1) * 3)
    return None


def fetch_kline_tencent(code, start_date="20230101", end_date=None):
    """
    腾讯API备用数据源（用于日K线补充）。
    注：腾讯API不支持历史批量，需逐日获取，仅做兜底。
    实际不实现批量——保留接口供扩展。
    """
    pass


def convert_to_qlib_csv(kline_data, output_path):
    """转换K线数据 → Qlib CSV格式"""
    import pandas as pd
    records = []
    for k in kline_data:
        try:
            close = float(k["收盘"])
            volume = float(k["成交量(手)"])
            amount = float(k["成交额(万元)"])
            records.append({
                "date": k["日期"],
                "open": float(k["开盘"]),
                "high": float(k["最高"]),
                "low": float(k["最低"]),
                "close": close,
                "volume": volume,
                "amount": amount,
                "change": 0.0,
                "pct_change": float(k.get("涨跌幅(%)", 0)),
                "vwap": round(amount / (volume * 100 + 1e-9), 4),
                "turnover": 0.0,
            })
        except (KeyError, ValueError, TypeError):
            continue
    if not records:
        return None
    df = pd.DataFrame(records).sort_values("date").drop_duplicates(subset="date")
    df.to_csv(output_path, index=False)
    return df


def ensure_stock_data(code, feat_dir, start_date="20230101", end_date=None):
    """补单只标的数据：有则跳过，无则生成"""
    csv_path = Path(feat_dir) / f"{code}.csv"
    if csv_path.exists():
        return True, f"已有: {csv_path.name}"
    records = fetch_kline_baostock(code, start_date, end_date)
    if not records:
        return False, "Baostock不可用"
    df = convert_to_qlib_csv(records, csv_path)
    if df is None:
        return False, "转换失败"
    return True, f"生成: {csv_path.name} ({len(df)}行)"


STOCK_MAP = {
    "002594": "比亚迪", "518880": "黄金ETF", "600519": "贵州茅台",
    "000001": "平安银行", "000333": "美的集团", "000858": "五粮液",
    "002415": "海康威视", "002475": "立讯精密", "300750": "宁德时代",
    "600036": "招商银行", "600887": "伊利股份", "600276": "恒瑞医药",
    "601318": "中国平安", "600487": "亨通光电", "600105": "永鼎股份",
    "600522": "中天科技", "300589": "江龙船艇", "603950": "长源东谷",
    "688551": "科威尔", "600118": "中国卫星", "002506": "协鑫集成",
    "002309": "中利集团", "000720": "新能泰山", "600707": "彩虹股份",
    "688015": "交控科技", "002297": "博云新材", "000066": "中国长城",
    "002049": "紫光国微", "600941": "中国移动", "600703": "三安光电",
    "000063": "中兴通讯", "000938": "紫光股份", "002466": "天齐锂业",
    "002560": "通达股份", "512480": "半导体ETF", "515790": "光伏ETF",
}

# ── 向后兼容导出 ──
# run_omnidata_spider ── 已废弃
# BROKEN_KLINE_STOCKS ── 已废弃
fetch_kline = fetch_kline_baostock


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Qlib数据转换器 v2（Baostock引擎）")
    parser.add_argument("--codes", nargs="+", help="股票代码，默认=全部STOCK_MAP")
    parser.add_argument("--start", default="20230101")
    parser.add_argument("--end", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--output", default="/config/qlib_data")
    parser.add_argument("--all", action="store_true", help="强制刷新所有")
    args = parser.parse_args()

    feat_dir = Path(args.output) / "features"
    feat_dir.mkdir(parents=True, exist_ok=True)

    if args.codes:
        codes = args.codes
    else:
        codes = list(STOCK_MAP.keys())

    ok, fail = [], []
    for code in codes:
        must = args.all
        csv_path = feat_dir / f"{code}.csv"
        if csv_path.exists() and not must:
            ok.append((code, "已有"))
            continue
        success, msg = ensure_stock_data(code, feat_dir, args.start, args.end)
        if success:
            ok.append((code, msg))
        else:
            fail.append((code, msg))

    print(f"\n=== 结果: {len(ok)} OK, {len(fail)} FAIL ===")
    if fail:
        for c, m in fail:
            print(f"  ⛔ {c} ({STOCK_MAP.get(c,'?')}): {m}")
    if ok:
        for c, m in ok:
            print(f"  ✅ {c} ({STOCK_MAP.get(c,'?')}): {m}")

#!/usr/local/bin/python3
"""
qlib_data_fetcher.py — Qlib数据获取器（Baostock替代OmniData）
=============================================================
根因：push2his.eastmoney.com 被环境网络阻断，OmniData MCP的K线接口不可用。
此脚本使用 Baostock（盘后数据，不受push阻断影响）获取K线数据。
"""

import os, sys, json, time
from pathlib import Path
from datetime import datetime, timedelta

# ── 市场前缀映射 ──
def stock_code_to_bs(code):
    """股票代码 → Baostock格式（含exchange前缀）"""
    # 5/6开头 → 沪市, 其他 → 深市
    if code.startswith(("5", "6")):
        return f"sh.{code}"
    elif code.startswith(("0", "3", "1", "2")):
        return f"sz.{code}"
    else:
        return f"sz.{code}"  # 默认深市

def fetch_kline_baostock(code, start_date="20230101", end_date=None, max_retries=3):
    """
    通过Baostock获取K线数据。
    Baostock date格式: YYYY-MM-DD
    """
    if end_date is None:
        end_date = datetime.now().strftime("%Y%m%d")

    bs_code = stock_code_to_bs(code)
    start = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
    end = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"

    import baostock as bs
    import io, contextlib

    for attempt in range(max_retries):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                lg = bs.login()
            if lg.error_code != "0":
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                print(f"  [FAIL] {code}: Baostock登录失败 {lg.error_msg}", file=sys.stderr)
                return None

            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume,amount,peTTm,pctChg",
                start_date=start, end_date=end,
                frequency="d", adjustflag="2"  # 2=前复权
            )

            if rs.error_code != "0":
                print(f"  [FAIL] {code}: Baostock查询失败 {rs.error_msg}", file=sys.stderr)
                bs.logout()
                return None

            records = []
            while rs.next():
                row = rs.get_row_data()
                if row[0] is None or row[0] == "":
                    continue
                records.append({
                    "date": row[0].replace("-", ""),       # YYYYMMDD
                    "open": float(row[1]) if row[1] else 0,
                    "high": float(row[2]) if row[2] else 0,
                    "low": float(row[3]) if row[3] else 0,
                    "close": float(row[4]) if row[4] else 0,
                    "volume": float(row[5]) if row[5] else 0,
                    "amount": float(row[6]) if row[6] else 0,
                    "peTTm": float(row[7]) if row[7] else 0,
                    "pctChg": float(row[8]) if row[8] else 0,
                })

            bs.logout()

            if not records:
                print(f"  [FAIL] {code}: Baostock返回0条记录 (bs_code={bs_code}, {start}~{end})", file=sys.stderr)
                return None

            print(f"  [OK] {code}: Baostock {len(records)} 条 ({records[0]['date']} ~ {records[-1]['date']})", file=sys.stderr)
            return records

        except Exception as e:
            if attempt < max_retries - 1:
                wait = (attempt + 1) * 3
                print(f"  [RETRY] {code}: {e}，{wait}s后重试", file=sys.stderr)
                time.sleep(wait)
            else:
                print(f"  [FAIL] {code}: Baostock异常 {e} (已重试{max_retries}次)", file=sys.stderr)
                return None

    return None


def convert_to_qlib_csv(records, output_path):
    """转换Baostock记录 → Qlib CSV格式"""
    import pandas as pd
    rows = []
    for r in records:
        close = r["close"]
        volume = r["volume"]
        amount = r["amount"]
        # 计算vwap
        vwap = round(amount / (volume * 100 + 1e-9), 4) if volume > 0 else close
        # 计算换手率（baostock不直接提供，用估算）
        turnover = 0.0
        rows.append({
            "date": r["date"],
            "open": round(r["open"], 4),
            "high": round(r["high"], 4),
            "low": round(r["low"], 4),
            "close": round(close, 4),
            "volume": int(volume),
            "amount": round(amount, 2),
            "change": round(close - r["open"], 4),
            "pct_change": round(r.get("pctChg", 0), 4),
            "vwap": vwap,
            "turnover": turnover,
        })
    df = pd.DataFrame(rows).sort_values("date").drop_duplicates(subset="date")
    df.to_csv(output_path, index=False)
    print(f"  [OK] 写入 {output_path} ({len(df)} 行)", file=sys.stderr)
    return df


def generate_code(code, output_dir, start_date="20230101", end_date=None, force=False):
    """为单个股票代码生成Qlib CSV"""
    csv_path = os.path.join(output_dir, f"{code}.csv")
    if os.path.exists(csv_path) and not force:
        print(f"  [SKIP] {code}: 已有 {csv_path}", file=sys.stderr)
        return csv_path

    records = fetch_kline_baostock(code, start_date, end_date)
    if not records:
        return None

    convert_to_qlib_csv(records, csv_path)
    return csv_path


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Qlib数据获取器（Baostock引擎）")
    parser.add_argument("--codes", nargs="+", help="股票代码列表")
    parser.add_argument("--start", default="20230101")
    parser.add_argument("--end", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--output", default="/config/qlib_data/features")
    parser.add_argument("--force", action="store_true", help="强制重新生成")
    parser.add_argument("--all", action="store_true", help="覆盖所有现有数据")
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)

    if args.all and not args.codes:
        # 读取当前所有CSV + 额外需要覆盖的
        existing = [f.replace(".csv", "") for f in os.listdir(args.output) if f.endswith(".csv")]
        codes = sorted(existing)
    elif args.codes:
        codes = args.codes
    else:
        codes = ["000063", "002466", "002560", "515790"]  # 默认缺的4只

    results = {"ok": [], "fail": []}
    for code in codes:
        path = generate_code(code, args.output, args.start, args.end, force=args.force or args.all)
        if path:
            results["ok"].append(code)
        else:
            results["fail"].append(code)

    print(f"\n{'='*40}", file=sys.stderr)
    print(f"完成: {len(results['ok'])} OK, {len(results['fail'])} FAIL", file=sys.stderr)
    if results["ok"]:
        print(f"成功: {', '.join(results['ok'])}", file=sys.stderr)
    if results["fail"]:
        print(f"失败: {', '.join(results['fail'])}", file=sys.stderr)
    print(json.dumps(results))

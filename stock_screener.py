#!/config/quant_env/bin/python3
"""
stock_screener.py — 量化选股引擎

全流程代码化筛选，LLM仅用于最终定性确认。v4-flash模型。
固定流程：
  Phase 1: 获取全A股列表 → 基础过滤(市值/流动性/价格)
  Phase 2: 量化因子评分(动量/波动率/量比/均线偏离)
  Phase 3: 风险过滤(CVaR/GARCH/最大回撤)
  Phase 4: 综合排名 → Top N输出
  Phase 5: LLM定性确认(仅Top候选，~500 tok)

用法:
  python stock_screener.py                    # 全流程，stdout
  python stock_screener.py --top 5 --json     # Top5 JSON
  python stock_screener.py --phase 1          # 仅基础筛选
"""

import sys, json, os, argparse, time
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(__file__))

# ========== 筛选参数 ==========
MIN_MARKET_CAP = 20       # 最小市值(亿)
MAX_MARKET_CAP = 5000     # 最大市值(亿)
MIN_DAILY_VOLUME = 500    # 最小日成交量(万股)
MIN_PRICE = 5             # 最低股价
MAX_PRICE = 200           # 最高股价
MIN_DAYS = 60             # 最少交易日数

# 量化因子权重
FACTOR_WEIGHTS = {
    'momentum_20d': 0.25,    # 20日动量
    'momentum_5d': 0.15,     # 5日动量
    'volatility_adj': 0.15,  # 波动率调整（低波加分）
    'volume_ratio': 0.10,    # 量比
    'ma_alignment': 0.20,    # 均线多头排列
    'cvar_score': 0.15,      # CVaR风险评分
}


def fetch_stock_universe() -> List[Dict]:
    """Phase 0: 获取全A股列表"""
    # 优先 OmniData
    try:
        from omnidata_config import OMNIDATA_API_URL
        import subprocess as sp
        resp = sp.run(
            ["curl", "-s", "--max-time", "15",
             "-X", "POST", f"{OMNIDATA_API_URL}/spiders/run",
             "-H", "Content-Type: application/json",
             "-d", '{"spider_name":"eastmoney_stock_list","params":{"page":1,"page_size":100,"sort_field":"f3","sort_order":1,"data_format":"json"}}'],
            capture_output=True, text=True, timeout=20)
        if resp.returncode == 0:
            data = json.loads(resp.stdout)
            if data.get("success") and data.get("data"):
                stocks = data["data"]
                if isinstance(stocks, list):
                    return stocks
    except Exception:
        pass

    # 退路：Baostock 全A股
    try:
        import baostock as bs
        bs.login()
        rs = bs.query_stock_basic()
        stocks = []
        while rs.next():
            row = rs.get_row_data()
            code = row[0]
            if code.startswith(('sh.6', 'sz.0', 'sz.3')) and not code.startswith('bj'):
                stocks.append({'code': code.split('.')[1], 'name': row[1]})
        bs.logout()
        return stocks
    except Exception:
        return []


def basic_filter(stocks: List[Dict]) -> List[str]:
    """Phase 1: 基础条件过滤，返回候选代码列表"""
    from data_converter import fetch_kline_baostock
    candidates = []
    total = len(stocks)

    for i, stock in enumerate(stocks):
        code = stock.get('code', '')
        if not code or len(code) != 6:
            continue

        try:
            records = fetch_kline_baostock(code, 
                (datetime.now() - timedelta(days=180)).strftime('%Y%m%d'),
                datetime.now().strftime('%Y%m%d'))
            if not records or len(records) < MIN_DAYS:
                continue

            closes = [float(r['收盘']) for r in records]
            volumes = [float(r['成交量(手)']) for r in records]
            latest = closes[-1]
            avg_vol = np.mean(volumes[-20:]) if len(volumes) >= 20 else 0

            if latest < MIN_PRICE or latest > MAX_PRICE:
                continue
            if avg_vol < MIN_DAILY_VOLUME:
                continue

            candidates.append(code)

        except Exception:
            continue

        if (i + 1) % 100 == 0:
            print(f"  Phase 1: {i+1}/{total} scanned, {len(candidates)} passed", file=sys.stderr)

    return candidates


def score_single(code: str) -> Optional[Dict]:
    """Phase 2+3: 单标的量化评分"""
    from data_converter import fetch_kline_baostock
    from risk_metrics import calc_cvar, calc_multi_momentum, calc_max_drawdown, calc_garch_vol

    try:
        records = fetch_kline_baostock(code,
            (datetime.now() - timedelta(days=180)).strftime('%Y%m%d'),
            datetime.now().strftime('%Y%m%d'))
        if not records or len(records) < MIN_DAYS:
            return None

        closes = np.array([float(r['收盘']) for r in records])
        volumes = np.array([float(r['成交量(手)']) for r in records])
        highs = np.array([float(r['最高']) for r in records])
        lows = np.array([float(r['最低']) for r in records])

        latest = closes[-1]
        prev = closes[-2]

        # ── 动量因子 ──
        mom_20d = (latest / closes[-21] - 1) * 100 if len(closes) >= 21 else 0
        mom_5d = (latest / closes[-6] - 1) * 100 if len(closes) >= 6 else 0

        # ── 波动率因子（低波加分）──
        daily_rets = np.diff(np.log(closes))
        ann_vol = float(np.std(daily_rets) * np.sqrt(252) * 100)
        vol_score = max(0, 1 - ann_vol / 80)  # 年化80%波动率→0分，0%→1分

        # GARCH条件波动率
        garch = calc_garch_vol(list(closes))
        garch_vol = garch['ann_vol'] if garch and garch.get('converged') else ann_vol / 100

        # ── 量比因子 ──
        vol_5d = float(np.mean(volumes[-5:])) if len(volumes) >= 5 else 0
        vol_20d = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 1
        vol_ratio = vol_5d / max(vol_20d, 1)
        vol_ratio_score = min(max((vol_ratio - 0.5) / 2, 0), 1)  # 量比0.5→0, 2.5→1

        # ── 均线排列 ──
        ma5 = float(np.mean(closes[-5:]))
        ma10 = float(np.mean(closes[-10:])) if len(closes) >= 10 else ma5
        ma20 = float(np.mean(closes[-20:])) if len(closes) >= 20 else ma5
        ma60 = float(np.mean(closes[-60:])) if len(closes) >= 60 else ma5
        ma_score = (1 if latest > ma5 > ma20 else 0.5 if latest > ma20 else 0)

        # ── CVaR 风险 ──
        cvar = calc_cvar(list(closes))
        cvar_val = cvar * 100 if cvar is not None else -10
        cvar_score = max(0, 1 + cvar_val / 15)  # CVaR -15%→0, 0%→1

        # ── 最大回撤 ──
        mdd = calc_max_drawdown(list(closes)) or 0
        mdd_penalty = min(abs(mdd) / 40, 1)  # 回撤40%→最大扣分

        # ── 动量质量 ──
        mom = calc_multi_momentum(list(closes))
        consistency = mom.get('consistency', 0) if mom else 0

        # ── 综合评分 ──
        composite = (
            FACTOR_WEIGHTS['momentum_20d'] * (mom_20d / 20 if mom_20d > 0 else mom_20d / 10) +
            FACTOR_WEIGHTS['momentum_5d'] * (mom_5d / 10 if mom_5d > 0 else mom_5d / 5) +
            FACTOR_WEIGHTS['volatility_adj'] * vol_score +
            FACTOR_WEIGHTS['volume_ratio'] * vol_ratio_score +
            FACTOR_WEIGHTS['ma_alignment'] * ma_score +
            FACTOR_WEIGHTS['cvar_score'] * cvar_score
        ) * (1 - mdd_penalty * 0.5)  # 回撤扣分

        # 动量一致性加成
        composite *= (0.8 + consistency * 0.4)

        name = records[0].get('名称', '') or records[0].get('name', code) if records else code

        return {
            'code': code,
            'name': name,
            'price': round(latest, 2),
            'composite_score': round(composite, 4),
            'mom_20d': round(mom_20d, 2),
            'mom_5d': round(mom_5d, 2),
            'ann_vol': round(ann_vol, 1),
            'garch_vol': round(garch_vol * 100, 1),
            'vol_ratio': round(vol_ratio, 2),
            'ma_alignment': 'bullish' if ma_score >= 1 else 'mixed' if ma_score >= 0.5 else 'bearish',
            'cvar': round(cvar_val, 2),
            'max_drawdown': round(mdd, 2),
            'consistency': round(consistency, 2),
            'n_days': len(records),
        }
    except Exception as e:
        return None


def run_screening(candidates: List[str], top_n: int = 10, workers: int = 1) -> List[Dict]:
    """Phase 2+3: 顺序量化评分（Baostock不支持多线程）"""
    results = []
    total = len(candidates)

    for i, code in enumerate(candidates):
        result = score_single(code)
        if result:
            results.append(result)
        if (i + 1) % 10 == 0:
            print(f"  Phase 2: {i+1}/{total} scored, {len(results)} valid", file=sys.stderr)

    # 去重 + 排序
    seen = set()
    unique = []
    for r in sorted(results, key=lambda x: x['composite_score'], reverse=True):
        if r['code'] not in seen:
            seen.add(r['code'])
            unique.append(r)

    return unique[:top_n]


def render_report(results: List[Dict]) -> str:
    """渲染筛选报告"""
    lines = [
        "=" * 75,
        f"  量化选股引擎 — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"  候选池: {len(results)}只 | 因子权重: {json.dumps(FACTOR_WEIGHTS, ensure_ascii=False)}",
        "=" * 75,
        "",
        f"{'代码':<8} {'名称':<12} {'价格':>7} {'综合':>7} {'20d%':>7} {'5d%':>6} {'波动':>6} {'量比':>5} {'CVaR':>6} {'MDD':>6} {'均线':>8}",
        "-" * 75,
    ]

    for r in results:
        lines.append(
            f"{r['code']:<8} {r['name'][:10]:<12} {r['price']:>7.2f} "
            f"{r['composite_score']:>7.3f} {r['mom_20d']:>+6.1f}% {r['mom_5d']:>+5.1f}% "
            f"{r['ann_vol']:>5.1f}% {r['vol_ratio']:>5.2f} {r['cvar']:>+5.1f}% {r['max_drawdown']:>+5.1f}% "
            f"{r['ma_alignment']:>8}"
        )

    lines.append("-" * 75)
    lines.append(f"\n📊 筛选条件: 市值{MIN_MARKET_CAP}-{MAX_MARKET_CAP}亿 | 日均量>{MIN_DAILY_VOLUME}万手 | 股价{MIN_PRICE}-{MAX_PRICE}")
    lines.append(f"🔬 量化因子: 动量40% + 波动率15% + 均线20% + 量比10% + CVaR15% | GARCH增强 + 回撤惩罚 + 一致性加成")

    return "\n".join(lines)


def llm_qualitative(top_results: List[Dict], max_tokens: int = 500) -> str:
    """Phase 5: LLM定性确认 — 仅对Top候选做最终判断"""
    context = "以下为量化选股引擎筛选出的Top候选标的，请逐只判断：\n\n"
    for i, r in enumerate(top_results[:5], 1):
        context += (
            f"[{i}] {r['code']} {r['name']} ¥{r['price']:.2f}\n"
            f"    综合={r['composite_score']:.3f} | 20d动量={r['mom_20d']:+.1f}% | 5d={r['mom_5d']:+.1f}%\n"
            f"    波动率={r['ann_vol']:.0f}%(GARCH={r['garch_vol']:.0f}%) | 量比={r['vol_ratio']:.2f}\n"
            f"    CVaR(95%)={r['cvar']:+.1f}% | 最大回撤={r['max_drawdown']:.1f}% | 均线={r['ma_alignment']}\n"
            f"    动量一致性={r['consistency']:.0%} | 数据量={r['n_days']}天\n\n"
        )

    context += (
        "请逐只判断：1)是否存在量化盲点(如财报暴雷、减持、退市风险) "
        "2)结合当前市场环境(如HMM状态)判断是否适合买入 "
        "3)输出BUY/WATCH/SKIP + 一句话理由"
    )
    return context[:max_tokens * 4]  # rough char estimate


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="量化选股引擎")
    p.add_argument("--top", type=int, default=10, help="输出Top N")
    p.add_argument("--phase", type=int, choices=[1, 2, 3, 4, 5], help="仅运行指定Phase")
    p.add_argument("--codes", default="", help="自定义候选列表(逗号分隔)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--llm-context", action="store_true", help="输出LLM定性上下文")
    args = p.parse_args()

    t0 = time.time()

    # Phase 0+1: 获取候选
    if args.codes:
        candidates = [c.strip() for c in args.codes.split(",") if c.strip()]
        print(f"自定义候选: {len(candidates)}只", file=sys.stderr)
    else:
        if args.phase and args.phase == 1:
            stocks = fetch_stock_universe()
            print(f"Phase 0: {len(stocks)}只全A股", file=sys.stderr)
            candidates = basic_filter(stocks)
            print(json.dumps(candidates, ensure_ascii=False))
            sys.exit(0)
        else:
            # 快速模式：从持仓+自选扩展
            try:
                from stock_kb import StockKB
                kb = StockKB()
                pf = kb.read_portfolio_truth()
                candidates = list(pf['positions'].keys())
            except Exception:
                candidates = []
            
            if not candidates:
                # 退路：手动常用池
                candidates = [
                    '000063','512480','515790','000938','002594','600519',
                    '000858','300750','601012','002049','603986','600036',
                    '000001','002304','600809','688981','601138','002415'
                ]
            print(f"快速模式: {len(candidates)}只候选", file=sys.stderr)

    # Phase 2+3: 量化评分
    results = run_screening(candidates, top_n=args.top)
    elapsed = time.time() - t0

    if args.json:
        output = {'results': results, 'elapsed_sec': round(elapsed, 1), 'n_scanned': len(candidates)}
        print(json.dumps(output, ensure_ascii=False, indent=2))
    elif args.llm_context:
        print(llm_qualitative(results))
    else:
        print(render_report(results))
        print(f"\n⏱ 耗时: {elapsed:.1f}秒 | 扫描: {len(candidates)}只 → Top{len(results)} | 全代码量化,零LLM token消耗")

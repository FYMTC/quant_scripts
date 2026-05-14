#!/config/quant_env/bin/python3
"""
stock_screener.py — 量化选股引擎

全流程代码化筛选，LLM仅用于最终定性确认。v4-flash模型。
固定流程：
  Phase 1: 获取全A股列表 → 基础过滤(市值/流动性/价格)
  Phase 2: 量化因子评分(动量/波动率/量比/均线偏离)
  Phase 3: 风险过滤(CVaR/GARCH/最大回撤)
  Phase 4: 综合排名 → Top N输出
  Phase 5: 输出结构化候选摘要供上层LLM参考(纯排名,不给买卖建议)

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

# ── 行业过滤：排除夕阳产业/垃圾板块/落后技术 ──
SECTOR_BLACKLIST = [
    # 夕阳产业
    '煤炭', '钢铁', '石油', '石化', '化纤', '造纸', '纺织',
    # 垃圾板块
    '房地产', '房地产业', '建筑装饰', '建筑材料',
    # 落后技术/传统制造
    '传统汽车', '摩托车', '自行车',
    # 过剩/高污染
    '水泥', '玻璃', '陶瓷', '火力发电', '热力',
]

SECTOR_CODE_BLACKLIST = ['B06', 'B07', 'B08', 'B09',  # 采矿业
                          'C25', 'C30', 'C31',         # 石油/非金属/黑色金属
                          'D44', 'D45',                 # 电力/燃气
                          'K70',                        # 房地产业
                          ]

SECTOR_WHITELIST_PREFIX = ['C39', 'C40',  # 计算机/通信/电子
                            'C35', 'C36',  # 专用设备/汽车(新能源)
                            'C27',         # 医药
                            'I63', 'I64', 'I65',  # 信息技术/互联网
                            'M74',         # 科学研究
                            ]

# 优先行业（加分项）
SECTOR_BONUS = {
    # Baostock行业名匹配
    '计算机、通信和其他电子设备': 0.08,  # C39: 含半导体/芯片/消费电子
    '软件和信息技术服务': 0.10,          # I65: 含AI/云计算
    '互联网': 0.08,                      # I64
    '医药': 0.08, '医疗': 0.08,         # C27
    '电气机械和器材': 0.08,              # C38: 含新能源/光伏/锂电池
    '专用设备': 0.05,                    # C35: 含军工/航空航天
    '铁路、船舶、航空航天': 0.05,        # C37
    '仪器仪表': 0.08,                    # C40
    # 关键词匹配（用于更精确的细分）
    '半导体': 0.10, '芯片': 0.10, '集成电路': 0.10,
    '新能源': 0.08, '光伏': 0.08,
    '人工智能': 0.10, '机器人': 0.08,
    '创新药': 0.08, '生物': 0.05,
    '军工': 0.05, '航天': 0.05,
    '软件': 0.08, '云计算': 0.08, '数据': 0.05,
}

def _fetch_industry_map(codes: List[str]) -> Dict[str, str]:
    """批量获取行业分类（缓存60秒）"""
    cache_file = '/tmp/stock_industry_cache.json'
    import time as _time
    if os.path.exists(cache_file) and _time.time() - os.path.getmtime(cache_file) < 3600:
        with open(cache_file) as f:
            return json.load(f)
    
    industry_map = {}
    try:
        import baostock as bs
        bs.login()
        for code in codes:
            try:
                rs = bs.query_stock_industry(f'sh.{code}' if code.startswith('6') else f'sz.{code}')
                if rs.error_code == '0':
                    while rs.next():
                        row = rs.get_row_data()
                        industry_map[code] = row[3] if len(row) > 3 else ''
            except Exception:
                pass
        bs.logout()
    except Exception:
        pass
    
    with open(cache_file, 'w') as f:
        json.dump(industry_map, f)
    return industry_map

def is_blacklisted(industry: str, code: str = '') -> bool:
    """检查行业是否在黑名单中"""
    # 代码前缀黑名单
    for bl in SECTOR_CODE_BLACKLIST:
        if industry.startswith(bl):
            return True
    # 关键词黑名单
    for kw in SECTOR_BLACKLIST:
        if kw in industry:
            return True
    return False

def get_sector_bonus(industry: str) -> float:
    """获取行业加分"""
    for kw, bonus in SECTOR_BONUS.items():
        if kw in industry:
            return bonus
    return 0.0


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
    """Phase 0: 获取全A股列表（含实时价格/成交量）"""
    try:
        from omnidata_config import OMNIDATA_API_URL
        import subprocess as sp
        
        all_stocks = []
        for page in [1, 2, 3, 4, 5, 6, 7, 8]:  # 8页=800只，按成交额排序
            resp = sp.run(
                ["curl", "-s", "--max-time", "10",
                 "-X", "POST", f"{OMNIDATA_API_URL}/spiders/run",
                 "-H", "Content-Type: application/json",
                 "-d", json.dumps({"spider_name":"eastmoney_stock_list",
                     "params":{"page":page,"page_size":100,"sort_field":"f6",
                              "sort_order":1,"data_format":"json"}})],
                capture_output=True, text=True, timeout=15)
            if resp.returncode == 0:
                data = json.loads(resp.stdout)
                if data.get("success") and data.get("data"):
                    page_data = data["data"]
                    if isinstance(page_data, dict):
                        stocks = page_data.get("stocks", [])
                    elif isinstance(page_data, list):
                        stocks = page_data
                    else:
                        continue
                    for s in stocks:
                        code = s.get("股票代码", "")
                        if code and len(code) == 6:
                            all_stocks.append({
                                "code": code,
                                "name": s.get("股票名称", ""),
                                "price": float(s.get("最新价", 0)),
                                "volume": float(s.get("成交量(手)", 0)),
                                "pe": float(s.get("市盈率(动态)", 0)) if s.get("市盈率(动态)") not in (None, "-", "") else 0,
                            })
            if len(all_stocks) >= 500:
                break
        
        if all_stocks:
            return all_stocks
    except Exception:
        pass

    # 退路：Baostock 全A股（仅有代码名称，无实时价格）
    try:
        import baostock as bs
        bs.login()
        rs = bs.query_stock_basic()
        stocks = []
        while rs.next():
            row = rs.get_row_data()
            code = row[0]
            if code.startswith(('sh.6', 'sz.0', 'sz.3')) and not code.startswith('bj'):
                stocks.append({'code': code.split('.')[1], 'name': row[1], 'price': 0, 'volume': 0})
        bs.logout()
        return stocks
    except Exception:
        return []


def basic_filter(stocks: List[Dict]) -> List[str]:
    """Phase 1: 基础条件过滤。
    
    优先使用 OmniData 实时数据（快），退路用 Baostock K线（慢但有历史数据）。
    """
    candidates = []
    total = len(stocks)

    # 检测是否有实时价格数据
    has_realtime = any(s.get('price', 0) > 0 for s in stocks[:10])

    if has_realtime:
        # 快速路径：直接用实时价格/量过滤
        for stock in stocks:
            code = stock.get('code', '')
            price = stock.get('price', 0)
            volume = stock.get('volume', 0)
            if not code or len(code) != 6:
                continue
            if price < MIN_PRICE or price > MAX_PRICE:
                continue
            # 排除ST/*ST
            name = stock.get('name', '')
            if 'ST' in name or '*ST' in name:
                continue
            candidates.append(code)

        return candidates

    # 退路：Baostock K线逐个检查
    from data_converter import fetch_kline_baostock
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

        # 行业加分
        sector_bonus = 0.0
        try:
            industry_map = _fetch_industry_map([code])
            ind = industry_map.get(code, '')
            sector_bonus = get_sector_bonus(ind)
            composite += sector_bonus
        except Exception:
            pass

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
    """Phase 5: 为上层LLM生成结构化候选摘要 — 不给买卖建议，只提供量化事实"""
    context = "以下为量化选股引擎筛选出的Top候选标的（纯量化排名，不含买卖建议）：\n\n"
    for i, r in enumerate(top_results[:5], 1):
        context += (
            f"[{i}] {r['code']} {r['name']} ¥{r['price']:.2f}  综合={r['composite_score']:.3f}\n"
            f"    动量: 20d={r['mom_20d']:+.1f}% 5d={r['mom_5d']:+.1f}% 一致性={r['consistency']:.0%}\n"
            f"    风险: CVaR={r['cvar']:+.1f}% MDD={r['max_drawdown']:.1f}% GARCH={r['garch_vol']:.0f}%(历史={r['ann_vol']:.0f}%)\n"
            f"    技术: 均线={r['ma_alignment']} 量比={r['vol_ratio']:.2f} 样本={r['n_days']}天\n\n"
        )

    context += (
        "以上为纯量化排名。因子构成: 动量40% + 均线20% + 波动率15% + CVaR15% + 量比10%。"
        "GARCH增强+回撤惩罚+一致性加成。"
        "供上层LLM结合市场情绪/新闻/基本面做综合判断。"
    )
    return context[:max_tokens * 4]


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
    elif args.phase:
        if args.phase == 1:
            stocks = fetch_stock_universe()
            print(f"Phase 0: {len(stocks)}只全A股", file=sys.stderr)
            candidates = basic_filter(stocks)
            print(json.dumps(candidates, ensure_ascii=False))
            sys.exit(0)
    else:
        # 默认：全市场扫描 — 自动获取全A股→基础过滤→量化评分
        print("Phase 0: 获取全A股列表...", file=sys.stderr)
        stocks = fetch_stock_universe()
        print(f"  全A股: {len(stocks)}只", file=sys.stderr)
        
        if stocks:
            print(f"Phase 1: 基础过滤(市值{MIN_MARKET_CAP}-{MAX_MARKET_CAP}亿/量>{MIN_DAILY_VOLUME}万手/价{MIN_PRICE}-{MAX_PRICE})...", file=sys.stderr)
            candidates = basic_filter(stocks)
        else:
            # OmniData不可用时的退路：从常用池取
            print("  ⚠️ 全A股获取失败，使用常用候选池", file=sys.stderr)
            candidates = [
                '000063','512480','515790','000938','002594','600519',
                '000858','300750','601012','002049','603986','600036',
                '000001','002304','600809','688981','601138','002415',
                '002415','600031','000725','601318','600887','002475',
                '300124','000333','002230','603259','601899','600585',
            ]
        print(f"  基础过滤后: {len(candidates)}只候选", file=sys.stderr)
        
        # 候选池上限控制（K线逐个获取，每只~2秒）
        MAX_CANDIDATES = 50  # 交互模式上限；cron可传--top更大
        if len(candidates) > MAX_CANDIDATES:
            print(f"  候选过多，取前{MAX_CANDIDATES}只（按成交额排序）", file=sys.stderr)
            candidates = candidates[:MAX_CANDIDATES]
        
        # 行业过滤：排除夕阳产业/垃圾板块
        if len(candidates) > 20:
            print("  行业过滤: 获取行业分类...", file=sys.stderr)
            industry_map = _fetch_industry_map(candidates)
            filtered = []
            blacklisted = []
            for code in candidates:
                ind = industry_map.get(code, '')
                if ind and is_blacklisted(ind, code):
                    blacklisted.append(f"{code}({ind})")
                else:
                    filtered.append(code)
            if blacklisted:
                print(f"  排除: {', '.join(blacklisted[:5])}{'...' if len(blacklisted)>5 else ''}", file=sys.stderr)
            candidates = filtered
            print(f"  行业过滤后: {len(candidates)}只候选", file=sys.stderr)

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

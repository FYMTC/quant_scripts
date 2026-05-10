"""
earnings_monitor.py — 财报自动触发监控

每日检查持仓+自选股的财报日历，
如果当天有财报发布，自动写入触发文件，
cron读取后启动TradingAgents分析。

用法：
  python earnings_monitor.py    # 检查今天是否有财报
  python earnings_monitor.py --check   # 仅检查，不写触发
"""

import json
import os
import sys
import subprocess
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional

# ========== 配置 ==========
SCRIPT_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(SCRIPT_DIR, "guard_config.json")
TRIGGER_PATH = os.path.join(SCRIPT_DIR, "earnings_trigger.json")

# 已知财报日历（手动维护 + 可从OmniData获取）
# 格式: {"code": {"name": "xx", "next_date": "2026-04-28", "period": "Q1"}}
KNOWN_EARNINGS = {
    "002594": {"name": "比亚迪", "next_date": None, "period": None},
    "000938": {"name": "紫光股份", "next_date": None, "period": None},
    "600522": {"name": "中天科技", "next_date": None, "period": None},
    "600487": {"name": "亨通光电", "next_date": None, "period": None},
    "002475": {"name": "立讯精密", "next_date": None, "period": None},
    "002466": {"name": "天齐锂业", "next_date": None, "period": None},
    "002560": {"name": "通达股份", "next_date": None, "period": None},
}

# A股财报季规律（大部分公司）
# Q1: 4月30日前
# 半年报: 8月31日前
# Q3: 10月31日前
# 年报: 次年4月30日前


def load_config() -> Dict:
    """加载持仓+自选"""
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH) as f:
        return json.load(f)


def get_watch_stocks() -> List[str]:
    """获取所有需要监控的股票代码（排除ETF）"""
    config = load_config()
    stocks = set()
    
    # 持仓中的股票（非ETF）
    for code in config.get("positions", {}):
        if not code.startswith(("51", "15", "16", "56", "58")):
            stocks.add(code)
    
    # 自选股中的股票
    for code in config.get("watch_list", {}):
        if not code.startswith(("51", "15", "16", "56", "58")):
            stocks.add(code)
    
    return list(stocks)


def search_earnings_web(code: str, name: str) -> Optional[Dict]:
    """从东方财富搜索财报公告"""
    try:
        cmd = f"""curl -s --max-time 10 -X POST http://172.17.0.3:8380/api/v1/spiders/run \
          -H 'Content-Type: application/json' \
          -d '{{"spider_name":"eastmoney_search","params":{{"keyword":"{name} 2026 财报 季报","search_type":"ann"}}}}'"""
        
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return None
        
        data = json.loads(result.stdout)
        if not data.get("success"):
            return None
        
        items = data.get("data", [])
        if not isinstance(items, list) or len(items) == 0:
            return None
        
        today = date.today()
        for item in items[:5]:  # 只看最近5条
            title = item.get("标题", "")
            pub_date_str = item.get("时间", "")[:10]
            
            if "季报" in title or "年报" in title or "业绩" in title:
                try:
                    pub_date = datetime.strptime(pub_date_str, "%Y-%m-%d").date()
                    if pub_date >= today:
                        period = "Q1" if "一季" in title or "第一季" in title else \
                                 "H1" if "半年" in title else \
                                 "Q3" if "三季" in title else \
                                 "年报" if "年报" in title else "未知"
                        return {
                            "date": pub_date_str,
                            "period": period,
                            "title": title[:60],
                        }
                except:
                    continue
    except Exception:
        pass
    return None


def check_earnings_approaching(code: str, name: str) -> Optional[Dict]:
    """检查财报是否临近（财报季边界日期）"""
    today = date.today()
    month, day = today.month, today.day
    
    # Q1截止4月30日 → 4月20日~4月30日都是密集发布期
    if month == 4 and day >= 20:
        return {"date": None, "period": "Q1", "note": "Q1截止4/30，处于密集发布期"}
    
    # 半年报截止8月31日
    if month == 8 and day >= 20:
        return {"date": None, "period": "H1", "note": "半年报截止8/31，处于密集发布期"}
    
    # Q3截止10月31日
    if month == 10 and day >= 20:
        return {"date": None, "period": "Q3", "note": "Q3截止10/31"}
    
    return None


def find_next_earnings(code: str, name: str) -> Dict:
    """查找标的的下一次财报日期"""
    
    # 1. 从已知日历查
    if code in KNOWN_EARNINGS and KNOWN_EARNINGS[code]["next_date"]:
        info = KNOWN_EARNINGS[code]
        return {
            "code": code,
            "name": name,
            "date": info["next_date"],
            "period": info.get("period", "未知"),
            "source": "known_calendar",
        }
    
    # 2. 从东方财富搜索
    web_result = search_earnings_web(code, name)
    if web_result:
        return {
            "code": code,
            "name": name,
            "date": web_result["date"],
            "period": web_result["period"],
            "source": "eastmoney_search",
        }
    
    # 3. 财报季逼近检查
    season = check_earnings_approaching(code, name)
    if season:
        return {
            "code": code,
            "name": name,
            "date": None,
            "period": season["period"],
            "source": "season_check",
            "note": season["note"],
        }
    
    return {
        "code": code,
        "name": name,
        "date": None,
        "period": None,
        "source": "unknown",
    }


def write_trigger(stocks_with_earnings: List[Dict]):
    """写入触发文件"""
    trigger = {
        "time": datetime.now().isoformat(),
        "date": date.today().isoformat(),
        "stocks": stocks_with_earnings,
        "count": len(stocks_with_earnings),
    }
    with open(TRIGGER_PATH, "w") as f:
        json.dump(trigger, f, ensure_ascii=False, indent=2)
    print(f"✅ 写入触发文件: {len(stocks_with_earnings)}只标的今日有财报")


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true", help="仅检查不写触发")
    args = p.parse_args()

    stocks = get_watch_stocks()
    config = load_config()
    watch_names = config.get("watch_list", {})
    
    print(f"📅 财报监控 ({date.today()})")
    print(f"   监控标的: {len(stocks)}只\n")
    
    today_stocks = []
    
    for code in stocks:
        name = watch_names.get(code, config.get("positions", {}).get(code, {}).get("name", code))
        info = find_next_earnings(code, name)
        
        status = "✅" if info["date"] else "⚠️" if info.get("note") else "—"
        
        if info["date"]:
            try:
                info_date = datetime.strptime(info["date"], "%Y-%m-%d").date() if info["date"] else None
                if info_date and info_date <= date.today():
                    status = "🔔 TODAY"
                    today_stocks.append(info)
            except:
                pass
        elif info.get("note"):  # 财报季逼近
            status = "📅 季报期"
        
        print(f"  {status} {name}({code}) | {info.get('period','?')} | {info.get('date',info.get('note','待定'))}")
    
    if today_stocks and not args.check:
        write_trigger(today_stocks)
        print(f"\n🚨 今日{len(today_stocks)}只标的有财报！触发TradingAgents分析")
    elif today_stocks:
        print(f"\n🚨 今日{len(today_stocks)}只标的有财报！(--check模式，未写触发)")
    else:
        print("\n✅ 今日无财报")


if __name__ == "__main__":
    main()

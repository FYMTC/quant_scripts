"""
token_tracker.py — Token使用量追踪

记录每次TradingAgents分析的token消耗，按日/周/月汇总，
帮助控制deepseek-v4-pro的API费用。

用法：
  from token_tracker import TokenTracker
  tracker = TokenTracker()
  tracker.record("BYD分析", input_tokens=123535, output_tokens=6173)
  print(tracker.today_summary())
"""

import json
import os
from datetime import datetime, date
from typing import Dict, List, Optional

TRACKER_PATH = os.path.join(os.path.dirname(__file__), "token_usage.json")

# deepseek-v4-pro 定价（原价，单位：¥/1M tokens）
PRICING = {
    "input": 2.0,    # ¥2/1M input tokens
    "output": 8.0,   # ¥8/1M output tokens  
    "discount": 0.25,  # 当前2.5折
}

class TokenTracker:
    def __init__(self, path=TRACKER_PATH):
        self.path = path
        self._ensure_file()

    def _ensure_file(self):
        if not os.path.exists(self.path):
            with open(self.path, "w") as f:
                json.dump({"records": []}, f)

    def _load(self) -> dict:
        with open(self.path, "r") as f:
            return json.load(f)

    def _save(self, data: dict):
        with open(self.path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def record(self, task: str, input_tokens: int = 0, output_tokens: int = 0):
        """记录一次分析消耗"""
        data = self._load()
        record = {
            "time": datetime.now().isoformat(),
            "date": date.today().isoformat(),
            "task": task,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost_original": self._calc_cost(input_tokens, output_tokens, discount=1.0),
            "cost_actual": self._calc_cost(input_tokens, output_tokens),
        }
        data["records"].append(record)
        self._save(data)

    def _calc_cost(self, inp: int, out: int, discount=None) -> float:
        d = discount if discount is not None else PRICING["discount"]
        cost = (inp / 1_000_000 * PRICING["input"] + 
                out / 1_000_000 * PRICING["output"]) * d
        return round(cost, 4)

    def today_summary(self) -> str:
        """今日汇总"""
        today = date.today().isoformat()
        data = self._load()
        today_records = [r for r in data["records"] if r["date"] == today]

        if not today_records:
            return "今日无记录"

        total_in = sum(r["input_tokens"] for r in today_records)
        total_out = sum(r["output_tokens"] for r in today_records)
        total_cost = sum(r["cost_actual"] for r in today_records)
        tasks = [r["task"] for r in today_records]

        lines = [
            f"📊 Token用量 ({today})",
            f"  分析次数: {len(today_records)}",
            f"  Input: {total_in:,} tokens",
            f"  Output: {total_out:,} tokens",
            f"  总计: {total_in+total_out:,} tokens",
            f"  费用: ¥{total_cost:.2f} (2.5折)",
            f"  原价: ¥{total_cost/PRICING['discount']:.2f}",
        ]
        return "\n".join(lines)

    def query(self, days: int = 7) -> List[dict]:
        """查询最近N天记录"""
        data = self._load()
        cutoff = date.today().isoformat()
        return [r for r in data["records"] 
                if r["date"] >= cutoff][-days*10:]  # 近似筛选


# 便捷函数：从delegate_task结果中记录
def track_from_delegate(name: str, results: list):
    """从delegate_task返回结果中提取token消耗并记录"""
    tracker = TokenTracker()
    for i, r in enumerate(results):
        if isinstance(r, dict):
            tokens = r.get("tokens", {})
            inp = tokens.get("input", 0)
            out = tokens.get("output", 0)
            if inp or out:
                tracker.record(f"{name}分析师{i+1}", inp, out)
    return tracker.today_summary()


if __name__ == "__main__":
    tracker = TokenTracker()
    print(tracker.today_summary())

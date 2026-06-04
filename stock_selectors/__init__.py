"""
selectors/ — 选股策略插件目录
每个策略是一个独立脚本，暴露 score(code) -> dict 接口。
引擎自动发现并调用所有已注册的策略。
"""
import json
import os
from typing import Dict, List, Optional

_REGISTRY_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "selector_registry.json")


def load_registry() -> dict:
    if not os.path.isfile(_REGISTRY_PATH):
        return _default_registry()
    try:
        with open(_REGISTRY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _default_registry()


def _default_registry() -> dict:
    return {
        "version": 1,
        "selectors": [
            {
                "id": "momentum_breakout",
                "name": "动量突破",
                "module": "stock_selectors.momentum_breakout",
                "weight": 0.4,
                "enabled": True,
                "description": "基于动量、波动率、量比的综合评分",
            },
            {
                "id": "rdagent_factors",
                "name": "RD-Agent因子",
                "module": "stock_selectors.rdagent_factors",
                "weight": 0.35,
                "enabled": True,
                "description": "消费 RD-Agent 周末生成的稳定因子（IC 加权）",
            },
            {
                "id": "low_vol_value",
                "name": "低波价值",
                "module": "stock_selectors.low_vol_value",
                "weight": 0.25,
                "enabled": True,
                "description": "低波动 + 合理估值 + 正动量的防守型选股",
            },
        ],
    }


def active_selectors() -> List[dict]:
    reg = load_registry()
    return [s for s in reg.get("selectors", []) if s.get("enabled", True)]


def score_with_selector(selector: dict, code: str) -> Optional[dict]:
    """调用单个策略对单只股票评分。策略必须暴露 score(code) -> dict 接口。"""
    module_path = selector.get("module", "")
    try:
        mod = __import__(module_path, fromlist=["score"])
        if hasattr(mod, "score"):
            return mod.score(code)
    except Exception:
        return None
    return None

"""
统一系统配置加载器
===================
所有路径从此模块获取，不再硬编码。

用法:
    from system_config import cfg
    db_path = cfg.path.trade_db          # 点号访问
    data_dir = cfg.get("data_dir")       # 或 get()
    root = cfg.root                      # 等价 cfg.system.root

环境变量覆盖: 设置 QUANT_SYSTEM_ROOT=/new/path 可覆盖 system.root
现有 env 兼容: QUANT_RUNTIME_DATA_DIR / STOCK_KB_DB_PATH 等继续生效
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).resolve().parent / "system_config.yaml"

# 环境变量 → 配置路径 映射表
_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "QUANT_SYSTEM_ROOT": ("system", "root"),
    "QUANT_WIKI_ROOT": ("system", "wiki_root"),
    "QUANT_HERMES_ROOT": ("system", "hermes_root"),
    "QUANT_RUNTIME_DATA_DIR": ("paths", "data_dir"),
    "QUANT_WIKI_REPORTS_DIR": ("paths", "wiki_reports_dir"),
    "STOCK_KB_DB_PATH": ("paths", "trade_db"),
    "QUANT_TRADE_DB_PATH": ("paths", "trade_db"),
    "GUARD_CONFIG_PATH": ("paths", "guard_config"),
}


class _ConfigView:
    """支持点号访问的配置视图"""

    def __init__(self, data: dict):
        object.__setattr__(self, "_data", data)

    def __getattr__(self, key: str) -> Any:
        data = object.__getattribute__(self, "_data")
        if key not in data:
            raise AttributeError(f"config key not found: {key}")
        val = data[key]
        if isinstance(val, dict):
            return _ConfigView(val)
        return val

    def __repr__(self):
        return repr(object.__getattribute__(self, "_data"))


class SystemConfig:
    def __init__(self):
        self._raw: dict = {}
        self._resolved: dict = {}
        self._view: _ConfigView | None = None
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        if _CONFIG_PATH.is_file():
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                self._raw = yaml.safe_load(f) or {}
        self._apply_env_overrides()
        self._resolved = self._resolve_refs(self._raw)
        self._view = _ConfigView(self._resolved)
        self._loaded = True

    def _apply_env_overrides(self):
        for env_key, path in _ENV_OVERRIDES.items():
            val = os.environ.get(env_key)
            if val:
                d = self._raw
                for key in path[:-1]:
                    if key not in d:
                        d[key] = {}
                    d = d[key]
                d[path[-1]] = val

    def _resolve_refs(self, obj: Any, depth: int = 0, _root: dict | None = None) -> Any:
        if depth > 10:
            raise RecursionError("config variable depth exceeded")
        if _root is None:
            _root = self._raw
        if isinstance(obj, dict):
            return {k: self._resolve_refs(v, depth, _root) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._resolve_refs(v, depth, _root) for v in obj]
        if isinstance(obj, str) and "${" in obj:
            return self._interpolate(obj, _root)
        return obj

    def _interpolate(self, value: str, root: dict) -> str:
        """递归替换 ${xxx.yyy} 引用"""

        def _resolve_dotpath(path: str) -> Any:
            parts = path.strip().split(".")
            node = root
            for p in parts:
                if isinstance(node, dict):
                    node = node.get(p)
                else:
                    return ""
            return node if isinstance(node, (str, int, float)) else ""

        pattern = re.compile(r"\$\{([^}]+)\}")
        result = value
        max_iter = 5
        for _ in range(max_iter):
            new_result = pattern.sub(lambda m: str(_resolve_dotpath(m.group(1))), result)
            if new_result == result:
                break
            result = new_result
        return result

    # ---- Public API ----

    def reload(self):
        """强制重新加载（用于配置热更新）"""
        self._loaded = False
        self._load()

    def get(self, key: str) -> Any:
        """按 key 获取 paths 下的值，如 cfg.get('trade_db')"""
        self._load()
        return self._resolved.get("paths", {}).get(key)

    @property
    def root(self) -> str:
        self._load()
        return self._resolved["system"]["root"]

    @property
    def path(self) -> _ConfigView:
        self._load()
        return _ConfigView(self._resolved.get("paths", {}))

    @property
    def system(self) -> _ConfigView:
        self._load()
        return _ConfigView(self._resolved.get("system", {}))

    @property
    def services(self) -> _ConfigView:
        self._load()
        return _ConfigView(self._resolved.get("services", {}))

    @property
    def python(self) -> str:
        self._load()
        return self._resolved["python"]["binary"]

    @property
    def data_dir(self) -> str:
        self._load()
        return self._resolved["paths"]["data_dir"]

    @property
    def all(self) -> dict:
        self._load()
        return self._resolved


# 全局单例
cfg = SystemConfig()

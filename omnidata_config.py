"""
omnidata_config.py — OmniData 地址/端口统一配置

P3-1 修复: 环境变量化，消除硬编码。所有引用统一走此模块。

环境变量:
  OMNIDATA_BASE_URL — OmniData 基础 URL（默认 http://localhost:8380）
  OMNIDATA_API_URL   — API 完整地址（默认 {BASE_URL}/api/v1）

用法:
  from omnidata_config import OMNIDATA_BASE_URL, OMNIDATA_API_URL
"""

import os

OMNIDATA_BASE_URL = os.environ.get(
    "OMNIDATA_BASE_URL",
    "http://localhost:8380"
)

OMNIDATA_API_URL = os.environ.get(
    "OMNIDATA_API_URL",
    f"{OMNIDATA_BASE_URL}/api/v1"
)

OMNIDATA_MCP_URL = os.environ.get(
    "OMNIDATA_MCP_URL",
    f"{OMNIDATA_BASE_URL}/mcp/finance/"
)

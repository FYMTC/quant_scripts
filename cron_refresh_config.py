"""盘中数据刷新 cron job（仅 script，无 LLM）。"""
from system_config import cfg

REFRESH_JOB_IDS = frozenset({
    "38a1c0401a1d",  # 开盘闪电战 flash
    "718bad2ea1fe",  # 盘中快照 midday
    "81c08b8f2cbe",  # 午间 noon
    "6907661c0a15",  # 下午 afternoon
    "1af47883139e",  # 收盘 close
})

REFRESH_POLL_PROMPT = "no-agent：仅 data_refresh_app，刷新 *_output.json，不唤 LLM。"

EMERGENCY_SIGNAL = cfg.path.guard_emergency_signal
EMERGENCY_FILE = cfg.path.guard_emergency

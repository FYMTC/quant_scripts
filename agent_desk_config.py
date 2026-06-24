"""Agent Desk cron 双 job 配置（轮询无 LLM / 有任务才唤 LLM）。"""

from system_config import cfg

DESK_PENDING_PATH = cfg.path.agent_desk_pending
DESK_POLL_CRON_ID = "76ef0dd15954"
DESK_LLM_CRON_ID = "a7f3e81d9llm"
DESK_PENDING_MAX_AGE_SEC = 180

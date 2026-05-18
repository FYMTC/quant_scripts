"""Agent Desk cron 双 job 配置（轮询无 LLM / 有任务才唤 LLM）。"""

DESK_PENDING_PATH = "/config/quant_scripts/data/agent_desk_pending.json"
DESK_POLL_CRON_ID = "76ef0dd15954"
DESK_LLM_CRON_ID = "a7f3e81d9llm"
DESK_PENDING_MAX_AGE_SEC = 180

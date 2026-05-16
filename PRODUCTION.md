# 生产脚本索引（v5，2026-05-17）

> 定时入口在 `/config/.hermes/scripts/*_app.py`；核心逻辑在本目录。归档见 [archive/README.md](archive/README.md)。

## 主链路

| 模块 | 文件 |
|------|------|
| 传感器 | `smart_guard_v3.py` |
| 队列 / Desk | `agent_queue.py`, `agent_desk.py` |
| 信号环 | `signal_loop.py`, `stock_signal_profile.py` |
| 门禁 / 约束 | `decision_gate.py`, `core/constraints.py` |
| 出站 | `trade_outbox.py` |
| 记忆 | `stock_kb.py`（`portfolio --live`、`trade`、`trade-undo`） |
| 量化 | `risk_metrics.py`, `market_regime.py`, `position_sizer.py`, `tradingagents_runner.py`, … |
| 定时 apps | `apps/morning.py`, `flash.py`, `midday.py`, `noon.py`, `afternoon.py`, `close.py`, `night.py` |
| 自检 | `v5_self_check.py`, `tests/` |

## Wiki

- 架构：`quant-wiki/concepts/system-architecture.md`
- Hermes：`quant-wiki/concepts/hermes-v5-agent-prompts.md`
- 自然语言：`/.hermes/memories/QUANT-V5-USER-DIALOGUE.md`

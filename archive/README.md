# 📦 归档脚本说明

归档时间：2026-05-07 | 最后清理：**2026-05-17**（v5 整理）

这些脚本不再被系统使用，但保留代码不删，方便反悔。

## 清理记录

| 日期 | 文件 | 操作 |
|:----|:-----|:-----|
| 2026-05-09 | signal_push.py | **删除** — 依赖已归档的 rl_inference.py，功能已被cron系统取代（交易日cron + TradingAgents）|
| 2026-05-17 | earnings_monitor.py | **删除** — 空文件，无实现 |
| 2026-05-17 | live_signals / parallel_analysts / consolidate_report / api_audit / signal_dashboard / rolling_predict | **移入 archive** — 无生产链引用或已被 v5/TA runner 取代 |

## 2026-05-17 新增归档

| 脚本 | 归档原因 |
|------|----------|
| `live_signals.py` | FinRL 实盘信号，无 cron；依赖已归档模型 |
| `parallel_analysts.py` | 旧 TA 并行 CLI，由 `tradingagents_runner` 取代 |
| `consolidate_report.py` | 旧综合裁决 CLI；Gate1 逻辑在 `decision_gate.py` |
| `api_audit.py` | 开发期 API 统计，非生产链 |
| `signal_dashboard.py` | 未接入 cron（TODO 待接） |
| `rolling_predict.py` | 仅 `archive/daily_report.py` 引用 |

## 归档清单

| 脚本 | 大小 | 归档原因 | 备注 |
|------|:----:|:---------|:----:|
| daily_report.py | 7.2K | 被cron系统 + DB持久化取代 | 以前生成文本日报 |
| eastmoney_data.py | 6.4K | 被OmniData MCP取代 | 东方财富HTTP爬虫已废 |
| four_stock_report.py | 5.6K | 无人调用 | 短命脚本，早于TradingAgents |
| position_alerts.py | 3.4K | 死脚本 | 持仓预警，未接入系统 |
| rdagent_demo.py | 3.4K | 演示脚本 | RD-Agent随便跑的样例 |
| rdagent_factors.py | 6.4K | 旧版RD因子 | 被rd_agent_quant.py取代 |
| rd_auto_factor.py | 13K | 实验性因子 | 早期自动化因子挖掘尝试 |
| test_multiindex.py | 1.1K | 测试脚本 | 多级索引测试 |
| test_new_libs.py | 3.0K | 测试脚本 | 新库可用性测试 |
| tomorrow_check.py | 4.6K | 死脚本 | 明日检查，未接入cron |

### FinRL实验组（待定）
| 脚本 | 大小 | 状态 |
|:-----|:----:|:----:|
| finrl_astock_pipeline.py | 2.4K | 未接入系统 |
| sb3_ppo_train.py | 5.1K | 未接入系统 |
| ppo_position_train.py | 4.5K | 未接入系统 |
| super_ppo_train.py | 6.2K | 未接入系统 |
| rl_inference.py | 8.6K | 死循环（仅signal_push import它） |
| signal_push.py | 4.1K | 死循环（仅rl_inference import它） |
| finrl_astock_env.py | 10.6K | FinRL环境，无cron引用 |
| finrl_astock_data.py | 9.2K | FinRL数据，无cron引用 |
| finrl_astock_trainer.py | 8.4K | FinRL训练器，无cron引用 |

**保留未归档的RL相关**：
- `rd_agent_quant.py` — 被周末周报cron skill引用，保留

FinRL组代码完整但从未通过cron跑过。方案：要么接入周末周报做RL信号参考，要么保持归档。

**保留未归档**（被活跃脚本引用，无法安全移除）：
- data_converter.py — 6个脚本依赖
- risk_portfolio.py — smart_guard_v3引用
- finrl_astock_env/data/trainer — FinRL核心库，互联引用

## 恢复方式
```shell
mv archive/<script_name>.py .
```

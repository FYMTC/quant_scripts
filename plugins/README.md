# quant_scripts/plugins — 可扩展量化插件（P5）

1. 复制 `_template.py` → `your_factor.py`
2. 实现 `run(ctx: dict) -> dict` 返回 JSON 可序列化结果
3. 登记 `data/quant_registry.yaml`（`status: experimental` → 回测 → `production`）
4. 在 `agent_desk.py` / `morning_plan_app` / `night_preflight` 中按 registry 加载（目标态）

`ctx` 常用字段：`code`, `closes`, `date`, `factor_library_path`

# services/ —— 后端模块化单体（目录轴=模块，2026-09-27 用户裁决）

一级目录=模块（交付边界）；模块内子包约定：`api/`(路由+DTO) `domain/`(模型+仓储 Protocol) `business|core|retrieval|runtime`(逻辑) `data/`(ORM+仓储实现)。
全局装配=gateway（中间件链+路由挂载），全局底座=platform（config/security/deps/errors/kernel/ports/llm/db）。
逻辑分层 L2~L7 为约束概念（权威=docs/architecture/01 §1.2/§4）；import 治理=pyproject importlinter 十契约。

| 模块 | 职责 | 里程碑 |
| ---- | ---- | ---- |
| iam | 租户/用户/角色/APIKey/审计 | M1 ✅ |
| agent | 会话/任务/编排/内核 | M1 ✅ / M3 |
| ontology | TBox/SHACL/lint/版本/changeset | M1-M2 |
| kb | 文档/抽取流水线/检索 | M2 |
| review | 候选终审（横切） | M2.2 |
| memory / plugin / mcp / writeback / rsi | 记忆/插件/MCP/回写/自进化 | M3-M5 |
| sandbox | 沙箱与执行环境 | 并行线 |

门禁：ruff + lint-imports(10 契约) + mypy(domain/core strict) + pytest（51+）。

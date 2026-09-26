# business/ — L3 业务层（用例编排）

> 状态：v0.1 | 日期：2026-09-26 | 上游依据：[架构锚点](../../docs/architecture/01-总体架构与分层.md)
> 细节权威：docs/architecture/03-业务层设计.md（已定稿，本文只写要点）

## 1. 职责与边界

- 用例编排（application services）：事务边界、跨模块流程、领域事件发布；**不含业务规则本身**（规则在 L4/L5）。
- 禁止：实现 HTTP 细节、写 SQL。七步请求链路（检索→校验→生成→回写等）在本层串装（锚点 §3.5）。

## 2. 目录结构规划

```text
business/
├── chat_orchestrator.py    # 对话编排：记忆→检索→校验→生成→回写（对话核心闭环）
├── agent_runtime/          # agent 工具托管运行时插槽：下发任务/回收事件流/注入平台工具
├── kb_pipeline/            # 抽取流水线（七步，见 docs/OntRAG）
├── review_workflow/        # 人工终审工作流（候选非成品门禁，横切各编排器）
├── memory_service/         # 记忆读写/沉淀编排（L1~L4，含 L2→L3 升级审核）
├── plugin_lifecycle/       # 插件上架审核/安装/启停/升级
└── events/                 # 领域事件发布（Outbox 出口）
```

## 3. 核心机制要点

- **UoW + Outbox**：每个用例一个 Unit of Work 事务边界；领域事件与业务写同事务写 outbox 表，由 events/ 后台投递，保证「库改了事件必达」。
- **候选非成品门禁**：LLM 产物（候选实例/记忆/插件/变更单）一律先建 review 工单，`review_workflow` 终审通过才转权威数据（锚点 §6.5）；P4/P7/P9 的审核动作最终都落在这里。
- **SSE 事件生产**：编排过程产生 AG-UI 语义事件（文本增量/工具调用/状态），交 gateway/sse 编码下发；对话流式经 `chat_orchestrator` 驱动。
- **任务化**：抽取、批量审核、插件安装等长任务统一落 `task` 聚合并发事件，前端任务中心消费。

## 4. 接口契约

- **依赖**：L4（聚合与仓储接口）、L5（ontology.*/knowledge.* 能力接口）、L7（llm/mcp/插件运行时/msg）。
- **被谁调用**：gateway/routers 各模块；MCP 能力经 CapabilityProvider 注册给 infra/mcp_gateway（倒置点之二）。
- **提供**：各用例服务接口（`chat.send`、`kb.extract`、`review.decide`、`plugin.install` 等，命名与 docs/architecture/03 定稿对齐）。

## 5. 数据模型要点

- 本层不定义表结构；消费 L4 聚合（agent/session/task/document/memory/plugin/review…）与 L6 的 UoW/仓储；自有持久化仅 **outbox 事件表**（经 L6）。

## 6. 开发指南与验收标准

- 用例服务必须有显式入参/出参 dataclass、事务注解与 trace 打点；编排器之间不互相直接调用（经领域事件解耦）。
- 验收（对齐锚点 M2~M4）：M2 抽取流水线最小版（文档→候选→审核→入图）；M3 对话编排 SSE 可用、L1/L2 记忆回写；M4 插件安装与上架审核闭环。

## 7. 待办与开放问题

- [ ] 重任务（抽取/批量导入）队列选型：Redis 轻量队列 vs 引入 broker（锚点 §3.1 遗留）。
- [ ] agent_runtime 插槽与 docs/Agent 接口对齐（设计已定稿）。
- [ ] review_workflow 工单状态机定稿（pending/approved/rejected/withdrawn + 批量语义）。

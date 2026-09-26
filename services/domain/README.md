# domain/ — L4 领域层（纯 Python）

> 状态：v0.1 | 日期：2026-09-26 | 上游依据：[架构锚点](../../docs/architecture/01-总体架构与分层.md)
> 细节权威：docs/architecture/04-领域层设计.md（已定稿，本文只写要点）

## 1. 职责与边界

- 核心域模型与规则：聚合根/实体/值对象、业务不变式、领域事件、**仓储接口（仅 Protocol）**、领域服务接口。
- **零框架依赖**：纯 Python + pydantic；禁止 import FastAPI/SQLAlchemy/任何存储客户端（锚点 §2）。

## 2. 目录结构规划

```text
domain/
├── model/      # 9 个聚合根：agent / session / task / ontology /
│               #   document / memory / plugin / tool / review
├── repo/       # 仓储接口（typing.Protocol，按聚合一一对应）
├── events/     # 领域事件定义（dataclass，含事件名/载荷/版本）
└── services/   # 领域服务接口（如本体校验门禁，实现倒置到 L5）
```

## 3. 核心机制要点

- **不变式内聚**：状态变更只能经聚合根方法（如 `review.approve(actor)` 校验终审权限与状态机），外部不得直接改属性。
- **依赖倒置点一**：`repo/` 只定义 Protocol；实现（L6 `repo_impl/`）在运行时注入——L4 永不 import L6。
- **领域服务接口**：涉及语义能力判断的领域规则（如「变更单发布前必须通过 SHACL 校验」）在 `services/` 定义接口，由 L5 实现，保持 L4 纯净。
- **领域事件**：状态变更产生事件（`CandidateApproved`、`ChangesetPublished`…），由 L3 经 Outbox 投递。
- **值对象优先**：可整体比较与替换的小对象（三元组、置信度、来源指针、tenant_id）用 frozen dataclass 表达，收窄可变状态面。

## 4. 接口契约

- **依赖**：无（不允许依赖任何其他层）。
- **被谁调用/实现**：L3 消费聚合与事件；L6 实现 `repo/` 接口；L5 实现 `services/` 门禁接口。

## 5. 数据模型要点

- 聚合根本身即权威模型：agent（工具托管配置/适配器）、session（会话与消息）、task（状态机）、ontology（版本/变更单）、document（分片/抽取产物指针）、memory（L1~L4 分层）、plugin（版本/scope）、tool（注册元数据）、review（工单）。
- 落库形态由 L6 ORM 表达，L4 只保证内存态不变式；大文本一律存指针（MinIO object key），不进聚合字段。

## 6. 开发指南与验收标准

- 每个聚合必须有纯单测覆盖不变式与状态机（不连任何存储）；事件命名过去式、带 `occurred_at` 与 `tenant_id`。
- 验收：import-linter 断言 L4 无外部框架 import；9 聚合单测全绿；仓储 Protocol 与 L6 实现签名一致性由 mypy strict 保证。

## 7. 待办与开放问题

- [ ] 9 聚合的字段级定稿（随 docs/architecture/04 编写，联动 L6 ORM 与迁移）。
- [ ] `ontology` 聚合与 L5 rdflib 制品的边界（权威在制品，聚合持指针+元数据）需在 04 篇明确。
- [ ] 领域事件 schema 版本化策略（加字段向后兼容约定）。
- [ ] `task` 聚合状态机与前端任务中心状态枚举（docs/frontend/02 P2）对齐。

# data/ — L6 数据层（仓储实现与多存储）

> 状态：v0.1 | 日期：2026-09-26 | 上游依据：[架构锚点](../../docs/architecture/01-总体架构与分层.md)
> 细节权威：docs/architecture/06-数据层设计.md（已定稿，本文只写要点）

## 1. 职责与边界

- 仓储接口实现、SQLAlchemy ORM 模型、Alembic 迁移、五存储引擎客户端、Unit of Work。
- 禁止：包含业务规则（不变式在 L4）；本层是 L4 Protocol 的「哑实现」+ 存储细节封装。

## 2. 目录结构规划

```text
data/
├── orm/          # SQLAlchemy 2.0 模型（PG 全部业务表）
├── repo_impl/    # L4 仓储 Protocol 的实现（每聚合一个）
├── clients/      # pg / neo4j / milvus / minio / redis 五客户端封装
├── migrations/   # Alembic（版本化迁移，禁止手改库）
└── uow.py        # Unit of Work：PG 事务边界 + Outbox 同事务写
```

## 3. 核心机制要点

- **UoW**：L3 用例开一个 UoW；PG 事务内完成聚合持久化 + outbox 事件写入，事件由后台投递器发往 events 通道。
- **多租户强制过滤**：所有 PG 表带 `tenant_id`，仓储基类注入过滤；Redis key / Milvus collection / Neo4j 标签按 tenant 隔离命名（锚点 §6.1）。
- **五存储职责**（锚点 §3.2，什么数据放哪里）：

| 存储 | 放什么 | 不放什么 |
| ---- | ---- | ---- |
| PostgreSQL | 用户/租户/权限、会话与任务元数据、审核工单、本体版本与变更单、插件/工具注册、审计、记忆 L1/L2 结构化底座 | 大文本原文（存 MinIO 指针） |
| Neo4j | ABox 知识图谱、TBox 属性图物化（n10s）、组织记忆 L3 时序图 | 权威 TBox（权威在 rdflib 制品） |
| Milvus | 文档切片向量、社区摘要向量、记忆嵌入 | 结构化查询 |
| MinIO | 原始文档、抽取中间产物、插件包、OWL/Turtle 制品 | 需事务/关联查询的数据 |
| Redis | L1 会话工作记忆、分布式锁、限流计数、轻量任务队列 | 需持久审计的数据（落 PG） |

## 4. 接口契约

- **依赖**：L7（config/security/observability）；**实现** L4 `repo/` Protocol（倒置点一）；**被** L3/L5 调用。
- 客户端封装只暴露领域化方法（`save_aggregate`、`hybrid_search`），不向调用方泄漏 SDK 类型。

## 5. 数据模型要点

- ORM 表与 L4 九聚合一一映射（agent/session/task/ontology/document/memory/plugin/tool/review）+ outbox 表 + 审计表；大文本字段一律 `object_key` 指针。
- 迁移纪律：模型改动必出 Alembic revision；迁移脚本 forward-only，回滚走新迁移。

## 6. 开发指南与验收标准

- 仓储实现单测用真实 PG（testcontainers/docker-compose），禁止 mock SQL 验证；Neo4j/Milvus 客户端封装有契约测试。
- 验收（对齐锚点 M1/M2）：M1 一条实体 CRUD 经 UoW 全层贯通、迁移可重放；M2 Neo4j/Milvus 入库与混合检索通过。

## 7. 待办与开放问题

- [ ] 全量表结构 DDL 定稿（随 docs/architecture/06 与 L4 聚合字段定稿同步）。
- [ ] Neo4j 多租户隔离粒度（库 per tenant vs 标签/属性隔离）需 06 篇裁决。
- [ ] Redis 轻量任务队列与后续 broker 的切换预案。

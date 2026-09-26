# gateway/ — L2 网关层（FastAPI）

> 状态：v0.1 | 日期：2026-09-26 | 上游依据：[架构锚点](../../docs/architecture/01-总体架构与分层.md)
> 细节权威：docs/architecture/02-网关层设计.md（已定稿，本文只写要点）

## 1. 职责与边界

- 平台唯一流量入口：REST API + SSE 事件流出口；认证鉴权、限流、审计、租户上下文注入、DTO 校验（pydantic）。
- 禁止：写业务规则、直接访问存储。网关只是协议与治理层，一切用例交给 L3。

## 2. 目录结构规划

```text
gateway/
├── app.py          # FastAPI 应用工厂 create_app()、lifespan（启动/关闭钩子）
├── deps.py         # 依赖注入：当前用户 / 租户 / 会话 / 分页
├── middlewares/    # CORS / RequestID / JWT / 租户 / 限流 / 审计 / 异常
├── schemas/        # pydantic DTO（请求/响应），与领域模型严格分离
├── sse/            # AG-UI 蓝本事件流编码器（EventPayload → SSE 帧）
└── routers/        # agents / sessions / ontology / kb / memory / plugins / mcp / admin
```

## 3. 核心机制要点

- **应用工厂**：`create_app()` 按环境装配中间件链并挂载 routers；lifespan 内完成 L3/L5 能力向 L7 的注册（CapabilityProvider 注入点之一）。
- **中间件链（固定顺序）**：CORS → RequestID（生成/透传 `trace_id`）→ JWT → 租户上下文 → 限流（Redis 计数）→ 审计 → 异常兜底（统一错误体 `{code, message, trace_id}`）。
- **SSE 事件流出口**：`StreamingResponse` + `sse/` 编码器；事件语义以 AG-UI 16 事件为蓝本（RUN_STARTED、文本增量、工具调用、状态，见 docs/研究整理/02）；输出关缓冲（`X-Accel-Buffering: no`）、带心跳、支持 `Last-Event-ID` 续传。
- **DTO 与领域模型分离**：`schemas/` 只做传输校验与文档（OpenAPI），进 L3 前转为领域入参；响应由 DTO 显式组装，杜绝 ORM/聚合对象直接出网。

## 4. 接口契约

- **依赖**：仅 L3（`business`）；横切能力（限流计数、密钥）经 `infra`。
- **被谁调用**：frontend（Web 控制台）、cli；对外还暴露 MCP Server 出口与 A2A Agent Card（由 `infra/mcp_gateway` 承载，网关挂路由）。
- **对外约定**：所有路由前缀 `/api/v1`；错误体统一结构；分页 `page/page_size`；写操作全审计。

## 5. 数据模型要点

- 本层**无自有 ORM 模型、不落表**；审计事件、限流计数分别交由 L3（落 PG 审计表）与 infra（Redis）持久化，网关只生产事件。

## 6. 开发指南与验收标准

- 路由文件与 `routers/` 一一对应模块地图（锚点 §5）；每个路由必须有 pydantic 入出参 + `Operation-ID` 稳定命名（前端按此对齐）。
- 验收（对齐锚点 M1）：JWT/租户/审计中间件生效；一条实体的 CRUD 示例从前端经网关贯通到存储；无权限请求 403、无 token 401 均带统一错误体。
- 质量：ruff + mypy 通过；路由单测覆盖鉴权与错误体；SSE 单测覆盖心跳与断线帧序。

## 7. 待办与开放问题

- [ ] SSE 鉴权细节：生产用一次性 stream ticket（前端待办对齐项）。
- [ ] OpenAPI 导出与前端类型生成（docs/frontend/03 §13 待办）的协作方式。
- [ ] 限流策略分档（按租户/按用户/按接口）参数定稿。

# infra/ — L7 基础层（横切能力）

> 状态：v0.1 | 日期：2026-09-26 | 上游依据：[架构锚点](../../docs/architecture/01-总体架构与分层.md)
> 细节权威：docs/architecture/07-基础层设计.md（已定稿，本文只写要点）；MCP 设计见 docs/MCP/、插件见 docs/Skills/（已定稿）

## 1. 职责与边界

- 收拢横切能力：模型网关、MCP 网关、插件运行时/沙箱、消息、可观测、配置、安全。
- 禁止：依赖 L1~L6 任何模块；只允许依赖外部系统（模型 API、MCP Server、消息中间件）。

## 2. 目录结构规划

```text
infra/
├── llm/              # LiteLLM 网关封装：多提供商路由/兜底/预算/成本追踪、Claude 直连 prompt caching
├── mcp_gateway/      # server/（平台能力出口）、client/（外部 MCP 接入）、registry/、a2a/（Agent Card）
├── plugin_runtime/   # 插件守护进程 + 沙箱（Dify plugin-daemon 模式）
├── msg/              # 消息/队列封装（Redis 起步，后续 broker）
├── observability/    # OTel traces/metrics/logs，trace_id 贯穿
├── config/           # 配置加载（环境分层），全平台唯一读环境变量处
└── security/         # 密钥管理、scope 定义、ACL 原语
```

## 3. 核心机制要点

- **CapabilityProvider 依赖倒置**（锚点 §3.4，倒置点之二）：L7 定义协议，L3/L5 启动时把能力实现**注册**进 mcp_gateway 注册表；网关只认协议不认实现，MCP 出口由此自动获得平台能力。
- **模型网关**：内部统一 OpenAI Chat Completions 兼容协议，LiteLLM 承担多提供商/预算/兜底/成本追踪；Claude 保留直连通道吃 prompt caching（docs/研究整理/02 结论）。
- **协议栈落点**：MCP（远程 Streamable HTTP / 本地 stdio，兼容 2025-06-18 与 2025-11-25 协商）+ A2A v1.0（签名 Agent Card 发现与任务委托）。
- **安全红线**：MCP `annotations` 仅作 UI 提示、不作授权依据；工具调用授权走 scope + RBAC（锚点 §6.2）。
- **可观测**：每次 LLM/MCP/工具调用记录成本与延迟，挂接当前 trace_id（锚点 §6.7）。

## 4. 接口契约

- **依赖**：仅外部系统（模型 API、外部 MCP Server、消息中间件）+ 本层 config/security。
- **被谁调用**：L3（llm/msg/plugin_runtime）、L5（llm/clients 能力）、gateway（SSE 经 msg 的旁路、运维面）；能力注册方向相反（L3/L5 → 注册表）。

## 5. 数据模型要点

- 无自有业务表；注册表/会话态缓存放 Redis，密钥放 config/security（环境注入，禁止入库明文）；成本与调用记录作为事件流交 L3 落 PG 审计/成本表。

## 6. 开发指南与验收标准

- 对外部系统的每个客户端必须有契约测试（录制/回放或 mock server）；沙箱内插件进程不得访问平台密钥环境变量。
- 验收（对齐锚点 M3/M4）：M3 LiteLLM 流式接入与成本记录；M4 MCP Server 出口 + 外部 MCP 接入 + 插件市场最小闭环（外部 Agent 经 MCP 调平台能力）。

## 7. 待办与开放问题

- [ ] 插件沙箱隔离级别定稿（进程级 vs 容器级，随 docs/Skills 编写）。
- [ ] A2A Agent Card 与平台本体「行动类」映射（研究整理 02 待办）。
- [ ] LiteLLM 预算/配额的租户级策略参数。

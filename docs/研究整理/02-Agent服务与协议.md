# 02 - Agent 服务与协议（模型协议 + Agent 协议）

> 状态：v0.1 | 调研时间：2026-09 | 依据：各协议官方规范/官网、GitHub 仓库、官方文档
>
> Agent 服务是平台托管 agent 工具的运行时：**对下**用模型协议接模型，**对上/对横**用 Agent 协议对接前端与其他 Agent。

---

## 1. Agent 服务职责

- **托管 agent 工具**：为 nanobot / openclaw / hermes / claude / pi 等提供标准运行时插槽（下发任务、回收事件流、注入平台工具，见 [01 篇](./01-Agent工具调研.md)）；
- **会话与任务管理**：会话生命周期、多租户隔离、任务状态机；
- **模型接入**：统一模型网关（见下）；
- **平台能力注入**：把知识检索、本体推理、记忆、工具以 MCP/内置工具形式注入各 agent 工具的上下文。

## 2. 模型协议（对下）

| 协议 | 核心机制 | 现状 |
|---|---|---|
| **OpenAI Chat Completions** | 无状态：每次传完整 `messages`；`/v1/chat/completions` + tool calling（JSON Schema） | **事实标准**，几乎所有厂商/网关/框架默认兼容 |
| **OpenAI Responses API**（2025-03） | **有状态**：`store: true` + `previous_response_id` 服务端保存历史；原生集成 MCP 与内置工具（web_search/file_search）；面向 agent 场景 | OpenAI 推荐的演进方向；Assistants API 已定于 2026-08-26 停服。社区实测延迟或高 2–3 倍（未确认）；官方口径收益：SWE-bench 约 +3%、缓存利用率 +40–80% |
| **Anthropic Messages API** | `/v1/messages`；tool use；**Prompt Caching**（`cache_control` 断点，缓存读取折扣计价）；Message Batches（10 万请求/24h，5 折） | Claude 系直连首选；缓存+批处理是两大降本杠杆；Anthropic 也是 MCP 发起方 |

**模型网关**：

- **LiteLLM**（BerriAI/litellm）：Python SDK + Proxy，统一 100+ 提供商；API Key 集中管理、团队预算/配额、限流、fallback、成本追踪、日志回调——**生产级首选**；
- **One-API**（songquanpeng/one-api）：Go 轻量 OpenAI 接口分发/渠道管理，部署简单、中文社区流行；企业功能（观测/预算/回退）弱于 LiteLLM——轻量场景可选；
- 同类：OpenRouter、Portkey、Kong AI Gateway。网关核心价值：多模型路由/负载均衡/兜底、Key 与配额管理、成本追踪、语义缓存。

**平台选型**：内部以 **OpenAI Chat Completions 兼容协议为最大公约数**；网关用 LiteLLM（生产）或 One-API（轻量）；为 Claude 保留直连通道吃 prompt caching 红利；跟进 Responses API 的会话态/内置工具能力。

## 3. Agent 协议（对上/对横）

### 3.1 MCP（Model Context Protocol）——Agent ↔ 工具/上下文

- 定位："AI 应用的 USB-C"，JSON-RPC 2.0，借鉴 LSP；Anthropic 发起。
- **规范版本演进**：2025-06-18（**Streamable HTTP 取代旧 HTTP+SSE**，stdio 仍为本地标准）→ 2025-11-25（引入 **Tasks** 长任务、URL-based elicitation）→ 当前 latest **2026-07-28**（Tasks 转为 opt-in 扩展）。
- **能力协商**：`initialize` 握手，客户端声明 sampling/roots/elicitation，服务端返回 tools/resources/prompts 等能力并协商版本；Streamable HTTP 用 `Mcp-Session-Id` 维持会话。
- **安全要点**：工具的 `annotations`（readOnlyHint/destructiveHint 等）属**不可信元数据**，不能作为授权依据。
- 生态：官方 MCP Registry（`server.json` 元数据，2025-09 preview）；Smithery、mcp.so、Glama、PulseMCP 为发现层。

### 3.2 A2A（Agent2Agent）——Agent ↔ Agent

- 定位：Agent 间**发现、能力协商与任务委托**（互为不透明服务，不共享内存/工具）。
- 时间线：Google 2025-04 发布 → 2025-06 捐入 **Linux Foundation** → **v1.0.0 于 2026-03 发布**；2026-08 有公告称加入 LF 旗下 Agentic AI Foundation（治理细节以官方为准）。
- 机制：**Agent Card**（`/.well-known/agent-card.json`）能力发现，v1.0 头号特性**签名 Agent Card**（可验证身份）；传输 JSON-RPC over HTTP 与 **gRPC**；认证委托 OAuth 2.0 / OIDC。
- 注：IBM/BeeAI 的 ACP（REST 风格 agent 间协议）**已并入 A2A**——agent 协议领域的首个重要收敛。

### 3.3 其他

- **Zed ACP**（Agent Client Protocol，2025-08）：JSON-RPC over stdio，"LSP for AI agents"，面向**编辑器 ↔ 编码 Agent**（Zed、JetBrains 已支持）；
- **AG-UI**（Agent-User Interaction Protocol）：CopilotKit 主导的轻量**事件流协议**，16 种标准事件（RUN_STARTED、文本流、共享状态增量、工具调用生成等），SSE/WebSocket；已适配 LangGraph、CrewAI、AG2、Mastra。

### 3.4 互补关系

```
MCP：agent 接工具/数据（纵向）
A2A：agent 找 agent 干活（横向）
AG-UI：agent 流到界面上（人机层）
Zed ACP：编辑器驱动编码 agent（IDE 场景）
```

## 4. 平台协议选型结论

| 方向 | 选型 | 说明 |
|---|---|---|
| 对下（模型） | OpenAI Chat Completions 兼容 + LiteLLM 网关；Claude 直连通道 | 生态最大公约数 + 降本（caching/batches） |
| 对上（前端） | **AG-UI 式 SSE 事件流**（16 事件语义作蓝本） | 流式输出、共享状态、工具调用可视化 |
| Agent 间（对外互联） | **A2A v1.0**（Agent Card + 签名卡 + 任务委托） | 平台 MCP Server 同时发布 Agent Card，供外部 Agent 发现 |
| 工具接入 | **MCP**（远程 Streamable HTTP，本地 stdio），兼容 2025-06-18 / 2025-11-25 协商 | 与 [05-MCP平台能力](./05-MCP平台能力.md) 对齐 |

---

## 5. 待办与开放问题

- [ ] Agent 服务语言栈决策（与 MCP Server、插件守护进程同栈：Python FastAPI vs TypeScript/Node）
- [ ] AG-UI 事件流自研 or 直接引入 CopilotKit 生态组件 PoC
- [ ] A2A Agent Card 与平台本体注册信息的映射（本体"行动类"→ Agent Card skill 描述）

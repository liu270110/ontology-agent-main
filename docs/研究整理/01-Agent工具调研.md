# 01 - Agent 工具调研（nanobot / openclaw / hermes agent / claude / piagent）

> 状态：v0.1 | 调研时间：2026-09-25 | 方法：GitHub API 元数据核验 + README/官方文档原文 + 交叉搜索
>
> 这五个工具是平台要托管的"agent 工具"候选。所有结论均有来源；未确认处已标注。

---

## 0. 名称歧义澄清（重要）

| 调研名 | 最终识别 | 同名混淆项 |
|---|---|---|
| nanobot | **HKUDS/nanobot**（Python 个人 AI agent 框架，48.5k stars，该名字下当前最知名） | ① nanobot-ai/nanobot 现已迁移为 **obot-platform/nanobot**（Go 版 MCP Host，1.3k stars，**已进维护模式**）；② FloatTech/NanoBot 等为无关 QQ 机器人框架 |
| openclaw | **openclaw/openclaw**（前身 Clawdbot → Moltbot → OpenClaw，因 Anthropic 商标问题两度更名） | 无 |
| hermes agent | **NousResearch/hermes-agent**（248k stars） | 注意与 Nous Research 的 Hermes **模型系列**区分；与 Meta 的 Hermes JS 引擎无关 |
| claude | Anthropic **Claude Code**（agent harness/CLI）+ **Claude Agent SDK**（原 Claude Code SDK，2025 年末更名） | 无 |
| piagent | **badlogic/pi-mono**，已迁移更名为 **earendil-works/pi**（Mario Zechner / badlogic，libGDX 作者） | 与 Inflection AI 的对话产品 "Pi" 无关 |

---

## 1. nanobot（HKUDS/nanobot）

**一句话**：超轻量、自托管的 Python 个人 AI agent 框架（OpenClaw 的极简替代品）。

- **仓库**：<https://github.com/HKUDS/nanobot>（48.5k stars，Python ≥3.11，MIT，v0.3.5）
- **架构**：数千行可读核心代码实现 agent 主循环、工具调用、长期记忆、MCP 接入、模型路由、多 agent 委托与定时自动化。三种形态：`nanobot webui`（内置工作台）、TUI/CLI、`nanobot gateway`（常驻网关，Docker/systemd 部署）。
- **扩展**：内置工具（文件、shell、网页搜索/抓取、cron、图像生成、subagents）经 `@` 提及挂载；技能（Skills）经 `$` 调用；**MCP 客户端**（Apps 面板接 MCP server）；是否可作 MCP server 对外暴露未确认。
- **模型**：任何 OpenAI 兼容 API（含 Ollama/vLLM 本地部署），支持 fallback 与模型路由。
- **记忆**：会话历史 + "Dream" 长期记忆；上下文压缩；会话分支。
- **平台集成**：提供 **Python SDK + OpenAI 兼容 API**，可 Docker 托管；渠道覆盖 Telegram/Discord/Slack/微信·飞书/邮件等。

### 1b. 同名项目：obot-platform/nanobot（Go）——不建议新依赖

独立部署的 **MCP Host**（`nanobot run` 在 localhost:8080 以 HTTP 暴露 MCP；配置即 agent：`nanobot.yaml` + `agents/*.md`），同时具备 MCP 客户端与服务端双重角色。**但项目已进维护模式**（外部 PR/issue 关闭，官方指向 MCP Go SDK 与后继项目 "mmmcp"），Apache 2.0，alpha 状态。

## 2. openclaw（openclaw/openclaw）

**一句话**：运行在自己硬件上的开源个人 AI 助理，通过现有聊天软件（WhatsApp/Telegram/Discord/iMessage/Slack/Teams 等 20+）指挥它干活。

- **仓库**：<https://github.com/openclaw/openclaw>（390k stars，TypeScript，Node 24+，MIT；2025-11 发起，现由 OpenClaw Foundation 运营）
- **架构**：**Gateway（本地控制平面）**统一管理 sessions/tools/events/渠道连接，可装为守护进程；Control UI/CLI/TUI 都是 Gateway 前端。设计哲学："trusted gateway, untrusted execution, deterministic policy"——入站消息视为不可信输入，陌生发信人需配对审批，工具默认跑宿主机、可选沙箱。
- **扩展**：**tools / skills / plugins** 三层，plugin bundle 可映射为 skills、hooks 和 MCP 工具；**ClawHub 市场分发**；模型 provider 本身也是可插拔 plugin。
- **MCP：双向**。客户端支持 Streamable HTTP / SSE / stdio、工具过滤、OAuth、热重载、按会话工具审批；服务端 `openclaw mcp serve` 把 Gateway 会话暴露给其他 MCP 客户端。
- **平台集成**：作为"自托管网关"部署后经其 Gateway 接口/渠道对接；偏"终端用户产品"而非嵌入式引擎，托管建议容器化整网关。

## 3. hermes-agent（NousResearch/hermes-agent）

**一句话**：Nous Research 出品的"自我改进 agent"——从经验中自动沉淀技能、带内生学习循环的开源 Python agent。

- **仓库**：<https://github.com/NousResearch/hermes-agent>（249k stars，Python，MIT，2025-07 创建）
- **架构**：完整 TUI + **单一网关进程**对接 30+ 消息平台；**7 种终端后端**（local、Docker、SSH、Singularity、Modal、Daytona、Vercel Sandbox——后两者支持 serverless 休眠）；subagent 并行；Python 脚本经 RPC 调用工具。
- **扩展**：40+ 内置工具按 toolsets 组织；**学习循环**（自动技能创建/自改进）；兼容 agentskills.io 开放技能标准；**MCP 双向**（客户端 + v0.6.0 起 server 模式，后者细节部分来自第三方评测，未确认）。
- **模型**：Nous Portal（300+ 模型订阅）、OpenRouter、OpenAI、自定义端点；provider-agnostic。
- **记忆**：FTS5 全文会话检索 + LLM 摘要跨会话回忆；持久用户画像。
- **平台集成**：`hermes claw migrate` 支持从 OpenClaw 一键迁移；7 种远程终端后端对平台做沙箱化托管是加分项；有官方托管版（价格细节未确认）。

## 4. claude（Claude Code / Claude Agent SDK）

**一句话**：Anthropic 官方 agent harness——Claude Code 是驱动其 CLI 的 agent 循环，Claude Agent SDK（原 Claude Code SDK）把同一套 harness 以库的形式开放。

- **仓库**：TS SDK <https://github.com/anthropics/claude-agent-sdk-typescript>；文档 <https://code.claude.com/docs/en/agent-sdk/overview>（npm 包 `@anthropic-ai/claude-agent-sdk`，旧包已弃用；Python SDK 同步提供）
- **架构**：在自己的进程里运行 Claude Code 二进制（**非开源内核**）；agent loop + 工具执行 + 上下文管理一体；其他语言可经子进程跑 CLI `-p` + JSON 流式输出驱动同一循环。
- **扩展**：hooks（生命周期钩子）、subagents、**MCP 客户端**、权限模式（自动放行 vs 需审批）、Skills/命令/记忆（`.claude/` 目录体系，Agent Skills 开放标准）、Plugins（打包 skills+agents+hooks+MCP servers）。
- **模型**：**仅 Claude 系**（一方 API / Bedrock / Vertex / Microsoft Foundry；`ANTHROPIC_BASE_URL` 可指向 LiteLLM 等网关，网关后接非 Claude 模型属非官方用法，未确认）。
- **许可证：专有**（Anthropic 商业条款）：禁止 "Claude Code" 品牌露出；面向客户产品须 API key 认证；2026 年计费政策有变动动向——**接入前需法务确认条款版本**。

## 5. pi（earendil-works/pi，原 badlogic/pi-mono）

**一句话**：Mario Zechner 的"极简可编程 agent harness"——自我可扩展的终端编码 agent + 可嵌入的 TypeScript agent 工具箱。

- **仓库**：<https://github.com/badlogic/pi-mono>（重定向至 earendil-works/pi；109k stars，TypeScript，MIT；CLI 包 `@earendil-works/pi-coding-agent`，Node ≥22.19）
- **架构**：monorepo 分层——`pi-ai`（统一多 provider LLM API）→ `pi-agent-core`（运行时）→ `pi-coding-agent`（CLI）；另有 pi-durable（持久运行时）、chord（应用编排）、pi-telemetry。无内置沙箱，官方建议容器化。
- **扩展**：**Extensions**（TS/JS 模块，`registerTool/registerCommand/on/registerProvider`，拥有完整 OS 权限）；**Skills**（SKILL.md，agentskills.io 规范）；packages（npm/git 分发）。**MCP：无内置**（官方文档未提及，社区称"pi 拒绝 MCP"未获官方确认；接入需写桥接扩展）。
- **模型**：调研中最广（Anthropic/OpenAI/Gemini/DeepSeek/Moonshot/Qwen/MiniMax 等 20+ provider，含国产模型），支持 OAuth 订阅/API key/云凭据。
- **会话**：**JSONL 树结构**（id/parentId 支持原地分支 `/tree`）、compaction 压缩、context_edit——工程化最佳。
- **平台集成**：四种自动化形态——print 模式、**JSON 事件流模式**、**RPC 模式**（控制独立 pi 进程）、**TypeScript SDK** 内嵌。

---

## 6. 横向对比

| 维度 | nanobot (HKUDS) | nanobot (obot, Go) | openclaw | hermes-agent | claude (Code/SDK) | pi |
|---|---|---|---|---|---|---|
| **定位** | 极简自托管个人 agent | 独立 MCP Host（停维） | 自托管个人 AI 助理 | 自我改进 agent | 官方 agent harness | 极简可编程工具箱 |
| **形态** | WebUI+TUI+网关 | HTTP 服务 | Gateway 守护进程 | TUI+网关 | CLI/SDK/子进程 JSON 流 | CLI+print/JSON/RPC+SDK |
| **扩展** | @工具、$技能、插件 | 纯配置式 | tools/skills/plugins 三层 | toolsets+自动技能 | hooks/subagents/skills/plugins | TS 扩展 API+SKILL.md |
| **MCP** | 客户端 | 双向 | **双向** | **双向** | 客户端 | 无内置 |
| **模型协议** | OpenAI 兼容 | OpenAI/Anthropic+4 dialect | 可插拔 provider 插件 | Nous Portal/OpenRouter 等 | **仅 Claude** | 20+ provider（最广） |
| **许可证** | MIT | Apache 2.0 | MIT | MIT | **专有** | MIT |
| **热度** | 48.5k★ | 1.3k★ | 390k★ | 249k★ | 官方包 | 109k★ |
| **集成难度** | **低** | 低（停维慎用） | 中 | 中 | 低-中（商业条款） | 低-中 |

## 7. 平台托管建议（ontology-rsi-harness 视角）

1. **首选编程接口而非"包壳 CLI"**：`claude`（TS/Python SDK + 流式 JSON）与 `pi`（RPC 模式 + TS SDK）提供最干净的控制平面（下发任务、回收事件流、注入工具），适合做成平台的标准 agent 运行时插槽；`pi` 胜在 MIT + 任意模型，可作非 Claude 模型的 harness。
2. **把 MCP 当统一工具总线**：五个候选中四个支持 MCP。平台侧做"工具市场"，agent 工具按 MCP 客户端身份接入工具集；pi 用桥接扩展或评估其 TS 工具 API 直连。
3. **服务型接入选 nanobot（HKUDS）**：OpenAI 兼容 API + 网关/Docker 对平台最友好；偏单用户场景，多租户需平台自己做会话隔离。
4. **openclaw / hermes 是"终端用户产品"**：以消息渠道和本地网关为中心、工具落在宿主机（安全边界大）。托管它们应容器化整网关、走渠道/网关接口，沿用其审批机制；hermes 的 7 种远程终端后端利于沙箱化。
5. **合规**：MIT/Apache 系可放心二开商用；**Claude Agent SDK 专有授权 + 计费政策变动**，接入前法务确认。
6. **维护风险**：Go 版 nanobot 已停维；pi 刚更名迁移（依赖锁定 `@earendil-works/*` 新坐标）。

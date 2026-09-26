# 02 - SSE 事件流协议（前端 ↔ 网关）

> 状态：v0.1 | 日期：2026-09-26 | 上游依据：[architecture/02-网关层设计 §5](../architecture/02-网关层设计.md)（**事件集 / 分波 / 帧格式 / 回放窗口的权威上游**）、[architecture/01-总体架构与分层 §3.3](../architecture/01-总体架构与分层.md)（协议栈：AG-UI 16 事件语义为蓝本）、[Agent/Agent服务设计 §6.3](../Agent/Agent服务设计.md)、[architecture/08-横切关注点 §5](../architecture/08-横切关注点与工程规范.md)（降级三件套中的事件通知）
>
> 本篇是对外发布的 SSE 协议契约。事件名、载荷字段、波次划分以 architecture/02 篇 §5 为准；本篇补充项（STEP_* 事件、重连退避建议值）已标注，待回填上游。

## 1. 协议定位

- **谁和谁说话**：L1 前端（及 CLI）↔ L2 网关，承载**对话与任务**的实时事件流；REST（01 篇）负责管理与 CRUD，SSE 只做服务端→客户端单向推送；
- **语义蓝本**：AG-UI 16 事件语义（研究整理 02 §3.3）——平台取其子集并加平台扩展事件（RETRIEVAL_EVIDENCE、GATE_VERDICT）；
- **两个 SSE 端点**（01 篇 §5.2）：
  - `POST /api/v1/sessions/{id}/messages`（`Accept: text/event-stream`）：发消息并直接以 SSE 流接收本次 Run 的事件；
  - `GET /api/v1/sessions/{id}/events`：纯订阅 / 断线重连（`Last-Event-ID`）。
- **事件先落库再推送**：事件先写 `task_events`（seq 单调递增）再推送（Agent §4），保证可回放、可审计。

## 2. 传输与帧格式

| 项 | 约定 |
| ---- | ---- |
| Content-Type | `text/event-stream` |
| 其他响应头 | `Cache-Control: no-cache`、`X-Accel-Buffering: no`（禁代理缓冲） |
| 帧构成 | `id:` 行 + `event:` 行 + `data:` 行（JSON），每帧以空行结尾 |
| `id` | 每会话独立单调递增序列（Redis INCR），即 `task_events.seq`，供断线重连定位 |
| 心跳 | 每 **15s** 一条注释帧 `: ping\n\n`（**建议值，压测后冻结**——02 篇 §5），防代理层空闲断连，**不计入事件序列** |
| 编码 | UTF-8；`data` 为单行 JSON（换行转义） |

帧示例（02 篇 §5 原样）：

```
id: 1739
event: TEXT_MESSAGE_CONTENT
data: {"message_id":"m_01J9","delta":"本体的"}

```

## 3. 事件总表（分波）

> 分波裁决（02 篇 §5）：**M3 主干波** = RUN / TEXT / TOOL 三族共 **9 个事件**（锚点称「8 事件」系 `tool_call_args` 单列成行所致，以表内实际行为准）；其余为 **M4+ 扩展波**。两波帧格式不变、协议向前兼容：M3 网关不外发扩展波事件，前端对未知 event 名一律忽略。
> 2026-09-26 评审修复F：TOOL_CALL_RESULT 与 RETRIEVAL_EVIDENCE **提前入主干波**（M3 主干波扩为 11 个事件）——M3 出口要求「有引用」，引用事件不能后置；属 §7 规则 4 允许的向后兼容变更。

| 事件名 | 方向 | 波次 | data 载荷 schema | 触发时机 | 前端处理建议 |
| ---- | ---- | ---- | ---- | ---- | ---- |
| RUN_STARTED | S→C | M3 | `{run_id, session_id, task_id}` | 编排器受理消息、task 置 running | 清空上一轮渲染态，标记「运行中」，记录 run_id |
| TEXT_MESSAGE_START | S→C | M3 | `{message_id}` | 一条新助手消息开始输出 | 创建消息气泡占位 |
| TEXT_MESSAGE_CONTENT | S→C | M3 | `{message_id, delta}` | 每个文本增量（token 级） | 追加渲染（Markdown 增量解析） |
| TEXT_MESSAGE_END | S→C | M3 | `{message_id, finish_reason}` | 该消息输出完毕 | 定稿渲染、落会话消息列表 |
| TOOL_CALL_START | S→C | M3 | `{tool_call_id, tool_name}` | 编排器决定调用工具 | 展示工具调用卡片（运行中态） |
| TOOL_CALL_ARGS | S→C | M3 | `{tool_call_id, delta}` | 工具参数增量 | 参数折叠区实时拼装 |
| TOOL_CALL_END | S→C | M3 | `{tool_call_id}` | 参数收集完成、开始执行 | 卡片转「执行中」 |
| TOOL_CALL_RESULT | S→C | M3 主干（2026-09-26 评审修复F：提前入主干波——M3 出口要求「有引用」） | `{tool_call_id, ok, summary, cost_ms}` | 工具执行完成 | 卡片转成功 / 失败态，展示 summary |
| STATE_SNAPSHOT | S→C | M4+ | `{snapshot}` | 共享状态全量下发（重连 / 开始） | 整体替换本地任务状态 |
| STATE_DELTA | S→C | M4+ | `{patch}`（JSON Patch） | 共享状态增量（进度、检查点） | 应用 JSON Patch |
| MESSAGES_SNAPSHOT | S→C | M4+ | `{messages}` | 会话消息全量校正 | 校正本地消息列表（防漂移） |
| RETRIEVAL_EVIDENCE | S→C | M3 主干（2026-09-26 评审修复F：提前入主干波——M3 出口要求「有引用」） | `{chunks, graph_paths, degraded}` | knowledge.search 返回后（引用先行，Agent §4） | 渲染引用列表；`degraded=true` 须标注「证据链暂缺」 |
| GATE_VERDICT | S→C | M4+ | `{ok, violations, ontology_version}` | 本体校验（OntologyGate）完成 | 校验失败时红标违规项与所对本体版本 |
| STEP_STARTED ★ | S→C | M4+ | `{step_name}` | 编排阶段切换：retrieve → validate → generate → persist | 阶段进度指示 |
| STEP_FINISHED ★ | S→C | M4+ | `{step_name}` | 阶段完成 | 阶段进度打勾 |
| RUN_FINISHED | S→C | M3 | `{run_id, usage}` | task 成功终态 | 解除「运行中」，展示 usage（tokens / cost） |
| RUN_ERROR | S→C | M3 | `{run_id, code, message}` | task 失败终态（code 为平台错误码，§4.3 of 01 篇） | 错误条幅 + 按 code 提示重试 / 联系管理员 |

> ★ STEP_STARTED / STEP_FINISHED 为本篇补充（依据 Agent §6.3 事件表；02 篇 §5 以「等平台扩展」涵盖但未单列），**待回填 02 篇分波表**。
> 载荷口径差异登记：Agent §6.3 中 RUN_STARTED 载荷含 `agent_id`、RUN_ERROR 含 `retryable`，与 02 篇 §5（`{run_id, session_id, task_id}` / `{run_id, code, message}`）不一致——**以 02 篇为准**，是否并入 `agent_id` / `retryable` 登记 §8 待办。

M3 主干波一次成功对话的典型序列：

```
RUN_STARTED → TEXT_MESSAGE_START → TEXT_MESSAGE_CONTENT* → TEXT_MESSAGE_END
            → [TOOL_CALL_START → TOOL_CALL_ARGS* → TOOL_CALL_END → TOOL_CALL_RESULT]*   ← 工具循环
            → RETRIEVAL_EVIDENCE（knowledge.search 之后，引用先行）
            → TEXT_MESSAGE_*（工具结果后的继续输出）
            → RUN_FINISHED
```

### 3.1 主干波载荷逐字段 schema（M3 必须）

| 事件 | 载荷字段与类型 | 约束 |
| ---- | ---- | ---- |
| RUN_STARTED | `run_id: string`、`session_id: string`、`task_id: string` | 一次消息受理恰好一条；与 202 响应 / tasks 查询同 id |
| TEXT_MESSAGE_START | `message_id: string` | 一条助手消息开始；后续同 message_id 的 CONTENT/END 归属它 |
| TEXT_MESSAGE_CONTENT | `message_id: string`、`delta: string` | delta 为**增量**文本，前端自行拼接；不重发全量 |
| TEXT_MESSAGE_END | `message_id: string`、`finish_reason: string` | finish_reason ∈ stop / length / tool_calls / cancelled |
| TOOL_CALL_START | `tool_call_id: string`、`tool_name: string` | 平台工具与 agent 自有工具统一呈现（Agent §4） |
| TOOL_CALL_ARGS | `tool_call_id: string`、`delta: string` | 参数 JSON 增量片段，END 后拼装结果须可解析 |
| TOOL_CALL_END | `tool_call_id: string` | 参数收集完成、开始执行；结果在 TOOL_CALL_RESULT（已随 2026-09-26 评审修复F 提前入 M3 主干） |
| RUN_FINISHED | `run_id: string`、`usage: object` | usage 含 tokens / cost（LiteLLM 回传，Agent §6.3） |
| RUN_ERROR | `run_id: string`、`code: integer`、`message: string` | code 为平台错误码（01 篇 §4.3），终态后不再有任何事件 |

### 3.2 扩展波载荷细则（M4+；其中 TOOL_CALL_RESULT / RETRIEVAL_EVIDENCE 已随 2026-09-26 评审修复F 提前入 M3 主干，载荷 schema 不变）

```json
TOOL_CALL_RESULT
{ "tool_call_id": "tc_01J9", "ok": true,
  "summary": "检索到 8 个切片、2 条图谱路径", "cost_ms": 612 }

RETRIEVAL_EVIDENCE        // 核心业务字段与 messages.citations 同构（Agent §6.3 注）
{ "chunks": [{ "doc_id": "d_01", "chunk_id": "c_017", "quote": "……", "score": 0.83 }],
  "graph_paths": [{ "nodes": ["OutageEvent", "Feeder"], "edges": ["locatedOn"] }],
  "degraded": false }     // degraded=true：图库或向量库单侧降级（08 篇 §5），前端必须显式标注

GATE_VERDICT
{ "ok": false,
  "violations": [{ "focus": "...#DEVICE_001", "constraint": "sh:in", "message": "状态不在枚举内" }],
  "ontology_version": "v3" }

STATE_DELTA               // RFC 6902 JSON Patch
{ "patch": [{ "op": "replace", "path": "/progress/retrieve", "value": "done" }] }

MESSAGES_SNAPSHOT
{ "messages": [{ "id": "m_01J8", "role": "user", "content": "……", "seq": 231 }] }
```

### 3.3 帧流样例（一次带工具调用的对话开头）

```
id: 1740
event: RUN_STARTED
data: {"run_id":"r_01JB3K","session_id":"s_01J9Z8","task_id":"t_01JB3K"}

id: 1741
event: TEXT_MESSAGE_START
data: {"message_id":"m_01J9"}

id: 1742
event: TEXT_MESSAGE_CONTENT
data: {"message_id":"m_01J9","delta":"我先检索相关工单……"}

id: 1743
event: TOOL_CALL_START
data: {"tool_call_id":"tc_01J9","tool_name":"knowledge.search"}

id: 1744
event: TOOL_CALL_ARGS
data: {"tool_call_id":"tc_01J9","delta":"{\"query\":\"城东 10kV 停电\",\"mode\":\"local\"}"}

id: 1745
event: TOOL_CALL_END
data: {"tool_call_id":"tc_01J9"}

```

## 4. 断线重连

| 项 | 契约 |
| ---- | ---- |
| 续传凭证 | `Last-Event-ID` 请求头（或 `?last_event_id=` 查询参数兜底），值为断开前收到的最后一帧 `id` |
| 回放机制 | 任意副本从 Redis Stream `sse:{tenant_id}:{session_id}` 中该 id 之后回放历史帧，再无缝切实时消费——**事件不重不漏** |
| 回放窗口 | Stream `MAXLEN 1000`、`TTL 10min`（均为建议值，压测后冻结——02 篇 §5） |
| 超窗行为 | 返回错误码 **4301 SSE_REPLAY_EXPIRED**（HTTP 映射建议 410） |
| 4301 处理 | 客户端**不再续传**：`GET /sessions/{id}/messages`（before_id 游标）拉全量历史校正本地状态，然后不带 Last-Event-ID 重新订阅 |
| 重连退避（建议） | 1s → 2s → 4s → … 指数退避，上限 30s，附 ±20% 抖动；收到心跳即视为链路健康 |
| 服务端排空 | 网关收 SIGTERM 后停止接流，排空进行中 SSE 流上限 30s（建议值，02 篇 §2）——客户端遇连接关闭按退避重连 |

## 5. 多副本语义

- **写入与推送分离**（02 篇 §5 评审修订）：事件统一写入 Redis Stream，SSE 推送一律从 Stream 消费后转发，**网关进程内不缓冲事件状态**；
- 推论：网关副本**无状态**，会话不绑副本——任意副本可服务任意会话的重连与订阅，水平扩容无需会话粘性；
- M3 出口条件：双副本压测——同一会话两个连接分别落在两个网关副本时，Last-Event-ID 跨副本断线重连正确、事件不重不漏（02 篇 §8）。

## 6. 安全

| 项 | 契约 |
| ---- | ---- |
| 事件流鉴权 | 与 REST 同一 JWT（scope `session:chat`）；EventSource 无法自定义 header，允许 `?access_token=` 兜底（02 篇 §3 ③） |
| 租户隔离 | Stream key 内嵌 `{tenant_id}`（08 篇 §1），重连回放经网关鉴权后按 key 定位，跨租户会话返回 403/404 |
| 事件内容 | 流内不携带密钥 / 凭据；工具参数增量（TOOL_CALL_ARGS）如含敏感字段由网关按审计同款脱敏规则截断 |
| 并发限制 | SSE 并发 20 流 / 用户（02 篇 §3 ⑤，超限 429 + 2005） |
| 审计 | 事件先落 `task_events` 再推送（§1），流内容可追溯 |

## 7. 版本演进规则

1. **只增不改**：已发布事件的名称与载荷字段**不删、不改名、不改语义**；新增字段必须可选且前端可忽略；
2. **未知事件忽略**：前端对未知 `event:` 名一律静默忽略（M3 前端须能忽略 M4+ 事件，反之亦然）——这是分波交付可行的前提；
3. **扩展波事件逐个对齐**：M4+ 事件随实现逐个与 AG-UI 官方 schema 对齐（02 篇 §9 待办：自研 vs 引入 CopilotKit 组件 PoC）；
4. **波次即里程碑承诺**：某事件标注的波次 = 该事件最早可用的里程碑；事件从扩展波「提前」进入主干波属向后兼容变更，反之不允许。

## 8. AG-UI 四段可视化映射（2026-09-26 合入：旧稿 16 篇增量吸收）

前端把事件流渲染为四段（对齐 AG-UI 语义与对话工作台布局）：

| 可视化段 | 事件 | 前端呈现 |
| ---- | ---- | ---- |
| 对话流 | RUN_*、TEXT_MESSAGE_* | 气泡与增量文本 |
| 工具时间线 | TOOL_CALL_*（含 RESULT） | 折叠时间线：工具名/参数摘要/耗时/成败 |
| 证据面板 | RETRIEVAL_EVIDENCE | 引用卡片（chunk 出处+图谱路径+degraded 标注） |
| 门禁徽章 | GATE_VERDICT | 通过/违例徽章（违例点击定位） |

STATE_* / MESSAGES_SNAPSHOT 用于重连校正，不直接渲染。

## 9. 待办与开放问题

- [ ] STEP_STARTED / STEP_FINISHED 回填 02 篇 §5 分波表（本篇补充）；
- [ ] RUN_STARTED 载荷是否并入 `agent_id`、RUN_ERROR 是否并入 `retryable`（02 篇 vs Agent §6.3 口径统一）；
- [ ] 心跳 15s、回放窗口（MAXLEN 1000 / TTL 10min）、服务端排空 30s——三项建议值压测后冻结（02 篇 §9）；
- [ ] 4301 的 HTTP 状态映射（建议 410 Gone）在 02 篇 §7 登记；
- [ ] 双副本压测执行（M3 出口条件，结论回填 02 篇 §8）；
- [ ] RETRIEVAL_EVIDENCE 的 `chunks` 内部字段（doc_id / chunk_id / quote / score）与 messages.citations 的同构关系在 OntRAG 篇定稿后回填本篇载荷 schema 细则；
- [ ] M5 阶段评估消息推送是否升级 WebSocket 双向通道（当前 SSE + REST POST 组合已满足需求，最小够用原则下不预建）。
- [ ] ★ 25 篇对标缺口事件预登记（2026-09-26）：`MESSAGE_BRANCH_CREATED`（消息编辑=分叉，api/01 §5.2 branch 端点配套，X3）与**用量水位事件**（Run 内 context 占用 20% 档位推送+终值，X8）待事件分波表评审后入 §3；事件名以本篇评审定稿为准。

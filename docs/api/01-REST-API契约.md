# 01 - REST API 契约（/api/v1）

> 状态：v0.1 | 日期：2026-09-26 | 上游依据：[architecture/02-网关层设计](../architecture/02-网关层设计.md)（端点表 / 错误码 / SSE 的**权威上游**）、[architecture/01-总体架构与分层](../architecture/01-总体架构与分层.md)（§3.3 协议栈）、[architecture/08-横切关注点与工程规范](../architecture/08-横切关注点与工程规范.md)（§2 JWT / RBAC / scope、§2.4 治理档位）、[Agent/Agent服务设计 §6](../Agent/Agent服务设计.md)、[ontology/本体核心设计 §7](../ontology/本体核心设计.md)、[memory/多层记忆设计 §5](../memory/多层记忆设计.md)、[Skills/技能与插件设计](../Skills/技能与插件设计.md)、[MCP/业务回写设计](../MCP/业务回写设计.md)
>
> 本篇是对外发布的 **REST API 评审契约**，也是**全量端点登记册的权威**（2026-09-26 裁决：02 篇 §4 改为代表性端点，端点清单以本篇为准；认证/分页/错误码/SSE 协议机制的权威仍是 architecture/02 篇）。标注「**本篇补充**」（★）的行为本篇补全项；与上游冲突处以 02 篇 / MCP 篇机制口径为准并登记 §9 待办。

## 1. 总则

| 约定项 | 值 | 说明 |
| ---- | ---- | ---- |
| Base URL | `/api/v1` | 全部路由统一前缀（02 篇 §4） |
| 版本策略 | **URL 版本** | 破坏性变更发 `/api/v2`，`/api/v1` 进入维护期；不使用 header 版本协商 |
| Content-Type | `application/json; charset=utf-8` | 例外：文件上传 `multipart/form-data`、SSE `text/event-stream`、MCP 端点 `application/json`（JSON-RPC 2.0，见 03 篇） |
| 字段命名 | snake_case | DTO 字段与 API JSON 一致（02 篇 §6） |
| 未知字段 | 一律拒绝 | pydantic `extra="forbid"`，多传字段返回 3001 |
| OpenAPI | FastAPI 自动生成，`GET /docs` 可视化 | 本篇是**评审契约**（人读），OpenAPI schema 是**机器事实**（前端 / CLI 据此生成客户端）；不一致时走 §8 变更流程裁决 |
| ID 格式 | UUID（新实体建议 UUIDv7） | 路径参数命名 `{snake_case_id}` |
| 时间格式 | ISO 8601 / RFC 3339（UTC） | 如 `2026-09-26T08:30:00Z` |
| 健康探针 | `GET /api/v1/healthz`（liveness）、`GET /api/v1/readyz`（readiness，任一依赖不可用返回 503） | 匿名白名单（02 篇 §2、§3 ③） |

## 2. 认证

### 2.1 认证通道（引 08 篇 §2.0）

| 通道 | 主体 | 凭据 | 入口 |
| ---- | ---- | ---- | ---- |
| 前端 / CLI | 用户 | JWT（access + refresh） | REST / SSE |
| 外部 Agent / 平台外系统 | 机器 | API Key（仅存哈希，scopes ⊆ owner 用户 scopes） | MCP 网关（见 03 篇 §6） |
| agent 工具 ↔ 后端 | 平台托管运行时 | 内部短时 JWT（网关签发，TTL ≤ 5min） | REST / MCP |

### 2.2 JWT Bearer

- 请求头：`Authorization: Bearer <access_token>`；RS256 验签，校验 exp / iss / aud；
- access 默认 2h、refresh 默认 14d；吊销黑名单在 Redis（`jti`，TTL=剩余有效期）；
- SSE 路由允许 `?access_token=` 兜底（EventSource 无法自定义 header，02 篇 §3 ③）。

JWT claims（权威定义见 [08 篇 §2.1](../architecture/08-横切关注点与工程规范.md)，此处为引用摘要）：

| claim | 类型 | 说明 |
| ---- | ---- | ---- |
| `sub` | UUID | 用户 id |
| `tenant_id` | UUID | 租户 id（租户上下文唯一来源） |
| `roles` | string[] | 角色码：admin / ontologist / curator / member / guest（+平台级 super_admin） |
| `scopes` | string[] | 展平权限集，网关据此做声明式鉴权（deny-by-default，精确匹配不支持通配符） |
| `typ` | string | access / refresh |
| `jti` / `iat` / `exp` | UUID / int / int | 令牌唯一 id 与有效期 |

### 2.3 错误体示例（401 / 403）

```json
HTTP/1.1 401 Unauthorized
{ "code": 1003, "message": "令牌已过期", "detail": { "exp": 1795900000 }, "trace_id": "req_01J9X7" }

HTTP/1.1 403 Forbidden
{ "code": 2001, "message": "scope 不足", "detail": { "required": "session:chat", "granted": ["session:read"] }, "trace_id": "req_01J9X8" }
```

## 3. 通用约定

### 3.1 分页与响应包裹

- 偏移分页：请求 `?page=1&page_size=20`（page_size 上限 100）；列表响应统一包裹：

```json
{ "data": [ /* 资源数组 */ ],
  "meta": { "page": 1, "page_size": 20, "total": 137 } }
```

- 游标分页：历史消息等时序资源用 `?before_id={message_id}` 向前翻页（Agent §6.2），响应 `meta.next_before_id` 为空表示翻尽；
- 非 列表/分页 响应同样包裹 `{data, meta}`（`meta` 可为空对象）；SSE 流、204/206、文件下载不包裹。

### 3.2 幂等

- 写操作（POST/PATCH/PUT/DELETE）可携带 `Idempotency-Key` 请求头：同键重复请求返回首次处理结果，不产生重复副作用（键 TTL 24h，存 PG + Redis `idem:*` 双落，08 篇 §5 通用降级纪律）；
- 业务回写的幂等键由平台生成：`{tenant_id}:{action_instance_id}`（见 03 篇 §7），与调用方 `Idempotency-Key` 相互独立。

### 3.3 trace_id 透传

- 请求可带 `X-Request-ID`（无则网关生成 uuid）；响应头原样回显 `X-Request-ID` 与 `X-Trace-ID`（CORS `EXPOSE_HEADERS` 已放行，02 篇 §3 ①）；
- 错误体 `trace_id` 与 `X-Request-ID` 同值，用于全链路（L1→L7、OTel）检索。

### 3.4 租户上下文

- `tenant_id` 一律取自 JWT claim，不由请求参数指定；
- 路径 / 请求体中的租户相关参数与 claim 不一致 → 403 + 2003 TENANT_MISMATCH；租户被停用 → 403 + 2004（02 篇 §3 ④）。

## 4. 错误响应体与错误码总表

### 4.1 错误体（固定四字段，02 篇 §7）

```json
{ "code": 3001, "message": "参数校验失败",
  "detail": [{ "field": "content", "issue": "String should have at least 1 character" }],
  "trace_id": "req_01J9X7" }
```

### 4.2 分段总表（每段保留号段说明）

| 分段 | 域 | 默认 HTTP | 号段保留说明 |
| ---- | ---- | ---- | ---- |
| 1xxx | 认证 | 401 | 全段归认证中间件 |
| 2xxx | 权限 / 租户 / 配额 | 403 / 429 | 全段归鉴权、租户与限流 |
| 3xxx | 请求校验 | 400 / 422 | 全段归 DTO 校验与协议层 |
| 4xxx | 业务规则 | 404 / 409 / 422 | 按模块分百位：40xx agent、41xx session、42xx ontology、43xx kb、44xx memory、45xx plugin、46xx mcp、47xx review；**48xx 本篇建议预留给 writeback（待回填上游）** |
| 5xxx | 依赖服务 / 内部 | 502 / 503 / 500 | 5999 固定为兜底 INTERNAL_ERROR |

新增错误码必须先在 02 篇 §7 登记分段归属；1xxx~3xxx 由网关中间件与 DTO 校验产生，4xxx/5xxx 由 L3 异常映射产生。

### 4.3 已登记错误码全表（汇编自 02 篇 §7）

| 码 | 名称 | 语义 | 产生层 |
| ---- | ---- | ---- | ---- |
| 1001 | TOKEN_MISSING | 未携带令牌 | 网关 |
| 1002 | TOKEN_INVALID | 令牌验签 / iss / aud 不通过 | 网关 |
| 1003 | TOKEN_EXPIRED | 令牌过期 | 网关 |
| 2001 | SCOPE_INSUFFICIENT | 缺少所需 scope | 网关 |
| 2002 | ROLE_FORBIDDEN | 角色无权（RBAC 矩阵外） | 网关 |
| 2003 | TENANT_MISMATCH | 租户参数与 claim 不一致 | 网关 |
| 2004 | TENANT_DISABLED | 租户已停用 | 网关 |
| 2005 | RATE_LIMITED | 触发限流（附 `Retry-After`） | 网关 |
| 3001 | PARAM_INVALID | 参数校验失败（含未知字段） | 网关 DTO |
| 3002 | BODY_MALFORMED | 请求体不可解析 | 网关 |
| 3003 | VERSION_CONFLICT | 乐观锁版本冲突 | 网关 |
| 3004 | UNSUPPORTED_MEDIA_TYPE | Content-Type 不支持 | 网关 |
| 4101 | SESSION_CLOSED | 会话已关闭（终态） | L3 映射 |
| 4102 | TASK_ALREADY_RUNNING | 同一会话已有活跃 Run | L3 映射 |
| 4201 | VERSION_IMMUTABLE | 本体版本不可变（改已发布版本） | L3 映射 |
| 4301 | SSE_REPLAY_EXPIRED | 断线重连超出回放窗口（HTTP 映射建议 410，待回填上游） | L3 映射 |
| 5001 | LLM_TIMEOUT | 模型调用超时 | L3/L7 映射 |
| 5002 | LLM_UNAVAILABLE | 模型不可用（含 fallback 耗尽） | L3/L7 映射 |
| 5003 | MCP_TARGET_UNAVAILABLE | 外部 MCP / 工具目标不可用（含熔断开路） | L3/L7 映射 |
| 5004 | STORAGE_UNAVAILABLE | 五存储任一不可用 | L3/L6 映射 |
| 5999 | INTERNAL_ERROR | 未分类内部错误（兜底，禁裸 500） | 全局异常处理 |

## 5. 端点清单

以 [02 篇 §4](../architecture/02-网关层设计.md) 端点表为基，结合 Agent §6、ontology §7、memory §5、Skills 篇补全。带 **★** = 本篇补充，待回填上游；标 `*` 的 HTTP 状态暂无已登记平台码（见 §9）。scope 命名 `{资源}:{动作}`，动作集固定：read / write / delete / invoke / publish / approve / submit / admin / chat / install（08 篇 §2.3 及 02 篇端点表已用动作）。

> 2026-09-26 评审修复F ★ 评审补录端点：`GET /tasks`、`GET /tasks/{task_id}/events`（§5.2）；kb 终审候选列表 / 单条决策 / 批量决策 / 分片预览与图谱三查 search·neighborhood·path（§5.4）；`GET /memory/facts/{id}/timeline`（§5.5）；`GET /admin/models`、`PUT /admin/models/{id}`、`GET /admin/costs`（§5.8）。

### 5.0 系统与探针（匿名）

| 方法 | 路径 | 用途 | 所需 scope | 成功码 | 主要错误码 |
| ---- | ---- | ---- | ---- | ---- | ---- |
| GET | /healthz | 存活探针（不查依赖） | 匿名 | 200 | — |
| GET | /readyz | 就绪探针（查 PG/Redis/L7 握手） | 匿名 | 200 | 5004（503） |

### 5.1 agents（routers/agents.py）

| 方法 | 路径 | 用途 | 所需 scope | 成功码 | 主要错误码 |
| ---- | ---- | ---- | ---- | ---- | ---- |
| POST | /agents | 创建 agent | agent:write | 201 | 3001、2001 |
| GET | /agents | 列表（分页 / 按状态筛选） | agent:read | 200 | — |
| GET | /agents/{agent_id} | 详情（模型配置 / 绑定工具 / 适配器健康） | agent:read | 200 | 404* |
| PATCH | /agents/{agent_id} | 更新模型 / 提示词 / 温度等（改 adapter_type 视为重建） | agent:write | 200 | 3001、3003 |
| DELETE | /agents/{agent_id} | 删除（存在 running task 时 409） | agent:write | 204 | 409*（4102 同义场景） |
| PUT | /agents/{agent_id}/tools | 绑定 / 解绑工具 | agent:write | 200 | 3001 |
| POST ★ | /agents/{agent_id}/health-check | 适配器探活（Agent §6.1） | agent:read | 200 | 5003 |

### 5.2 sessions 与 tasks（routers/sessions.py）

| 方法 | 路径 | 用途 | 所需 scope | 成功码 | 主要错误码 |
| ---- | ---- | ---- | ---- | ---- | ---- |
| POST | /sessions | 创建会话（绑定 agent） | session:write | 201 | 3001、404* |
| GET | /sessions | 当前用户会话列表 | session:read | 200 | — |
| GET | /sessions/{id} | 会话详情与状态 | session:read | 200 | 404* |
| DELETE | /sessions/{id} | 管理员软删已归档会话（清理语义；归档=close 状态迁移，2026-09-26 按下方裁决注记补齐行与措辞） | session:admin | 204 | 4101 |
| GET | /sessions/{id}/messages | 历史消息（before_id 游标分页） | session:read | 200 | 404* |
| POST | /sessions/{id}/messages | 发送消息：`Accept: text/event-stream` 返回 SSE 流，否则 202 + `{run_id}` | session:chat | 200（SSE）/ 202 | 3001、4101、4102、5001 |
| GET | /sessions/{id}/events | SSE 订阅 / 断线重连（Last-Event-ID，协议见 02 篇） | session:chat | 200（流） | 4301 |
| POST | /sessions/{id}/cancel | 取消运行中任务 | session:write | 202 | 4102 |
| POST | /sessions/{id}/close | 关闭会话（触发 L1 归档与 L2 沉淀；2026-09-26 按下方裁决注记补录） | session:write | 202 | 4101 |
| PATCH | /sessions/{id} | 会话元信息更新（重命名/置顶/标签/群聊 `routing` 切换；**不含归档**——生命周期迁移唯一入口=close；2026-09-26 预登记，25 篇 F-04/X11、27 篇 F-17） | session:write | 200 | 404*、3001 |
| POST | /sessions/{id}/messages/{mid}/branch | 从指定消息分叉并重生成（**编辑消息=分叉**语义，非原地改写；25 篇 F-03/X3，P0） | session:chat | 202 | 404*、4102 |
| PUT | /sessions/{id}/knowledge | 会话级知识库挂载（覆盖式更新挂载集合，检索收敛至挂载库；25 篇 F-05/X4） | session:write | 200 | 404*、3001 |
| POST | /sessions/{id}/share | 创建只读分享快照（body: expires_in / watermark；25 篇 F-07/X5） | session:share | 201 | 404*、409* |
| DELETE | /sessions/{id}/share | 撤销分享（快照即失效） | session:share | 204 | 404* |
| POST | /sessions/{id}/members | 群聊成员添加（body: members[]{slot_id,display_name,system_prompt,model,routing_role}；27 篇 F-17/X15） | session:write | 201 | 404*、409* |
| PATCH/DELETE | /sessions/{id}/members/{mid} | 成员更新/暂停（PATCH）与移除（DELETE） | session:write | 200 / 204 | 404* |
| GET ★ | /tasks | 任务列表（分页，按 `session_id` / `status` / `type` 过滤） | session:read | 200 | — |
| GET ★ | /tasks/{task_id} | 任务详情（状态 / 用量 / 成本，Agent §6.2） | session:read | 200 | 404* |
| GET ★ | /tasks/{task_id}/events | 任务事件时间线（task_events 按 seq 回放：`Accept: text/event-stream` 订阅 SSE，或 JSON 游标分页） | session:read | 200（流）/ 200 | 404*、4301 |
| POST ★ | /tasks/{task_id}/cancel | 取消运行（Agent §6.2） | session:write | 202 | 4102 |

> 已裁决（2026-09-26，参照 OpenAI 惯例 + 设计意图）：二者语义分立共存——`POST /sessions/{id}/close`=状态迁移（会话所有者，触发归档与 L2 沉淀）；`DELETE /sessions/{id}`=管理员软删已归档会话（清理语义，需 `session:admin`）。本表补录 close 行：`| POST | /sessions/{id}/close | 关闭会话（触发 L1 归档与 L2 沉淀） | session:write | 202 | 4101 |`。

> 预登记（2026-09-26，[25 篇](../架构设计/25-前端页面功能缺口对标与任务清单.md)对标缺口，契约先行）：上表 PATCH / branch / knowledge / share 五行为契约预登记，实现随 F-03~F-07 排期；`POST /sessions` 请求体补两个**向后兼容可选字段**：`ephemeral`（临时会话标记：不进历史检索、不产生 L2 候选沉淀，审计照写，F-06）、`effort`（思考档位初值 off|low|medium|high，F-02，档位变化写审计）。新增 scope `session:share` 在 11 篇 §2 Permission 字典登记，并回填本篇 §5 导语与 08 篇 §2.3 动作集枚举（X6/X7 同批）；`PATCH /sessions/{id}` 仅元信息、与 close/DELETE 三者关系=元信息/生命周期迁移/管理员清理，互不重叠。**群聊扩展（2026-09-26 第二批预登记，[27 篇](./27-Agent群聊与工作流编排设计.md)）**：`POST /sessions` body 再补三个可选字段 `type=single\|group`（默认 single）、`members[]`（成员=Agent 插槽实例：slot_id/display_name/system_prompt/model/routing_role）、`routing=mention\|round_robin\|all\|orchestrator`（发言编排四模式，前三种确定性路由、协调者为唯一 LLM 路由且写审计）；MESSAGE_* SSE 事件补 `agent_id`（挂账 02-SSE 协议篇，X15）。

### 5.3 ontology（routers/ontology.py）

| 方法 | 路径 | 用途 | 所需 scope | 成功码 | 主要错误码 |
| ---- | ---- | ---- | ---- | ---- | ---- |
| POST | /ontologies | 创建本体（draft） | ontology:write | 201 | 3001 |
| GET | /ontologies | 列表（含当前发布版本） | ontology:read | 200 | — |
| GET | /ontologies/{id} | 详情与版本历史（版本不可变） | ontology:read | 200 | 4201、404* |
| PUT ★ | /ontologies/{id} | 元信息更新（ontology §7.1） | ontology:write | 200 | 3003、4201 |
| DELETE ★ | /ontologies/{id} | 弃用（软删，ontology §7.1） | ontology:write | 204 | 409* |
| POST/GET/PUT/DELETE ★ | /ontologies/{id}/classes\|properties\|axioms\|rules | 元素 CRUD（写入必须携带 changeset_id，ontology §7.1） | ontology:write / ontology:read | 201/200/200/204 | 3001、4201 |
| POST | /ontologies/{id}/changesets | 新建变更单 | ontology:write | 201 | 3001 |
| POST | /ontologies/{id}/changesets/{cid}/submit | 提交变更单进终审（2026-09-26 裁决：五动词之一，替代原 submit-review） | review:submit | 202 | 4701、3003 |
| POST | /ontologies/{id}/changesets/{cid}/approve | 终审通过（enterprise 档双负责人会签） | review:approve:ontology | 202 | 4701、2002 |
| POST | /ontologies/{id}/changesets/{cid}/reject | 驳回（必附理由） | review:approve:ontology | 202 | 4701、3001 |
| POST | /ontologies/{id}/changesets/{cid}/publish | 发布（聚合 publish(gate_report, approvals) 断言后推进 head_version） | ontology:publish | 202 | 409*、4201 |
| POST ★ | /ontologies/{id}/changesets/{cid}/rollback | 回滚已发布变更单（生成逆向 changeset 重新发布） | ontology:publish | 202 | 4201、3003 |
| POST | /ontologies/{id}/validate | SHACL / 一致性试校验 | ontology:read | 200 | 3001、5004 |
| GET ★ | /ontologies/{id}/diff?base=&target= | 版本 / 变更单 diff（ontology §7.1） | ontology:read | 200 | 3001 |
| POST ★ | /ontologies/{id}/reason | 推理（consistency / classification / entailment / semantic，ontology §7.2） | ontology:read | 202 | 3001、5004 |
| POST ★ | /ontologies/{id}/query | 只读 SPARQL 查询（禁 UPDATE/DELETE 语法，ontology §7.2） | ontology:read | 200 | 3001、4201 |
| POST ★ | /ontologies/search | 语义检索类 / 属性 / 规则（向量，ontology §7.2） | ontology:read | 200 | 3001 |

> 已裁决（2026-09-26，标准动词子资源模式 + 设计意图）：**采 ontology §7.1 五动词**——`POST /ontologies/{id}/changesets/{cid}/submit|approve|reject|publish|rollback`，与聚合方法一一对应（changeset 是唯一流程头）；02 篇原 `submit-review`/`publish` 两行已改五动词。`POST /admin/reviews/{id}/decision` 保留给**通用工单对象**（文档/插件/记忆升级）的审批，changeset 不走它；`/api/v1/ontology/*` 裸前缀归一为 `/ontologies/{id}/*` 维持不变。

### 5.4 kb（routers/kb.py）

| 方法 | 路径 | 用途 | 所需 scope | 成功码 | 主要错误码 |
| ---- | ---- | ---- | ---- | ---- | ---- |
| POST | /kb/documents | 登记文档（返回 MinIO 预签名上传地址） | kb:write | 201 | 3001、5004 |
| GET | /kb/documents | 文档列表（含流水线状态） | kb:read | 200 | — |
| GET | /kb/documents/{id} | 详情 | kb:read | 200 | 404* |
| POST | /kb/documents/{id}/pipeline/start | 启动七步抽取流水线 | kb:write | 202 | 409* |
| GET | /kb/documents/{id}/pipeline | 流水线进度与 checkpoint | kb:read | 200 | — |
| POST | /kb/documents/{id}/pipeline/retry | 失败后从断点重跑 | kb:write | 202 | 409* |
| POST | /kb/search | GraphRAG 检索。参数（对齐权威 docs/OntRAG §5）：`query`、`mode=local\|global\|drift\|auto`（默认 auto）、`top_k`、`kb_id?`、`with_evidence?`、`entity_type_filter?`（本体类 IRI）、`ontology_version?`、`max_hops?`；返回 answers/hits/graph_paths/citations/confidence（2026-09-26 评审修复F：契约统一，权威=docs/OntRAG §5） | kb:read | 200 | 3001、5004 |
| GET ★ | /kb/documents/{id}/review/candidates | 候选实例列表（分页，按 type / status / confidence 过滤，P4 终审队列） | review:read | 200 | 404* |
| POST ★ | /kb/review/candidates/{cid}/decision | 单条终审决策：body `action=accept\|reject\|edit_accept`（edit_accept 必附编辑载荷，修订后入审） | review:approve | 202 | 3001、4701 |
| POST ★ | /kb/documents/{id}/review/batch-decision | 批量终审决策（body 同上，批量上限 200 条/批，超出 3001） | review:approve | 202 | 3001、4701 |
| GET ★ | /kb/documents/{id}/chunks | 分片预览（分片列表 + 原文/元数据，含命中高亮 `span` 偏移，FR-KB-03） | kb:read | 200 | 404* |
| GET ★ | /kb/graph/search | 图谱实体搜索（`q=` 关键词/IRI 片段，`top_k`） | kb:read | 200 | 3001 |
| GET ★ | /kb/graph/neighborhood | 实体邻域展开（`entity_id` / `depth` / `limit`，可按关系类型过滤） | kb:read | 200 | 404*、3001 |
| GET ★ | /kb/graph/path | 两实体间路径查询（`source` / `target` / `max_hops`） | kb:read | 200 | 3001、404* |

### 5.5 memory（routers/memory.py）

| 方法 | 路径 | 用途 | 所需 scope | 成功码 | 主要错误码 |
| ---- | ---- | ---- | ---- | ---- | ---- |
| GET | /memory | 读记忆（按 layer / key 过滤） | memory:read | 200 | — |
| POST | /memory/search | 语义检索（向量） | memory:read | 200 | 3001 |
| POST | /memory | 写入记忆（按层授权） | memory:write | 201 | 3001、2001 |
| POST | /memory/facts/{id}/invalidate | 失效标记（墓碑式软删，2026-09-26 裁决：替代原 DELETE——不物理删除为平台底线） | memory:write | 202 | 404* |
| POST | /memory/consolidate | 触发 L1 到 L2 沉淀 | memory:write | 202 | — |
| GET | /memory/context | 组装会话上下文记忆（mode=full\|light） | memory:read | 200 | 404* |
| GET ★ | /memory/l1/{session_id} | 读 L1 工作记忆（blocks / window / state，memory §5.1） | memory:read | 200 | 404* |
| PUT ★ | /memory/l1/{session_id} | 写 L1（自编辑记忆块 / 滑动窗口 / 任务草稿） | memory:write | 200 | 3001 |
| POST ★ | /memory/facts | 写候选事实（L2） | memory:write | 201 | 3001 |
| GET ★ | /memory/facts | 查询用户事实（分页、status / category 过滤） | memory:read | 200 | — |
| GET ★ | /memory/facts/{id}/timeline | 事实变更时间线（产生 / 升级 / 失效全程留痕，FR-MEM-06） | memory:read | 200 | 404* |
| POST ★ | /memory/facts/{id}/invalidate | 失效标记（置 status=invalidated） | memory:write | 202 | 404* |
| POST ★ | /memory/promotions | 发起 L2→L3 升级申请单 | memory:write | 202 | 409* |
| GET ★ | /memory/audit | 记忆审计查询（管理员，按 user/session 回放） | memory:read | 200 | 2002 |

> 已裁决（2026-09-26，设计意图优先）：**删除 DELETE 端点**——「全程不物理删除、可审计回放」是平台底线（memory 篇为该域权威）；遗忘 = `POST /memory/facts/{id}/invalidate` 墓碑式软删（上表已收录）。02 篇端点表已同步替换。

### 5.6 plugins 与 tools（routers/plugins.py）

| 方法 | 路径 | 用途 | 所需 scope | 成功码 | 主要错误码 |
| ---- | ---- | ---- | ---- | ---- | ---- |
| GET | /plugins | 市场列表 | plugin:read | 200 | — |
| GET | /plugins/{id} | 详情（含版本树） | plugin:read | 200 | 404* |
| POST | /plugins | 上传插件包（验签后登记） | plugin:write | 201 | 3001 |
| POST | /plugins/{id}/submit | 提交上架审核（五关自动门禁前置） | review:submit | 202 | 4701 |
| POST | /plugins/{id}/install | 安装已发布版本 | plugin:install | 202 | 409*、45xx |
| POST | /plugins/{id}/enable | 启用 | plugin:admin | 200 | 45xx |
| POST | /plugins/{id}/disable | 停用 | plugin:admin | 200 | 45xx |
| GET ★ | /tools | 工具注册中心目录（名称 + 摘要，按租户可见性过滤，Skills §2） | tool:invoke | 200 | — |
| POST ★ | /tools/search | Tool Search：按任务语义拉取完整 schema（Skills §2） | tool:invoke | 200 | 3001 |

### 5.7 mcp（routers/mcp.py）

| 方法 | 路径 | 用途 | 所需 scope | 成功码 | 主要错误码 |
| ---- | ---- | ---- | ---- | ---- | ---- |
| GET | /mcp/servers | 外部 MCP Server 列表 | mcp:read | 200 | — |
| POST | /mcp/servers | 注册外部 Server（stdio / Streamable HTTP） | mcp:write | 201 | 3001、5003 |
| POST | /mcp/servers/{id}/refresh | 拉取 tools/list 更新工具清单 | mcp:write | 200 | 5003 |
| GET | /mcp/servers/{id}/tools | 该 Server 的工具清单 | mcp:read | 200 | 404* |
| POST | /mcp/tools/{tool_id}/enable | 审核开启外部工具（默认不可信） | mcp:write | 200 | 404* |
| POST | /mcp/invocations | 经 L7 MCP 网关发起一次工具调用 | mcp:invoke | 202 | 3001、2001、5003 |
| GET | /mcp/capabilities | 平台能力出口清单（CapabilityProvider 注册表视图） | mcp:read | 200 | — |

### 5.8 admin 与 writeback 台账（routers/admin.py）

| 方法 | 路径 | 用途 | 所需 scope | 成功码 | 主要错误码 |
| ---- | ---- | ---- | ---- | ---- | ---- |
| POST | /admin/tenants | 创建租户 | admin:write | 201 | 3001、2001 |
| GET | /admin/audit-logs | 审计日志查询 | admin:read | 200 | — |
| GET | /admin/reviews | 审核工单列表 | review:read | 200 | — |
| POST | /admin/reviews/{id}/decision | 审批裁决（通过 / 驳回附理由） | review:approve | 200 | 4701、3003 |
| GET | /admin/quota/{tenant_id} | 配额查询 | admin:read | 200 | 404* |
| PUT | /admin/quota/{tenant_id} | 配额调整 | admin:write | 200 | 3001 |
| GET | /admin/stats | 平台统计（调用量 / 成本 / 延迟） | admin:read | 200 | — |
| GET ★ | /admin/models | 模型渠道列表（提供商 / 模型 / 优先级 / 预算 / 状态） | admin:read | 200 | — |
| PUT ★ | /admin/models/{id} | 渠道配置更新（优先级 / 预算 / fallback；密钥类只写哈希与长度） | admin:write | 200 | 3001、3003 |
| GET ★ | /admin/costs | 成本看板聚合（按租户 / 模型 / 日聚合，趋势与占比，P12） | admin:read | 200 | 3001 |
| GET ★ | /admin/writeback/ledger | 回写台账查询（status / needs_human 过滤，业务回写设计 §8） | admin:read | 200 | — |
| GET ★ | /admin/writeback/ledger/{id} | 台账单条详情（含 receipt 凭证与 attempts） | admin:read | 200 | 404* |
| POST ★ | /admin/writeback/ledger/{id}/dispose | 人工处置：重发（同幂等键新 attempt）/ 标记冲正 / 关闭（业务回写设计 §3.3） | admin:write | 202 | 409*、48xx |
| POST ★ | /admin/users | 创建用户（分配角色，password 哈希入库；08 篇 §2.2 矩阵「租户/用户/密钥管理」归 admin） | admin:write | 201 | 3001、2001 |
| GET ★ | /admin/users | 用户列表（分页，按 status / role 过滤） | admin:read | 200 | — |
| GET ★ | /admin/users/{user_id} | 用户详情（含角色绑定与 last_login_at） | admin:read | 200 | 404* |
| PATCH ★ | /admin/users/{user_id} | 更新用户元信息与角色绑定（display_name / username / roles） | admin:write | 200 | 3001、3003 |
| DELETE ★ | /admin/users/{user_id} | 禁用（软删 status=disabled；**不物理删除**，全程可追溯，审计留痕） | admin:write | 204 | 409* |
| POST ★ | /admin/api-keys | 签发 API Key（scopes ⊆ owner 用户 scopes；**明文仅本次响应返回一次**，库只存哈希与前缀——08 篇 §2.0/§2.6） | admin:write | 201 | 3001、2001 |
| GET ★ | /admin/api-keys | Key 列表（按 owner / status 过滤；只回前缀与元数据，不回明文与哈希） | admin:read | 200 | — |
| POST ★ | /admin/api-keys/{id}/rotate | 轮换：新钥即时生效，旧钥 24h 宽限（active→rotated，**状态机权威 08 篇 §2.6**） | admin:write | 200 | 404*、409* |
| POST ★ | /admin/api-keys/{id}/revoke | 吊销：立即失效不可逆（→revoked 终态，08 篇 §2.6；签发/轮换/吊销全走审计） | admin:write | 202 | 404*、409* |

> ★ 三条 writeback 端点为本篇补充（上游业务回写设计 §8 只定义了表结构未定义 REST 端点）；48xx 号段为本篇建议预留（§4.2）。

### 5.9 auth（routers/auth.py，2026-09-26 设计定稿）

路由归属裁决：**独立 auth router**（`routers/auth.py`，不并入 admin）。JWT claims 权威定义引 [08 篇 §2.1](../architecture/08-横切关注点与工程规范.md)；错误码**不新增**——复用现有 1xxx 段（02 篇错误码表不动），凭据类失败统一映射 1002（凭据无效）。登录/刷新失败与登出全走审计（08 篇 §3 认证类事件）。

| 方法 | 路径 | 用途 | 所需 scope | 成功码 | 主要错误码 |
| ---- | ---- | ---- | ---- | ---- | ---- |
| POST | /auth/login | 登录：校验凭据，签发 access（2h）+ refresh（14d），claims 按 08 篇 §2.1（sub/tenant_id/roles/scopes/typ/jti）；**匿名白名单**（02 篇 §3 ③） | 匿名 | 200 | 1002（凭据无效）、2005 |
| POST | /auth/refresh | 以有效 refresh token 换发新 access；旧令牌 `jti` 入 Redis 黑名单（TTL=剩余有效期，08 篇 §2.2；轮换细则随 08 篇 §10 对应待办定稿） | 匿名（body 携 refresh token） | 200 | 1002、1003 |
| POST | /auth/logout | 登出：将请求所持 access/refresh 的 `jti` 写入吊销黑名单 | 认证后（无额外 scope） | 204 | 1001、1002 |

> 三端点计入端点清单总数；body 与响应 DTO（token 对、错误分支）随 OpenAPI 契约测试快照冻结（§8 变更管理）。

### 5.10 files / prompts / groups / acl / permission-requests（routers/files.py 等，2026-09-26 预登记）

> 来源：[25 篇](../架构设计/25-前端页面功能缺口对标与任务清单.md) 对标缺口（25 篇 §7 契约先行落点）。本节为**契约预登记**：路径/scope/语义已定，实现随对应任务排期（P0 标注者随 M3/M5 对话批先行）。错误码复用现有号段；新增专用错误码（如分享已过期）在实现 PR 按 §4.2 号段登记。新增 scope `file:read/write`、`group:read/write`、`prompt:read/write`、`{resource}:admin`（acl）一并在 11 篇 §2 Permission 字典登记（X6/X7）。

| 方法 | 路径 | 用途 | 所需 scope | 成功码 | 主要错误码 |
| ---- | ---- | ---- | ---- | ---- | ---- |
| POST | /files | 临时附件上传（multipart；返回 file_id + MinIO 预签名直传地址；生命周期默认 7 天+租户配额，25 篇 F-01/X1，**P0**） | file:write | 201 | 3001、409* |
| GET | /files/{id} | 附件元数据与下载预签名（会话内引用） | file:read | 200 | 404* |
| POST | /files/{id}/to-kb | 临时附件转存知识库（创建 kb 文档登记并进七步流水线与审核队列，**不绕审**；25 篇 F-01，宪法 3） | kb:write | 202 | 404*、409* |
| GET/POST | /prompts | 提示词库列表/新建（`scope=personal\|tenant` 两级；25 篇 F-08/X12） | prompt:read / prompt:write | 200 / 201 | 3001 |
| PUT/DELETE | /prompts/{id} | 提示词更新/删除（tenant 级需管理权限） | prompt:write | 200 / 204 | 404*、2002 |
| GET/POST | /admin/groups | 用户组列表/建组（RBAC 之上的批量授权单元；25 篇 F-10/X6） | group:read / group:write | 200 / 201 | 3001 |
| PUT/PATCH/DELETE | /admin/groups/{id} | 组更新 / 成员增删（PATCH）/ 解散 | group:write | 200 / 200 / 204 | 404*、2002 |
| PUT | /{resource}/{id}/acl | 资源级 ACL 覆盖式授权（`resource=kb\|agent\|model_channel`；body: grants[]{subject_type=group\|user, subject_id, level=read\|use\|write}；25 篇 F-10/X6） | {resource}:admin | 200 | 404*、2002 |
| GET | /acl | 查询资源授权与继承链（`?resource=&id=`） | 对应资源 read | 200 | — |
| POST | /permission-requests | 提交权限申请（body: resource/action/reason；生成审批工单=**第六类对象**，权威=11 篇 §6；25 篇 F-11/X7） | 认证后（无额外 scope） | 202 | 3001、409* |
| GET | /permission-requests | 申请列表（`?role=mine\|approvable`） | review:read | 200 | — |

### 5.11 workflows（routers/workflows.py，2026-09-26 预登记）

> 来源：[27 篇](./27-Agent群聊与工作流编排设计.md) P15（工作流编排）。契约预登记，实现随 X16 工作流引擎排期；scope `workflow:read/run/edit/publish` 挂账 11 篇 §2/§3。工作流版本不可变；发布在 team/enterprise 档走 `workflow_publish` 审批（第七类对象候选，11 篇裁决）。

| 方法 | 路径 | 用途 | 所需 scope | 成功码 | 主要错误码 |
| ---- | ---- | ---- | ---- | ---- | ---- |
| GET | /workflow-templates | 工作流模板列表（画框"从模板新建"承载，27 篇 §3） | workflow:read | 200 | — |
| GET/POST | /workflows | 工作流列表/新建草稿（节点图 JSON：八类节点，27 篇 §3） | workflow:read / workflow:edit | 200 / 201 | 3001 |
| GET/PUT/DELETE | /workflows/{id} | 详情/更新草稿/删除（仅草稿可改；已发布版本不可变） | workflow:read / workflow:edit | 200 / 200 / 204 | 404*、409* |
| POST | /workflows/{id}/versions | 提交发布（校验 DAG 无环+节点参数完备 → 生成不可变版本；治理档位分流审批） | workflow:publish | 202 | 3001、409* |
| GET | /workflows/{id}/versions | 版本历史 | workflow:read | 200 | 404* |
| POST | /workflows/{id}/rollback | 回滚（以目标版本新建草稿，复用画框 25 交互模式） | workflow:edit | 202 | 404* |
| POST | /workflows/{id}/test | 试运行（dry-run，202→task；type=workflow_test；支持节点断点） | workflow:run | 202 | 409*、4102 |
| POST | /workflows/{id}/runs | 正式运行（202→task；type=workflow_run） | workflow:run | 202 | 409*、4102 |
| GET | /workflows/{id}/runs | 运行历史（对齐任务中心过滤） | workflow:read | 200 | 404* |
| POST | /workflows/{id}/runs/{run_id}/resume | 断点恢复（命中暂停后修参/从暂停节点继续；202→task；27 篇 §3 time-travel 语义） | workflow:run | 202 | 404*、409* |

### 5.12 sandboxes（routers/sandboxes.py，2026-09-27 预登记——[docs/Sandbox](../Sandbox/沙箱与执行环境设计.md) §12 契约来源，实现随 M5 / S0 提前批次）

管理面端点（供给与执行走 daemon 内部通道不经 REST，Sandbox §11）。信任级映射与容量配置为平台管理员独占（租户只读+可收紧项，Sandbox §3.2）；全部操作落 `sandbox_events` 审计。

| 方法 | 路径 | 用途 | 所需 scope | 成功码 | 主要错误码 |
| ---- | ---- | ---- | ---- | ---- | ---- |
| GET | /sandboxes | 沙箱实例列表（按 status/owner_kind 过滤，含资源用量） | sandbox:read | 200 | — |
| GET | /sandboxes/{id} | 详情（状态机/用量/归属/快照引用） | sandbox:read | 200 | 404* |
| POST | /sandboxes/{id}/stop | 停止并回收（终态前自动快照可配） | sandbox:write | 202 | 404*、409* |
| GET | /sandboxes/{id}/audit | 执行与出口审计事件流（sandbox_events 视图） | sandbox:read | 200 | 404* |
| GET/PUT | /sandbox-config | 池/配额/信任级映射/容量（平台管理员；fail-closed 拒绝不可静默降级，Sandbox §3.2） | sandbox:admin | 200 / 200 | 3001、3003 |


## 6. 关键端点详设

### 6.1 发消息（同步受理 + 流式）

```json
POST /api/v1/sessions/s_01J9Z8/messages
Authorization: Bearer <token>
Content-Type: application/json
Idempotency-Key: 7d1f09ab-...        // 可选，防双击重复发送

{ "content": "分析昨夜城东 10kV 线路停电的可能原因", "agent_id": null }
```

- 带 `Accept: text/event-stream`：返回 200 + SSE 流（RUN/TEXT/TOOL 事件，帧格式与事件总表见 [02-SSE事件流协议](./02-SSE事件流协议.md)）；
- 不带：返回 202，客户端再经 `GET /sessions/{id}/events` 订阅。

```json
HTTP/1.1 202 Accepted
{ "data": { "run_id": "r_01JB3K", "task_id": "t_01JB3K", "status": "queued" }, "meta": {} }
```

### 6.2 knowledge / kb 检索

```json
POST /api/v1/kb/search
{ "query": "配电网单相接地故障的处置流程", "mode": "drift", "top_k": 8,
  "kb_id": "kb_01J4", "filters": { "doc_status": "indexed" } }

HTTP/1.1 200 OK
{ "data": {
    "context": "汇总检索上下文（注入对话编排）",
    "chunks":   [{ "doc_id": "d_01", "chunk_id": "c_017", "quote": "...", "score": 0.83 }],
    "graph_paths": [{ "nodes": ["OutageEvent", "Feeder"], "edges": ["locatedOn"] }],
    "degraded": false },
  "meta": { "elapsed_ms": 612, "trace_id": "req_01J9X7" } }
```

`degraded=true` 表示图库或向量库单侧降级（08 篇 §5 降级矩阵），前端须显式标注「证据链暂缺」。

### 6.3 本体校验（SHACL）

```json
POST /api/v1/ontologies/0f2c.../validate
{ "graph": null, "data_graph_uri": "neo4j://kb/orders-2026-09", "shape_version": "current" }

HTTP/1.1 200 OK
{ "data": { "conforms": false,
    "stats": { "triples": 48210, "elapsed_ms": 387 },
    "results": [{ "focus": "...#DEVICE_001", "path": "...#hasStatus", "value": "flying",
      "constraint": "sh:in", "severity": "Violation",
      "message": "状态不在枚举 [created, paid, shipped, closed]",
      "source_shape": "...#R012-shape" }] }, "meta": {} }
```

### 6.4 changeset 状态迁移（创建 → 终审 → 发布）

```json
① POST /api/v1/ontologies/0f2c.../changesets
   { "title": "增加停电检修工单行动类", "base_version": "v2", "changes": [ /* 元素级增删改 */ ] }
   → 201 { "data": { "changeset_id": "cs_01K", "status": "draft" }, "meta": {} }

② POST /api/v1/ontologies/0f2c.../changesets/cs_01K/submit    → 202 { "data": { "status": "in_review" }, "meta": {} }
   （重复提交 → 4701 OBJECT_ALREADY_IN_REVIEW）

③ POST /api/v1/ontologies/0f2c.../changesets/cs_01K/approve   （scope：review:approve:ontology，五动词）
   { "reason": "双负责人已会签" }
   → 200 { "data": { "status": "approved" }, "meta": {} }

④ POST /api/v1/ontologies/0f2c.../changesets/cs_01K/publish   （scope：ontology:publish，五动词）
   → 202 { "data": { "version": "v3", "status": "published" }, "meta": {} }
```

审批链强度由租户 `governance_tier`（solo / team / enterprise，08 篇 §2.4）决定，状态机与端点不变。

### 6.5 回写台账查询（★ 本篇补充，待回填上游）

```json
GET /api/v1/admin/writeback/ledger?status=unknown&needs_human=true&page=1&page_size=20

HTTP/1.1 200 OK
{ "data": [{
    "id": "wl_01Q", "action_instance_id": "a_01P", "connector_id": "cn_power",
    "idempotency_key": "t_88:d1f0...", "status": "unknown", "attempts": 2,
    "receipt": null, "needs_human": true,
    "last_error": "execute timeout after 30s", "updated_at": "2026-09-26T01:12:44Z" }],
  "meta": { "page": 1, "page_size": 20, "total": 3 } }
```

状态机 `pending → accepted → succeeded|failed|compensated`（含 unknown 分支）以 [业务回写设计 §2.5](../MCP/业务回写设计.md) 为权威。

### 6.6 记忆读写

```json
GET /api/v1/memory/context?session_id=s_01J9Z8&mode=light
→ 200 { "data": { "l1": { "window": [ /* 最近消息 */ ], "blocks": [] },
                 "l2": [{ "fact_id": "f_01", "content": "...", "source": "l2" }],
                 "l3": [], "l4": [] }, "meta": { "mode": "light" } }

POST /api/v1/memory
{ "level": "l2", "content": "用户偏好：工单摘要先给结论", "category": "preference",
  "source_refs": { "session_id": "s_01J9Z8", "message_ids": ["m_0231"] } }
→ 201 { "data": { "fact_id": "f_02", "status": "candidate" }, "meta": {} }
```

写入按 (source_session_id, source_message_ids) 幂等去重，重复提交返回原 fact_id（memory §5.4）。

### 6.7 插件安装

```json
POST /api/v1/plugins/p_01W2/install
{ "version": "1.2.0", "scope_grants": ["weather:read"] }   // 逐项授权 x-platform.required_scopes

HTTP/1.1 202 Accepted
{ "data": { "install_id": "in_01X", "status": "installing" }, "meta": {} }
```

安装成功后插件 tools 进注册中心（`GET /tools` 可见）；scope 未授权的工具调用返回 2001。

## 7. SSE 端点说明

`POST /sessions/{id}/messages`（带 `Accept: text/event-stream`）与 `GET /sessions/{id}/events` 为 SSE 端点：帧格式、14+2 事件总表（M3 主干波 / M4+ 扩展波）、心跳、断线重连与多副本语义，**以 [02-SSE事件流协议](./02-SSE事件流协议.md) 为准**（权威上游为 architecture/02 篇 §5），本篇不重复定义。

## 8. 变更管理

1. **先改本篇**：任何端点 / 字段 / 错误码 / scope 变更，先在本篇（或对应协议篇）提 PR 评审——本篇是对外契约的评审入口；
2. **同步实现**：评审通过后同步 FastAPI 实现与 pydantic DTO，OpenAPI schema 快照进入契约测试（08 篇 §8.2：schema 快照变更必须在 PR 中显式确认，防接口悄悄漂移）；
3. **回填上游**：标注「本篇补充，待回填上游」的端点在定稿时回填 02 篇 / 模块设计文档，消除双源；
4. **版本化**：向后兼容的新增（加端点、加可选字段、加错误码）在 v1 内迭代，本篇状态号 +0.1；破坏性变更升 `/api/v2` 并公告弃用周期（建议 ≥ 2 个里程碑）。

## 9. 待办与开放问题

- [x] ~~冲突裁决 ① sessions 关闭端点~~（2026-09-26：close=状态迁移（session:write）与 DELETE=管理员软删（session:admin）语义分立共存，参照 OpenAI Threads 惯例；02 篇与本篇均已更新）；
- [x] ~~冲突裁决 ② memory 物理删除~~（2026-09-26：设计意图优先——DELETE 移除，invalidate 墓碑为唯一"遗忘"语义；02 篇已替换）；
- [x] ~~冲突裁决 ③ changeset 状态迁移路径~~（2026-09-26：采五动词 submit/approve/reject/publish/rollback 与聚合方法一一对应；admin decision 保留给通用工单；02 篇已改）；
- [x] ~~冲突裁决 ④ SSE 载荷口径~~（2026-09-26：02 篇为权威并超集化——RUN_STARTED 补 agent_id、RUN_ERROR 补 retryable，向后兼容）；
- [x] ~~冲突裁决 ⑤ MCP memory 层级命名~~（2026-09-26：MCP 对外语义枚举 session/user/org/knowledge ↔ 内部 L1~L4，映射表已入 MCP 篇 §2）；
- [ ] ★ 补充端点回填：02 篇 §4 已声明「全量端点登记册权威=本篇」（代表性端点不再回填），剩余动作是各**模块设计文档**（Agent/ontology/memory/Skills）端点小节加指向本篇的链接，随开发进行；
- [x] ~~端点补录：GET /tasks、GET /tasks/{task_id}/events、kb 审核（候选列表 / 单条决策 / 批量决策）、分片预览、图谱三查、GET /admin/models、PUT /admin/models/{id}、GET /admin/costs、GET /memory/facts/{id}/timeline 已补入 §5.2 / §5.4 / §5.5 / §5.8~~（2026-09-26 评审修复F ★ 评审补录）；
- [x] ~~`POST /auth/login` 路由归属定稿~~（2026-09-26 设计定稿：独立 auth router（routers/auth.py），login/refresh/logout 三端点入 §5.9；admin 补 users CRUD 五端点与 api_keys 四端点（签发/轮换/吊销/列表，状态机引 08 篇 §2.6）入 §5.8。错误码复用现有 1xxx、02 篇错误码表不动；02 篇 §9 同名待办随其维护流程同步勾销）；
- [ ] 48xx 号段（writeback）与 404 / 409 通用码在 02 篇 §7 正式登记；
- [ ] 4301 的 HTTP 映射（建议 410 Gone）在 02 篇登记；
- [ ] ★ 25 篇对标缺口端点实现排期（2026-09-26 预登记）：§5.2 新增五行（PATCH/branch/knowledge/share）与 §5.10（files/prompts/groups/acl/permission-requests）；实现时同步：新增 scope 与专用错误码登记（11 篇 §2/§3 + §4.2 号段）、**动作集枚举回填**（`share` 动作入 08 篇 §2.3 固定动作集与本篇 §5 导语）、SSE 分支/用量水位事件登记 02-SSE 协议篇（X3/X8）、OpenAPI 快照再生成（16 篇 §2.3 漂移校验）。协调项 X1~X14 见 [docs/代办任务/2026-09-26-前端页面功能缺口补齐.md](../代办任务/2026-09-26-前端页面功能缺口补齐.md)；
- [ ] ★ 27 篇群聊与工作流端点实现排期（2026-09-26 预登记）：§5.2 群聊扩展（type/members/routing 字段+members 两端点）与 §5.11 workflows 八端点；实现时同步：`workflow:*` scope 登记（11 篇 §2/§3）、`workflow_publish` 审批对象裁决（X16）、MESSAGE_* 事件 agent_id 与路由决策事件（02-SSE 协议篇，X15）、工作流 DAG 校验错误码定号（§4.2）。
- [ ] 本篇端点表的 OpenAPI 标注核对（八个 routers 全端点标注 scope，02 篇 §8 验收项）进入 CI 契约测试。

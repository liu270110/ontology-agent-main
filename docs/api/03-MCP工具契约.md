# 03 - MCP 工具契约（平台 MCP Server 出口）

> 状态：v0.1 | 日期：2026-09-26 | 上游依据：[MCP/MCP网关设计](../MCP/MCP网关设计.md)（**工具清单 / 语义标注 / 鉴权的权威上游**）、[MCP/业务回写设计](../MCP/业务回写设计.md)（回写契约 / 幂等 / 凭证）、[architecture/01-总体架构与分层 §3.3](../architecture/01-总体架构与分层.md)（协议栈：Streamable HTTP + stdio、双版本协商）、[architecture/08-横切关注点 §2](../architecture/08-横切关注点与工程规范.md)（scope 模型）、[memory/多层记忆设计 §5.2](../memory/多层记忆设计.md)
>
> 本篇是对外发布的 MCP 出口契约（「本体即服务」的对外总线）。工具集、`_meta.x-ontology` 语义标注、scope 以 MCP 篇 §2 为准逐工具展开；标注「**本篇补充，待回填上游**」的工具 / 字段为本篇补全项。

## 1. 协议版本与传输

| 项 | 契约 |
| ---- | ---- |
| 远程传输 | **Streamable HTTP**，端点 `/mcp`（取代旧 HTTP+SSE） |
| 本地传输 | **stdio**（本地插件 / 工具） |
| 协议版本 | 双版本协商：`initialize` 声明支持 `["2025-11-25", "2025-06-18"]`，取客户端交集最高版（MCP 篇 §7） |
| 会话 | Streamable HTTP 以 `Mcp-Session-Id` 请求头维持会话；会话状态存 Redis（MCP 篇 §1 代码结构 `server/session.py`） |
| Tasks | **opt-in**：默认关闭；仅对显式协商该能力且属长任务的行动类开放（MCP 篇 §7） |
| SDK | FastMCP（Python，与后端同栈；版本锁定见 MCP 篇待办） |
| 编码 | JSON-RPC 2.0 over HTTP POST（`application/json`）；工具结果走 `tools/call` 标准响应 |

## 2. initialize 握手

```json
--> { "jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": { "protocolVersion": "2025-06-18",
                  "capabilities": { "roots": {}, "sampling": {} },
                  "clientInfo": { "name": "claude-desktop", "version": "1.0" } } }

<-- { "jsonrpc": "2.0", "id": 1, "result": {
        "protocolVersion": "2025-06-18",
        "capabilities": { "tools": { "listChanged": true },
                          "resources": {}, "prompts": {}, "tasks": null },
        "serverInfo": { "name": "ontology-agent-mcp", "version": "0.1.0" } } }
```

握手后常规流程：`notifications/initialized` → `tools/list`（按租户可见性过滤，CapabilityRegistry `list_tools(tenant_id)`）→ 按需 `tools/call`。工具变更经 `tools.listChanged` 通知（能力注册 / 本体发布联动上下架）。

## 3. tools 清单契约

总表（MCP 篇 §2 为基；★ = 本篇补充）：

| 工具 | 所需 scope | 幂等性 | 风险面 |
| ---- | ---- | ---- | ---- |
| knowledge.search | `kb:read` | 只读，天然幂等 | 无 |
| ontology.validate | `ontology:read` | 只读，天然幂等 | 无 |
| ontology.reason | `ontology:read` | 只读，天然幂等 | 无 |
| ontology.query | `ontology:read` | 只读，天然幂等 | SPARQL 写语法拒绝（3001） |
| memory.read | `memory:read:{level}` | 只读，天然幂等 | 越层读取拒绝 |
| memory.write | `memory:write:{level}` | 按 (source_session_id, source_message_ids) 去重 | 产物为**候选事实**，进审核（候选非成品） |
| memory.invalidate ★ | `memory:write`（对应层） | 重复失效返回原状态 | 不可逆标记（不物理删除） |
| action.invoke | `action:invoke` + 业务域 scope | 平台侧幂等键 `{tenant_id}:{action_instance_id}` | 高风险须 `confirm_token`（§7） |
| writeback.status ★ | `action:invoke`（只读查询建议 `kb:read` 级独立 scope，待上游定） | 只读，天然幂等 | 无 |

### 3.1 knowledge.search

```json
inputSchema: {
  "type": "object",
  "properties": {
    "query":       { "type": "string", "description": "自然语言检索式" },
    "mode":        { "type": "string", "enum": ["auto", "local", "global", "drift"], "default": "auto",
                     "description": "auto=按问题特征自动路由（OntRAG §4.1）" },
    "top_k":       { "type": "integer", "minimum": 1, "maximum": 50, "default": 8 },
    "kb_id":       { "type": "string", "description": "目标知识库，缺省为租户默认" },
    "with_evidence":        { "type": "boolean", "default": true, "description": "可选：是否返回图谱路径证据链" },
    "entity_type_filter":   { "type": "array", "items": { "type": "string" },
                              "description": "可选：本体类 IRI 过滤" },
    "ontology_version":     { "type": "string", "description": "可选：缺省=当前发布版" },
    "max_hops":    { "type": "integer", "default": 2, "description": "可选：local/drift 图扩展跳数" } },
  "required": ["query"] }

输出 schema: { "answers": [{ "summary", "confidence",
                             "evidence": { "graph_paths", "citations", "community_reports" } }],
               "hits": ["RetrievalHit"], "graph_paths": ["GraphPath"],
               "citations": ["ChunkCitation"], "confidence": "number", "degraded": "boolean" }
_meta.x-ontology: { "kb_id": "kb_01J4", "domain_classes": ["...#OutageEvent"] }
错误映射: 3001（参数）/ 43xx（知识库业务）/ 5004（存储降级时 degraded=true 而非报错）
```

> 2026-09-26 评审修复F：契约统一，权威=docs/OntRAG §5——mode 补 `auto`（默认）与可选过滤器参数 `with_evidence` / `entity_type_filter` / `ontology_version` / `max_hops`，输出对齐 answers / hits / graph_paths / citations / confidence；原 `filters` 泛型对象并入 `entity_type_filter`。

### 3.2 ontology.validate

```json
inputSchema: {
  "type": "object",
  "properties": {
    "ontology_id":   { "type": "string" },
    "graph":         { "type": "string", "description": "待校验 RDF 图（Turtle/JSON-LD），缺省校验 data_graph_uri" },
    "data_graph_uri":{ "type": "string" },
    "shape_version": { "type": "string", "default": "current" } } }

输出 schema: { "conforms": "boolean",
               "stats": { "triples": "int", "elapsed_ms": "int" },
               "results": [{ "focus", "path", "value", "constraint", "severity", "message", "source_shape" }] }
_meta.x-ontology: { "shape_version": "v12", "candidate_source": "extraction|manual|agent" }
错误映射: 3001 / 42xx / 5004
```

### 3.3 ontology.reason

```json
inputSchema: {
  "type": "object",
  "properties": {
    "ontology_id": { "type": "string" },
    "type":        { "type": "string", "enum": ["consistency", "classification", "entailment", "semantic"] },
    "input":       { "type": "object", "description": "semantic 类型的自然语言问题及上下文" } },
  "required": ["ontology_id", "type"] }

输出 schema: { "verdict": "pass|fail|candidate", "answer": "string",
               "evidence": ["IRI"], "validation": { "owl": "bool", "shacl": "bool", "rules": "bool" } }
_meta.x-ontology: { "ontology_id": "...", "reasoning_route": "rule|owl|llm+gate" }
```

推理分级红线（设计宪法 2）：`semantic` 的 LLM 输出必须过规则校验（validation 字段即校验回执），未过校验的 verdict 只能是 `candidate`。

### 3.4 ontology.query

```json
inputSchema: {
  "type": "object",
  "properties": {
    "ontology_id": { "type": "string" },
    "sparql":      { "type": "string", "description": "只读 SPARQL；含 UPDATE/DELETE 语法即 3001 拒绝" },
    "timeout_ms":  { "type": "integer", "default": 5000, "maximum": 30000 } },
  "required": ["ontology_id", "sparql"] }

输出 schema: { "columns": ["string"], "rows": [["value"]] }
错误映射: 3001（写语法 / 超时参数）/ 42xx / 5004
```

### 3.5 memory.read

```json
inputSchema: {
  "type": "object",
  "properties": {
    "level": { "type": "string", "enum": ["session", "user", "org", "knowledge"],
               "description": "MCP 篇 §2 口径；与 memory §5.2 的 l1~l4 命名统一待裁决（§9）" },
    "query": { "type": "string" },
    "top_k": { "type": "integer", "default": 5 } },
  "required": ["level", "query"] }

输出 schema: { "memories": [{ "content": "string", "layer": "string", "score": "number",
                              "source": "l1|l2|l3|l4" }] }
_meta.x-ontology: { "memory_level_object_class": "...#MemoryFact" }
scope: memory:read:{level}（L3 须「经授权」、L4 只读走 knowledge.search，memory §5.3 矩阵）
```

### 3.6 memory.write

```json
inputSchema: {
  "type": "object",
  "properties": {
    "level":   { "type": "string", "enum": ["session", "user"],
                 "description": "L3/L4 不对 agent 开放写（memory §5.2）" },
    "content": { "type": "string" },
    "ttl":     { "type": "integer" },
    "tags":    { "type": "array", "items": { "type": "string" } } },
  "required": ["level", "content"] }

输出 schema: { "fact_id": "string", "status": "candidate" }
幂等: 按 (source_session_id, source_message_ids) 去重，重复提交返回原 fact_id（memory §5.4）
_meta.x-ontology: { "write_policy": "candidate-first", "layer_authorization": "memory:write:{level}" }
```

### 3.7 action.invoke（回写，详契约见 §7）

```json
inputSchema: {
  "type": "object",
  "properties": {
    "action_iri":    { "type": "string", "description": "必须是本体行动类（OB2 Behavior）IRI" },
    "params":        { "type": "object", "description": "行动类参数，按本体属性约束校验" },
    "confirm_token": { "type": "string", "description": "高风险（risk_level=high）动作必填，来自人工二次确认" } },
  "required": ["action_iri", "params"] }

输出 schema: { "ledger_id": "string", "idempotency_key": "string",
               "status": "pending|accepted|succeeded|failed|unknown",
               "receipt": "object|null", "action_instance_id": "string" }
```

`_meta.x-ontology` 示例（MCP 篇 §2 原样）：

```json
{ "x-ontology": {
    "action_iri": "http://ontology-agent.local/o/t1/supply#CancelOrder",
    "object_class": "http://ontology-agent.local/o/t1/supply#Order",
    "guard_rules": ["http://ontology-agent.local/o/t1/supply#R007"],
    "event_class": "http://ontology-agent.local/o/t1/supply#OrderCancelled",
    "risk_level": "high",
    "required_scopes": ["action:invoke", "supply:write"] } }
```

### 3.8 memory.invalidate（★ 本篇补充，待回填上游）

```json
inputSchema: { "type": "object",
  "properties": { "fact_id": { "type": "string" }, "reason": { "type": "string" } },
  "required": ["fact_id", "reason"] }
输出 schema: { "fact_id": "string", "status": "invalidated", "valid_to": "timestamp" }
```

语义：仅置失效标记并写 `valid_to`，不物理删除；已注入历史 prompt 的事实不回收（memory §5.4）。来源 memory §5.2，MCP 篇 §2 未列——待回填。

### 3.9 writeback.status（★ 本篇补充，待回填上游）

```json
inputSchema: { "type": "object",
  "properties": {
    "idempotency_key": { "type": "string" },
    "ledger_id":       { "type": "string" },
    "action_instance_id": { "type": "string" } },
  "anyOf": ["idempotency_key", "ledger_id", "action_instance_id"] }

输出 schema: { "ledger_id", "status", "attempts", "receipt", "needs_human", "updated_at" }
```

定位：外部调用方凭 action.invoke 返回的受理凭证（idempotency_key / ledger_id）查询回写台账状态，对应 REST 侧 `GET /admin/writeback/ledger/{id}`（01 篇 §5.8，同为补充项）。

## 4. resources 契约

| URI 模板 | 返回结构（概要） | 所需 scope |
| ---- | ---- | ---- |
| `ontology://tbox/{tenant}/{id}` | TBox 摘要：`{ontology_id, version, namespaces[], classes[{iri,label,definition}], properties[], rules[]}` | ontology:read |
| `ontology://glossary/{tenant}/{id}` | 领域术语表：`{terms[{iri,label,aliases[],definition}]}` | ontology:read |
| `knowledge://card/{kb}/{id}` | 知识卡片：`{card_id, title, summary, entity_iris[], source_refs[]}` | kb:read |

均为**只读**资源；`resources/read` 按租户可见性过滤，跨租户 URI 返回 403（2003）。版本语义：`ontology://tbox` 返回**当前发布版本**，历史版本经 REST `GET /ontologies/{id}`（版本历史）获取。

## 5. prompts 契约

领域任务模板（`prompts/list` + `prompts/get`）：

| 模板 | 参数 | 语义标注要求 |
| ---- | ---- | ---- |
| `ontology-cot`（基于本体的思维链：获取对象 → 提取数据 → 计算指标） | `{domain, task}` | 模板引用的行动类 / 规则 IRI（MCP 篇 §2） |
| 更多领域模板 | 随 M4+ 电力场景与本体发布补充 | 同上 |

prompts 无 scope 要求（模板本身不含数据）；`prompts/get` 渲染后的实际取数仍走对应 tools / resources 并按 scope 鉴权。

## 6. 鉴权与授权

| 维度 | 契约（MCP 篇 §6 + 08 篇 §2） |
| ---- | ---- |
| 外部调用 | API Key / OAuth2（Bearer + scope）；API Key 仅存哈希，scopes ⊆ owner 用户 scopes |
| 平台内调用 | 复用网关 JWT（agent 工具用内部短时 JWT，TTL ≤ 5min） |
| scope 模型 | deny-by-default、精确匹配、不支持通配符；工具级 scope + 数据级 ACL（Provider 内部实现行 / 字段级） |
| 租户隔离 | 按 tenant 隔离；注册表按租户过滤工具可见性；`mcp_invocations.tenant_id` 强制 |
| **annotations 红线** | `readOnlyHint` / `destructiveHint` 等属**不可信元数据**：仅作 UI 提示存储与展示，**不进入授权代码路径**——授权只认平台侧 scope 与 ACL |
| 限流 | 按租户 / 工具维度（Redis 计数），超限 429 + 重试提示 |
| 审计 | 所有 tool 调用留痕 `mcp_invocations`：调用方、参数摘要、结果、耗时、trace_id；高风险动作记录 confirm_by |

## 7. 回写工具特殊契约（action.invoke ↔ writeback_ledger）

- **受理即凭证**：action.invoke 不直接返回业务终态；受理成功返回 `ledger_id + idempotency_key + status=accepted`（附业务受理凭证 receipt）；状态经 writeback.status 或状态回执获取；
- **幂等键**：`idempotency_key = "{tenant_id}:{action_instance_id}"`，首次投递前先落 `writeback_ledger`（UK 约束），一切重复投递携带相同键，业务侧「同键已受理 → 返回首次受理结果」；
- **至少一次 + 消费端幂等**：平台保证至少一次投递（Outbox relay，M4 引入），不承诺恰好一次；
- **超时即 unknown**：投递超时 / 响应丢失进入 unknown 态，**禁止盲目重试**——先 `query_status` 按幂等键核实，再定终态或进对账（业务回写设计 §2.5）；
- **高风险闸**：`risk_level=high` 的行动类无 `confirm_token` 一律拒绝（Run 处于 waiting_tool 人工审批态，默认 24h 超时可配）；确认人记 `mcp_invocations.confirm_by`；
- **状态机**：`pending → accepted → succeeded|failed|compensated`（unknown 分支见业务回写设计 §2.5 状态图），台账只前进不回退；
- **失败回执同样回流**：ABox 标记行动实例失败态并附业务侧错误码，逆向闭环不断裂。

## 8. 错误语义（JSON-RPC ↔ 平台错误码）

| 层 | JSON-RPC / HTTP | 平台错误码（data 中携带） | 场景 |
| ---- | ---- | ---- | ---- |
| 传输层 | HTTP 401 | 1001 / 1002 / 1003 | 缺失 / 无效 / 过期凭据 |
| 传输层 | HTTP 403 | 2001 / 2003 / 2004 | scope 不足 / 租户不匹配 / 租户停用 |
| 传输层 | HTTP 429 | 2005 | 租户 / 工具限流（附 Retry-After） |
| 协议层 | `-32700` Parse error | 3002 | 请求体不可解析 |
| 协议层 | `-32600` Invalid Request | 3002 | 非法 JSON-RPC 请求 |
| 协议层 | `-32601` Method not found | —（工具未注册） | 未知 method / 未授权可见的工具 |
| 协议层 | `-32602` Invalid params | 3001 | inputSchema 校验失败（含 SPARQL 写语法） |
| 协议层 | `-32603` Internal error | 5999 | 未分类内部错误 |
| 工具执行层 | `tools/call` 结果 `isError: true` | 4xxx / 5xxx（业务回写设计 §4.2 汇编） | 工具已受理但执行失败：错误体放 `structuredContent`，含 `code`（平台错误码）、`message`、`trace_id`，与 REST 错误体四字段同构 |

## 9. 待办与开放问题

- [ ] ★ 回填上游：memory.invalidate、writeback.status 两工具登记进 MCP 篇 §2 能力清单；writeback.status 的所需 scope（本篇暂列 action:invoke）上游定稿；
- [ ] memory 层级命名统一：MCP 篇 `level: session|user|org|knowledge` vs memory §5.2 `layers: l1~l4`——两套口径待裁决归一；
- [ ] 工具执行层错误体（structuredContent 四字段同构）在 MCP 篇 §7 登记；
- [ ] FastMCP 版本锁定与 Streamable HTTP 会话恢复语义验证（MCP 篇待办，影响本篇 §1 会话契约）；
- [ ] Tasks opt-in 的适用行动类清单（MCP 篇待办）：哪些长任务走 MCP Tasks 而非平台任务系统；
- [ ] resources 历史版本寻址方案（如 `ontology://tbox/{tenant}/{id}?version=v3`）是否引入；
- [ ] 与 MCP 官方 Registry（server.json）互操作时 `x-ontology` 语义标注的对外发布格式（对接 Skills 篇 `x-platform` 扩展）。

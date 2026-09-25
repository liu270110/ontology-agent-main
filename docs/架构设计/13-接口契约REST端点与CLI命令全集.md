# 13 - 接口契约：REST 端点与 CLI 命令全集

> 状态：v1.0 待评审 | 日期：2026-09-26 | 上游依据：01 篇 §3、05 篇 §2/§4
>
> 定稿三件事：REST 通用约定与全量端点（操作级）、错误码全集（五段）、`oa` CLI 命令树。三出口同源校验见 05 篇 §7。

---

## 1. 通用约定

| 约定项 | 定稿 |
| ---- | ---- |
| 统一响应 | `{ "code": 0, "message": "ok", "data": ... }`；错误时 code 取 §3 错误码，`detail` 对齐 RFC 9457（05 篇 §2）；语义校验失败返回结构化 SHACL 报告，不吞成 500（01 篇 §4） |
| 分页 | cursor 分页 `?cursor=&limit=`（默认 20，最大 100），响应 `data.items` + `data.next_cursor`；管理后台表格可用 offset（01 篇 §3） |
| 排序/过滤 | `?sort=created_at`（`-` 前缀降序）；过滤参数=字段名（`?status=active`），`?q=` 全文检索 |
| 时间 | ISO8601 UTC（`2026-09-26T08:00:00Z`） |
| IRI 字段 | 本体/知识/记忆资源带 `iri`（RFC 3987）与 PG 主键 `id` 双标识：REST 路径用 `id`，语义引用/去重用 `iri`（术语表以 IRI 为主锚） |
| 幂等 | 动作类 POST（工具 invoke、审批决策、`action.invoke`、消息发送）支持 `Idempotency-Key` 头，网关去重窗口 24 h（05 篇 §2） |
| 限流 | 响应头 `X-RateLimit-*`，超限 429 + `Retry-After`（05 篇 §2） |
| SSE 订阅 | `GET /api/v1/sessions/{id}/events` 与 `GET /api/v1/runs/{id}/events`；事件 = AG-UI 蓝本裁剪扩展的 16 事件（**以 08 篇为准**）：`RUN_STARTED / RUN_FINISHED / RUN_ERROR`、`TEXT_MESSAGE_START / _CONTENT / _END`、`THINKING_START / _CONTENT / _END`、`TOOL_CALL_START / _ARGS / _END / _RESULT`、`STATE_SNAPSHOT / STATE_DELTA`、`APPROVAL_REQUIRED`、`CUSTOM`、`MESSAGES_SNAPSHOT`；`data` 统一信封 `{taskId, seq, ts, payload}`，`seq` 会话内单调递增，`Last-Event-ID` 断线续传；SSE 仅用于会话/任务事件订阅，不做 WS（ADR-8） |

## 2. REST 端点全集（96 个，14 模块）

「最低权限」为通过 PDP 五步判定（11 篇 §5）所需 Permission 码；「已认证」= 通过认证与租户隔离即可。

| 模块 | 方法 + 路径 | 说明 | 最低权限 | 备注 |
| ---- | ---- | ---- | ---- | ---- |
| **auth/iam** | POST /api/v1/auth/login | 登录，返回 access token | 匿名 | refresh 写 httpOnly cookie |
| | POST /api/v1/auth/refresh | 刷新 access token | 匿名（凭 cookie） | refresh 轮换，旧件作废 |
| | POST /api/v1/auth/logout | 登出并吊销 refresh | 已认证 | |
| | GET /api/v1/auth/me | 当前用户 + 角色 + 权限集 | 已认证 | `oa login` 连通自检用 |
| | GET /api/v1/users | 用户列表 | user:manage | 可 offset 分页 |
| | POST /api/v1/users | 创建用户 | user:manage | |
| | GET /api/v1/users/{id} | 用户详情 | user:manage 或本人 | |
| | PATCH /api/v1/users/{id} | 更新 / 停用用户 | user:manage | 审计含前后快照 |
| | PUT /api/v1/users/{id}/roles | 全量替换角色绑定 | user:manage | RoleBinding 留痕 |
| | GET /api/v1/roles | 角色列表 | user:manage | |
| | POST /api/v1/roles | 创建自定义角色 | user:manage | 不得超过自身权限 |
| | GET /api/v1/permissions | 权限字典（资源:动作全集） | 已认证 | |
| | GET /api/v1/api-keys | API Key 列表 | apikey:manage | 仅前 8 位前缀 |
| | POST /api/v1/api-keys | 签发 API Key（绑 scope） | apikey:manage | 明文仅本次返回 |
| | POST /api/v1/api-keys/{id}/rotate | 轮换 | apikey:manage | 旧钥 24h 宽限（11 篇 §4.1） |
| | DELETE /api/v1/api-keys/{id} | 吊销 | apikey:manage | 不可逆 |
| **tenants-admin** | POST /api/v1/tenants | 创建租户 | platform:admin | 管理面不对外发布 |
| | GET /api/v1/tenants/{id} | 租户详情 | tenant:manage | |
| | PATCH /api/v1/tenants/{id} | 状态 / 配置修改 | tenant:manage | 停用→全站 2011 |
| | GET /api/v1/tenants/{id}/quotas | 配额查询 | tenant:manage | |
| | PUT /api/v1/tenants/{id}/quotas | 配额调整 | tenant:manage | 审计快照 |
| | GET /api/v1/admin/stats | 平台总览（租户/用量/成本） | platform:admin | |
| **ontologies** | GET /api/v1/ontologies | 本体项目列表 | ontology:read | |
| | POST /api/v1/ontologies | 创建项目 | ontology:write | |
| | GET /api/v1/ontologies/{id} | 项目详情（当前版本） | ontology:read | |
| | PATCH /api/v1/ontologies/{id} | 元数据更新 | ontology:write | |
| | GET /api/v1/ontologies/{id}/versions | 版本列表 | ontology:read | |
| | POST /api/v1/ontologies/{id}/versions | 提交新版本（Turtle 快照） | ontology:write | 触发 SHACL 门禁 |
| | GET /api/v1/ontologies/{id}/versions/{vid} | 版本详情 / 快照下载 | ontology:read | |
| | POST /api/v1/ontologies/{id}/diff | 两版本 diff | ontology:read | body 指定 base/target |
| | POST /api/v1/ontologies/{id}/validate | SHACL 校验 | ontology:read | 结构化报告（4001） |
| | POST /api/v1/ontologies/{id}/reason | DL 推理（一致性/分类） | ontology:read | 低频，走任务队列 |
| | POST /api/v1/ontologies/{id}/publish | 发布（生成 ChangeRequest） | ontology:publish | 进审批中心 |
| | POST /api/v1/ontologies/{id}/rollback | 回滚到历史版本 | ontology:publish | 激活历史快照 |
| | GET /api/v1/ontologies/{id}/classes | 类树 / 实体类型 | ontology:read | |
| | GET /api/v1/ontologies/{id}/rules | 规则模板列表 | ontology:read | SPARQL CONSTRUCT |
| | POST /api/v1/ontologies/{id}/rules | 新增规则模板 | ontology:write | 独立版本化 |
| **kb** | GET /api/v1/kb | 知识库列表 | kb:read | |
| | POST /api/v1/kb | 创建知识库（kb_id 业务域） | kb:write | |
| | POST /api/v1/kb/{id}/documents | 登记文档，返回预签名 URL | kb:write | 直传 MinIO（01 篇 §3） |
| | GET /api/v1/kb/{id}/documents | 文档列表（生命周期状态） | kb:read | |
| | GET /api/v1/documents/{id} | 文档详情 + 同源检测结果 | kb:read | |
| | POST /api/v1/documents/{id}/extract | 触发七步流水线 | kb:write | ARQ 异步 |
| | GET /api/v1/extraction-jobs/{id} | 流水线任务状态 | kb:read | |
| | GET /api/v1/kb/{id}/candidates | 候选实例审核队列 | kb:candidate:review | |
| | POST /api/v1/candidates/{id}/review | 候选归档判定 | kb:candidate:review | 通过才写 ABox+索引 |
| | POST /api/v1/kb/{id}/search | GraphRAG 检索（local/global/DRIFT） | kb:read | `as_of` 三态语义（04 篇 §6.2） |
| | GET /api/v1/kb/{id}/graph | 子图查询（实体邻域） | kb:read | |
| | GET /api/v1/kb/{id}/terms | 术语表 | kb:read | 消歧户口本 |
| | GET /api/v1/kb/{id}/conflicts | 冲突记录列表 | kb:candidate:review | T1~T4 分诊结果 |
| **memory** | GET /api/v1/memory | 四层并行召回检索 | memory:read | L3 部分需 memory:L3:read |
| | POST /api/v1/memory/records | 手工写入（L2 本人） | memory:write | |
| | GET /api/v1/memory/records/{id} | 记录详情（出处+置信度） | memory:read | |
| | POST /api/v1/memory/records/{id}/forget | 逻辑遗忘（置 expired） | memory:write | 不物理删除（06 篇） |
| | GET /api/v1/memory/promotions | 升级申请列表 | memory:read | 本人/本域范围 |
| | POST /api/v1/memory/promotions | 发起 L2→L3 升级 | memory:promotion:submit | 进审批中心 |
| | GET /api/v1/memory/l3/timeline | L3 组织时序图 | memory:L3:read | Graphiti 式 invalidation 边 |
| | POST /api/v1/memory/export | 导出 | memory:export | MinIO 限时链接 + 审计 |
| **chat-sessions** | POST /api/v1/sessions | 创建会话 | 已认证 | |
| | GET /api/v1/sessions | 会话列表 | 已认证 | 本人范围 |
| | GET /api/v1/sessions/{id} | 会话详情 | 已认证 | |
| | DELETE /api/v1/sessions/{id} | 结束并触发归档/沉淀 | 已认证 | L1 归档 + L2 候选抽取 |
| | POST /api/v1/sessions/{id}/messages | 发消息（启动 agent run） | 已认证 | 幂等键；返回 run_id |
| | GET /api/v1/sessions/{id}/messages | 历史消息 | 已认证 | |
| | GET /api/v1/sessions/{id}/events | SSE 事件订阅 | 已认证 | AG-UI 16 事件（§1） |
| | POST /api/v1/sessions/{id}/compaction | 手动触发上下文压缩 | 已认证 | |
| **agents** | GET /api/v1/agents | 插槽列表（原生/pi/nanobot/Claude/openclaw） | agent:read | |
| | POST /api/v1/agents | 注册外部托管插槽 | agent:manage | |
| | PATCH /api/v1/agents/{id} | 启用/停用/配置 | agent:manage | |
| | POST /api/v1/agents/{id}/runs | 提交任务 | agent:run | `oa agent run` 同源 |
| | GET /api/v1/runs/{id} | 任务状态机 | agent:run | 含 waiting_approval |
| | POST /api/v1/runs/{id}/cancel | 取消任务 | agent:run | |
| | GET /api/v1/runs/{id}/events | 任务事件 SSE | agent:run | 非会话类事件流 |
| **tools** | GET /api/v1/tools | 工具注册表 | tool:read | MCP/本地/业务 API 同表 |
| | GET /api/v1/tools/search | Tool Search 按需发现 | tool:read | schema 按需下发 |
| | GET /api/v1/tools/{id} | 元数据 + schema + 语义标注 | tool:read | 含 `x-ontology` |
| | POST /api/v1/tools/{id}/invoke | 试调用 | tool:call | 写类仍走审批 |
| | GET /api/v1/tools/{id}/stats | 调用观测（成功率/延迟/成本） | tool:read | 反哺市场排序 |
| | POST /api/v1/tools/openapi-import | 业务 API 导入转工具 | tool:manage | 人工语义标注后启用 |
| **plugins** | GET /api/v1/marketplace/plugins | 市场列表 | plugin:read | |
| | GET /api/v1/plugins | 租户已安装列表 | plugin:read | |
| | GET /api/v1/plugins/{id} | 插件详情（版本/签名/状态） | plugin:read | |
| | POST /api/v1/plugins | 提交上架 | plugin:publish | 进审批中心 |
| | POST /api/v1/plugins/{id}/install | 安装 | plugin:install | 默认不装（05 篇 §5.4） |
| | POST /api/v1/plugins/{id}/uninstall | 卸载 | plugin:install | core-* 拒绝（见 §6） |
| | POST /api/v1/plugins/{id}/enable | 启用 | plugin:manage | |
| | POST /api/v1/plugins/{id}/disable | 停用 | plugin:manage | 内置插件只可停用 |
| | PATCH /api/v1/plugins/{id}/config | 租户级配置（白名单/配额/审批策略） | plugin:manage | |
| | GET /api/v1/plugins/{id}/versions | 版本列表 | plugin:read | |
| **skills** | GET /api/v1/skills | 技能列表 | skill:read | SKILL.md 资产库 |
| | POST /api/v1/skills | 上传 SKILL.md | skill:write | |
| | GET /api/v1/skills/{id}/versions | 版本列表 | skill:read | |
| | PUT /api/v1/agents/{id}/skills | 按 agent 分发技能集 | agent:manage | 渐进式披露（05 篇 §6.2） |
| **approvals** | GET /api/v1/approvals | 待办/已办列表 | approval:decide | `object_type`/`status` 过滤 |
| | GET /api/v1/approvals/{id} | 详情（含对象上下文） | approval:decide 或提交人 | |
| | POST /api/v1/approvals/{id}/approve | 通过 | approval:decide | 幂等键；审计留痕 |
| | POST /api/v1/approvals/{id}/reject | 驳回 | approval:decide | 必填 reason |
| | POST /api/v1/approvals/{id}/rollback | 生效后回滚 | approval:decide | 触发领域侧逆操作 |
| | GET /api/v1/approvals/{id}/timeline | 状态流转轨迹 | approval:decide | |
| **audit** | GET /api/v1/audit-logs | 审计查询 | audit:read | 强制租户谓词 |
| | GET /api/v1/audit-logs/{id} | 单条（含前后快照） | audit:read | |
| | GET /api/v1/audit-logs/export | 异步导出 | audit:export | MinIO 限时链接 |
| | GET /api/v1/traces/{trace_id} | 调用链查询（OTel） | audit:read | |
| **notifications** | GET /api/v1/notifications | 站内信列表 | 已认证 | |
| | POST /api/v1/notifications/{id}/read | 标记已读 | 已认证（本人） | |
| | POST /api/v1/notifications/read-all | 全部已读 | 已认证 | |
| **system** | GET /healthz | 存活探针 | 匿名 | |
| | GET /readyz | 依赖检查 | 匿名 | 五存储 + LLM 连通（01 篇 §4） |
| | GET /api/v1/meta/version | 版本协商 | 匿名 | CLI 启动探测（05 篇 §4.2） |
| | GET /api/v1/meta/openapi | OpenAPI 3.1 | 已认证 | 对外发布剥离管理面路由 |

## 3. 错误码全集

| 段 | 码 | 含义 |
| ---- | ---- | ---- |
| 1xxx 网关 | 1001 | 请求体格式错误（JSON 解析失败） |
| | 1002 | 参数校验失败（Pydantic） |
| | 1003 | 路由不存在 / 方法不允许 |
| | 1005 | 限流触发（附 Retry-After） |
| | 1006 | 幂等键冲突（24h 窗口内载荷不一致） |
| | 1007 | 请求载荷超限 |
| | 1008 | 请求超时（网关侧） |
| | 1009 | API 版本不支持 |
| | 1010 | 服务维护中 |
| 2xxx 认证 | 2001 | 未认证（缺失凭据） |
| | 2002 | access token 过期 |
| | 2003 | access token 无效（签名/受众不符） |
| | 2004 | refresh token 失效或已轮换（疑似重放） |
| | 2005 | API Key 不存在或格式错误 |
| | 2006 | API Key 已吊销/过期 |
| | 2007 | API Key scope 不足 |
| | 2008 | RBAC 权限不足 |
| | 2009 | 跨租户访问被拒 |
| | 2010 | 账号已停用 |
| | 2011 | 租户已停用 |
| 3xxx 业务 | 3001 | 资源不存在 |
| | 3002 | 状态机非法流转 |
| | 3003 | 重复提交（自然键冲突） |
| | 3004 | 依赖资源缺失（如术语未登记） |
| | 3005 | 配额超限（抽取/LLM/存储） |
| | 3006 | 术语唯一性冲突（入库门禁） |
| | 3008 | 对象审批中，禁止操作 |
| | 3009 | 插件依赖或 MCP 协议版本不满足 |
| | 3010 | 文档格式/大小不支持 |
| | 3011 | 会话已归档，不可写 |
| | 3012 | 动作未分类，默认按写类需审批（CLI-harness，05 篇 §6.3） |
| 4xxx 语义 | 4001 | SHACL 校验失败（附结构化报告） |
| | 4002 | DL 一致性检查不通过 |
| | 4003 | IRI 无法解析（不存在/跨域） |
| | 4004 | 术语歧义未消解 |
| | 4005 | 推理超时/超资源 |
| | 4006 | 规则物化执行失败 |
| | 4007 | 事实冲突待裁决（conflict_pending） |
| | 4008 | 抽取无有效候选（全被门禁拒绝） |
| | 4009 | 出处指针缺失（强制字段） |
| | 4010 | 本体版本不兼容（ABox 迁移失败） |
| 5xxx 存储 | 5001 | PG 写入/约束错误 |
| | 5002 | Neo4j 错误 |
| | 5003 | Milvus 错误 |
| | 5004 | MinIO 错误 |
| | 5005 | Redis 错误 |
| | 5006 | Outbox 事件投递失败 |
| | 5007 | 任务队列拥塞（ARQ） |
| | 5008 | 依赖健康检查失败（readyz） |
| | 5009 | 模型网关错误（LLM 渠道失败且无降级可用） |
| | 5010 | 凭据加解密失败（主密钥不匹配） |

## 4. `oa` CLI 命令树全集

退出码（05 篇 §4.2）：`0` 成功；`1` 业务错误（3xxx/4xxx）；`2` 用法错误；`3` 认证/网络错误；`4` 部分失败（批量）。下表只列需特别注意的退出码。

| 命令 | 参数选项 | 对应 REST | 退出码要点 |
| ---- | ---- | ---- | ---- |
| `oa login` | `--endpoint --api-key`（或 `OA_ENDPOINT/OA_API_KEY`） | GET /api/v1/auth/me（连通自检） | 3=认证/网络失败 |
| `oa logout` | —— | 本地凭据清除（config.toml） | 0 |
| `oa whoami` | —— | GET /api/v1/auth/me | 3 |
| `oa ontology init` | `--name --iri-ns` | POST /api/v1/ontologies | 1 |
| `oa ontology validate` | `--project --file --version` | POST /api/v1/ontologies/{id}/validate | 1（附 SHACL 报告） |
| `oa ontology diff` | `--project --base --target` | POST /api/v1/ontologies/{id}/diff | 1 |
| `oa ontology publish` | `--project --version --message` | POST /api/v1/ontologies/{id}/publish | 1；审批中返回 3008 |
| `oa ontology rollback` | `--project --to-version` | POST /api/v1/ontologies/{id}/rollback | 1 |
| `oa kb upload` | `--kb --file`（大文件预签名直传，CLI 不中转） | POST /api/v1/kb/{id}/documents | 4=批量部分失败 |
| `oa kb status` | `--job` | GET /api/v1/extraction-jobs/{id} | 1 |
| `oa kb list` | `--kb --status` | GET /api/v1/kb/{id}/documents | 0 |
| `oa memory search` | `--query --layers --as-of` | GET /api/v1/memory | 1 |
| `oa memory export` | `--format --range` | POST /api/v1/memory/export | 1 |
| `oa agent run` | `--agent --task --follow --output json` | POST /api/v1/agents/{id}/runs + GET /api/v1/runs/{id}/events | 4=run 失败；`--follow` 渲染 SSE |
| `oa plugin list` | `--marketplace --installed` | GET /api/v1/marketplace/plugins、GET /api/v1/plugins | 0 |
| `oa plugin install` | `--id --version` | POST /api/v1/plugins/{id}/install | 1 |
| `oa plugin uninstall` | `--id` | POST /api/v1/plugins/{id}/uninstall | 1（core-* 拒绝） |
| `oa tool list` | `--domain` | GET /api/v1/tools | 0 |
| `oa tool search` | `--query --limit` | GET /api/v1/tools/search | 0 |
| `oa tool call` | `--id --args --idempotency-key` | POST /api/v1/tools/{id}/invoke | 1；写类提示将走审批 |
| `oa approval list` | `--type --status` | GET /api/v1/approvals | 0 |
| `oa approval approve` | `--id --comment` | POST /api/v1/approvals/{id}/approve | 1 |
| `oa approval reject` | `--id --reason` | POST /api/v1/approvals/{id}/reject | 1 |
| `oa admin tenant` | `--create --suspend` | POST /api/v1/tenants、PATCH /api/v1/tenants/{id} | 1 |
| `oa admin quota` | `--tenant --get --set` | GET/PUT /api/v1/tenants/{id}/quotas | 1 |

输出与配置：默认人类可读表格，`--output json`（强制结构化 + UTF-8）/`--output yaml` 可选；profile 多环境配置见 05 篇 §4.2。

## 5. 版本与弃用策略

引用 05 篇 §2/§7：`/api/v1` 冻结破坏性变更；破坏性演进开 `/api/v2`，v1 保留 ≥6 个月双跑；弃用提前两个小版本公告 + 响应头/日志警告；OpenAPI 3.1 对外发布前剥离管理面路由（tenants-admin、audit 写操作、`/api/v1/admin/*`）；三出口签名同源由 CI 门禁校验（工具注册中心为单一事实源）。

## 6. 开放问题

- [ ] 平台扩展 SSE 事件（审批状态推送、流水线进度）的命名与载荷——随 08 篇定稿
- [ ] 5009 模型网关错误归 5xxx 外部依赖段是否合适，或单列 6xxx
- [ ] `oa plugin uninstall` 对 core-* 的拒绝码（3009 语义不贴切，拟新增 3013）
- [ ] 管理面是否独立 `/api/admin` 前缀，简化对外契约剥离
- [ ] offset 分页深翻页防护；Webhook 出站订阅（05 篇 §7 已列，v2）

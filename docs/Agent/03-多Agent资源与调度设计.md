# 多 Agent 资源与调度设计（resource & scheduling）

> 状态：v0.1 | 日期：2026-09-26 | 上游依据：[内核/能力层划分](./02-Agent内核与能力层划分设计.md)（A4 预算/B1 门禁/C3 租户）、[Agent服务设计](./Agent服务设计.md)、[研究整理/07 §2.2](../研究整理/07-Harness-Agent解剖与本体骨架.md)（子代理编排）、[架构锚点](../architecture/01-总体架构与分层.md)
>
> 本文回答：**同一个工具（/沙箱/模型/会话）什么时候一个 agent 独占、什么时候多 agent 共用**；执行、任务、会话在多 agent 场景下的资源语义。它是内核机制的自然延伸：资源语义**标注在本体行动类上、由内核的 ResourceManager 强制**——「本体为骨架」在资源层的落点。

---

## 1. 问题与总原则

平台是多 agent 的：同一租户下多个 Agent 实例（builtin/claude/pi…）、每会话一个活跃 Run、子代理（orchestrator-workers）并行。它们争抢五类资源：**工具、沙箱/执行后端、模型并发、外部 API 配额、会话与任务本身**。

三条总原则：

1. **共享是默认，独占是声明**：不标注就共享（只受租户限流），需要独占必须在行动类上显式标注并发语义——防止把所有工具都当独占锁死吞吐；
2. **语义进本体，调度进内核**：并发语义是行动类标注（数据，可评审可治理），租约与排队是内核 ResourceManager（代码，不可被能力层触碰）——与 B 类安全基线同级；
3. **对话路径优先**：资源争抢时优先级固定为 `chat(对话) > task(后台任务) > batch(批量/流水线) > rsi(自进化)`，防后台饿死交互。

## 2. 工具的并发语义（本体标注，三级）

行动类新增标注 `ob2:concurrency ∈ {shared, pooled, exclusive}` + `ob2:leaseTimeout`（独占租约上限），发布时过 lint（缺省=shared；exclusive 必须附理由）：

| 级别 | 语义 | 判定准则 | 典型例子 | 内核行为 |
| ---- | ---- | ---- | ---- | ---- |
| **shared**（默认） | 无限共享 | 无状态或幂等读；并发安全 | 查询类工具、knowledge.search、只读 MCP | 直接放行，仅计租户级 RPM/TPM |
| **pooled** | 实例池共享 | 有会话态但可多实例（登录态/连接池），实例数有限 | 浏览器实例、外部系统账号池、SSH 连接 | 从池取实例绑定到 Run；池空则排队（waiting_tool），归还后复用 |
| **exclusive** | 租约独占 | 物理设备、单许可证、写共享外部资源、不并发安全 | 单许可 ERP 写接口、一台设备、Git 工作区 | Redis 租约锁（`lock:tool:{tenant}:{action_iri}`，TTL=leaseTimeout）；持有期间其他 agent 请求进队列，超时 RUN_ERROR 4103 TOOL_BUSY |

**判定决策表**（建模评审用）：

| 问题 | yes → |
| ---- | ---- |
| 工具无状态且只读？ | shared |
| 有登录态/连接但可开 N 份？ | pooled（标注池上限） |
| 并发调用会产生交叉写坏数据？ | exclusive |
| 外部许可/设备只有一份？ | exclusive |
| 不确定？ | 先 exclusive 观察队列指标，再降级（降级走 changeset，升密容易放密难） |

## 3. 执行与计算资源

| 资源 | 模型 | 说明 |
| ---- | ---- | ---- |
| **沙箱/执行后端** | 实例池（pooled） | 容器池按租户配额（lite 默认并发 2）；executionMode=code 的行动类独占一个沙箱实例直到 Run 结束；池空排队 |
| **模型并发**（单卡 Ollama） | 全局信号量 | RTX 4060 8GB 单轨：对话路径预留 1 并发保证，批量任务（抽取/嵌入）共享剩余并在显存压力下让路（对齐 memory §9 空闲闸门同款信号） |
| **外部 API 配额** | 令牌桶按租户 | 挂在 ModelGateway/ToolPort 出口，超限排队不报错；kb 批量与对话路径**分桶**（审计已登记的背压联动在此落地） |
| **租约表** | `resource_leases`（PG） | resource_type/resource_key/holder(run_id)/expires_at/queue_pos；Redis 锁做快路径，PG 表做权威与审计 |

## 4. 会话与任务的多 agent 语义

**4.1 会话归属**：一个 Session 绑定一个 Agent（现行不变）；**主 agent 可派生子代理**（07 §2.2-4）：子代理 = 隶属同一 Task 的独立 Run + 干净上下文窗口 + 只回传结构化 Artifact——子 Run 共享父 Task 的资源配额与 trace，**不共享对话历史**（防窗口污染）。取消语义：取消 Task 级联取消全部子 Run。

**4.2 并发上限（分层桶）**：单 Session 单活跃 Run（不变）→ 单 Agent 并发 Run 上限（exclusive 工具型 agent 建议配 1）→ 单租户并发 Run 上限（默认 10）→ 全局。超限进队列，任务中心可见排队位次。

**4.3 冲突仲裁**：两个 agent 争同一 exclusive 工具：按 §1 优先级 + FIFO；**无抢占**（externalWrite 已开始执行不可打断——中止等于写坏外部状态）；waiting 排队计入 Run 的 waiting_tool 超时预算；死锁防护：一次 Run 同时最多申请 1 个 exclusive 租约（需要多个时分步申请、用完即还，禁止持有等待）。

**4.4 与门禁的关系**：exclusive/pooled 申请发生在 gates.pre **通过之后**、执行之前（B1 基线门禁不通过就不占资源）；租约获取失败回喂 LLM 结构化错误（"工具忙，预计等待 Xs"——错误即反馈原则）让它自行决策等待或换路径。

## 5. 数据模型与 API

- `resource_leases` 表要点（回填 database/01 待办）：id/tenant_id/resource_type(tool/sandbox/model/api)/resource_key/holder_type/holder_id(run_id)/status(granted/queued/released/expired)/expires_at/created_at；UK 活跃租约 `(tenant_id, resource_type, resource_key) WHERE status='granted'`（exclusive 用）；
- 行动类标注列：`classes.metadata->concurrency/leaseTimeout/poolSize`（免 DDL，随发布版本）；
- 端点（api/01 登记待办）：`GET /admin/resources`（租约与队列视图）、`POST /admin/resources/leases/{id}/revoke`（管理员强制释放，全程审计）；
- 指标（07 §6 登记）：`resource_queue_depth{resource_type}`、`tool_busy_rejected_total`、`lease_wait_seconds`。

## 6. 验收标准

- [ ] 并发语义 lint：exclusive 无理由被拒；缺省 shared 可查；
- [ ] 三级行为测试：shared 并发 10 Run 无阻塞；pooled 池=2 时第 3 个排队且归还后复用；exclusive 双 agent 竞争一得一排队、超时得 4103；
- [ ] 子代理：派生 3 子 Run 共享配额与 trace、Artifact 回传、取消级联；
- [ ] 死锁演练：尝试双 exclusive 申请被拒；优先级 chat>batch 生效；
- [ ] 管理员强制释放后队列首位的 agent 获得租约，全程审计留痕。

## 7. 待办与开放问题

- [ ] `resource_leases` DDL 回填 database/01 与 06 篇；4103 TOOL_BUSY 错误码登记 02 篇；
- [ ] 池实例的预热与回收策略（冷启动延迟预算）；
- [ ] 跨租户资源隔离的例外通道（平台级共享设备的租约归属裁决）；
- [ ] 子代理协议与 AgentSlot 的合并落地（承 Agent/02 §10 既有待办）。

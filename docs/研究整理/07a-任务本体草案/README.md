# 07a - 任务本体 v0.2 草案（设计说明）

> 日期：2026-09-26 | 上游：[07 篇 v0.2 §5.2](../07-Harness-Agent解剖与本体骨架.md)、[_qa/07 评审记录](../_qa/07-多专家评审与工程头脑风暴.md)
>
> 本目录是 07 篇"任务本体九类"的代码化落地：[task-ontology.ttl](./task-ontology.ttl)（TBox + SHACL shapes，单文件草案版）+ [_qa 校验自测脚本](../_qa/07a-validate-task-ontology.py)。

---

## 1. 交付物与验证状态

| 交付物 | 内容 | 状态 |
| ---- | ---- | ---- |
| `task-ontology.ttl` | 9 个任务类 + 7 组枚举词表（ExecutionMode/TrustLevel/StepState/PlanStatus/PlanOrigin/TaskState/ActorType/ExecutorKind）+ 属性 + **14 个 SHACL NodeShape** | ✅ 可被 rdflib 7.6 / pySHACL 0.40 加载 |
| `_qa/07a-validate-task-ontology.py` | 三例自测：S1 合法图 conform；S2 判据引用 agentAttested 事实被拒；S3 dependsOn 成环 + 行动类缺标注被拒 | ✅ 三例全绿 |

## 2. 命名空间策略

- 草案用占位域 `https://ontology-agent.example/ontology/task#`；**部署时按国标 IRI 规则替换为本体管理机构域名**，业务本体用独立命名空间（如 `ex:`），两者按 GB/T 48000.3 第 9 章扩展原则隔离。
- 枚举用**命名个体**（如 `task:stValidated`）而非字符串字面量：状态词表封闭可被 `sh:in` 强制，迁移合法性由规则模板层引用这些 IRI。

## 3. 九类 ↔ 07 篇 §5.2 映射（无增减）

Task / Plan / Step（`dependsOn` 构成 DAG）/ Precondition / Postcondition / SuccessCriterion / Artifact（信任级 + 出处指针）/ FailureMode（重试计数 + 补偿行动类）/ StateChange（归因元类：主体三分类 + 决策 ID + 能力包版本 + 时间）/ Executor。评审 P1 修复全部内嵌：Task 带预算（maxSteps/maxTokens/deadline）、shapeRef 带 shapeVersion、能力包变更强制 capabilityVersion。

## 4. 三层校验的归属（本文件只承担第一层半）

| 层 | 内容 | 归属 |
| ---- | ---- | ---- |
| 节点层 | 行动类存在、参数域、枚举封闭、必填、基数、**判据信任级**、**归因完整** | 本文件 SHACL ✅ |
| 图层（部分） | dependsOn 无环、每任务至多一个 active Plan、条件至少一种表达、能力包必带版本 | 本文件 `sh:sparql` 约束 ✅（pySHACL advanced 模式） |
| 图层（其余） | 跨步产物产消匹配、互斥与顺序约束、**状态机迁移合法** | SPARQL 图结构约束 / CONSTRUCT 规则模板，**版本化管理**，不进本文件 |
| 语义层 | 前置/后置类不相交、整体一致性 | DL 推理（owlready2/HermiT），规划期与检查点低频执行 |

## 5. 信任级强制的两半（诚实边界）

- **结构层（已验证）**：`SuccessCriterion → citesFact → { trustLevel 必须为 externallyVerified }`——S2 自测证明判据引用 agent 自证事实会被 SHACL 拒绝。
- **文本层（运行期，未在本文件）**：`criterionQuery` 是 SPARQL ASK 文本，SHACL 无法静态验证查询文本只触碰外部验证事实。运行期防线：判据求值以**只读绑定**执行 + 投影上预跑一次"查询可达性检查"（查询图模式涉及的所有谓词/类白名单），白名单外拒绝求值——这是语义层 `validation` 模块的实现项（M2），不是本体文件能表达的。

## 6. 与运行时的接口约定

- **门禁求值载体**：前置门禁的 askQuery 打在**会话内任务级 RDF 投影**上（TBox 子图 + 激活 shapes + 任务 ABox 子图的内存合并图），不打在 Neo4j 存储（无 SPARQL 端点）——07 篇 §6.2-3。
- **状态机迁移**：`stPlanned → stGated → stExecuting → stWaitingApproval → stValidated / stFailed / stSuspended` 的合法迁移表由规则模板层声明，非法迁移在写回前被拒绝；本文件只锁词表。
- **写回协议对齐**：每次写 ABox 的 StateChange 归因四元组（actorType/actorId/decisionId/capabilityVersion）即本文件 `task:StateChange` 的实例——任取三元组可归因（评审提问 9 的落实）。

## 7. 开放问题

- [ ] 正式命名空间域名（随部署定，替换占位域后全文 IRI 需重生成——脚本化替换）
- [ ] 多租户下的命名空间方案：共享任务本体 + 租户实例图分区（`task:` 词表共享、实例 IRI 带租户段？）
- [ ] `criterionQuery` 的运行期白名单复检实现（M2 语义层 validation 模块）
- [ ] 与 PG 恢复账本的字段映射（StateChange ↔ PG 步快照表的外键关系）
- [ ] `sh:sparql` 约束在投影上的执行成本实测（并入门禁投影 PoC）

# Agent 内核与能力层划分设计（kernel vs capability layer）

> 状态：v0.1 | 日期：2026-09-26 | 上游依据：[Agent服务设计](./Agent服务设计.md)、[研究整理/07](../研究整理/07-Harness-Agent解剖与本体骨架.md)（六模块/五规律/八扩展点/四通道）、[DSec 技术报告](../Dsec/DSec-技术报告.md)（双组件/可组合环境层/出口控制/pack_diff）、[研究整理/01 §3](../研究整理/01-Agent工具调研.md)（hermes-agent）、[架构锚点](../architecture/01-总体架构与分层.md) §3.4
>
> 本文回答一个问题：**agent 运行时里，哪些机制是内核（不可下放），哪些机制下放到能力层（插件/工具/Skills/MCP 四通道的哪一个）**。它是 07 篇「能力层可插拔设计」的工程裁决版：07 给了扩展点与通道的框架，本文给出**逐机制的裁决清单与判定准则**。冲突时以本文为准（更具体），本文与锚点冲突以锚点为准。

---

## 1. 判定准则（一个机制能否下放，过四条测试）

| # | 测试 | 问题 | 不过测试 ⇒ 进内核 |
| ---- | ---- | ---- | ---- |
| **T1 安全基线** | 该机制被绕过/篡改是否直接导致越权、数据外泄或虚假完成？ | 「冻结账户」的权限门被插件短路 = 事故 | 安全基线**硬编码于内核**，能力只能叠加不能替换（07 §7.4 铁律 1） |
| **T2 状态主权** | 该机制是否拥有/仲裁任务状态与完成判定？ | 判据求值若外包给插件，插件可宣告「任务完成」 | 状态主本（ABox）与判据求值归内核+语义层；插件只能**申报**事实，不能**裁决** |
| **T3 循环语义** | 该机制是否定义 agent loop 本身的阶段与转移？ | 循环骨架可插拔 = 平台不再是平台 | loop 七阶段与状态机归内核；阶段内的**策略**可下放 |
| **T4 普适必要性** | 拿掉它，是否任何任务都跑不起来或不可审计？ | 审计/trace/预算拿掉 = 黑盒失控 | 横切必需品归内核；**领域差异化的部分**下放 |

**推论**：一个机制常可拆成「内核骨架 + 可下放策略」两半——如规划：loop 里的规划**阶段**是内核，「怎么规划」（模板/组合/自由三档策略）下放。判定的粒度是**机制的一半**，不是整个模块。

## 2. 内核机制清单（不可下放，三类十二项）

### 2.1 A 类：循环骨架（T3）

| # | 机制 | 内容 | 参照 |
| ---- | ---- | ---- | ---- |
| A1 | **agent loop 七阶段** | 装载（Grounding+确认）→ 分级预判 → 规划 → 逐步循环（组装→门禁→执行→后验→写回）→ 漂移检测 → 闭环落账 | 07 §6；Claude Code「gather→act→verify→repeat」 |
| A2 | **Run/Step 状态机** | queued→running⇄waiting_tool→终态；StepState planned→gated→executing→waiting_approval→validated→failed；非法迁移被拒绝 | 04 篇权威状态机；迁移合法性内核断言 |
| A3 | **扩展点分发器** | 循环只认八扩展点接口（§4），内核不 import 任何具体能力；分发器本身是内核 | pi extensions 模式 |
| A4 | **预算与终止判定** | token/步数/时长/成本四维预算；终止只认判据求值与预算耗尽，**不认模型自述** | 07 §3 规律 5：策略永不交给模型 |

### 2.2 B 类：安全与信任基线（T1/T2，硬编码，任何通道不可替换）

| # | 机制 | 内容 | 参照 |
| ---- | ---- | ---- | ---- |
| B1 | **门禁基线** | 平台所有的前置/后验基线校验（行动类枚举合法性、参数域、executionMode 分级审批、scope 校验）；**包 gate 只增不替** | 07 §7.4 铁律 1 |
| B2 | **判据求值器** | SuccessCriterion 在任务级 RDF 投影上 focus-node 求值；**只认外部回执（信任级 externally_verified）先于判据**，不认 agent 自写状态 | 重型本体优化 §5.1；DSec「rollout 状态单一事实源」同构 |
| B3 | **信任级与不可信标界** | ABox 事实信任级（externally_verified/agent_attested）；一切外部输入（工具结果/证据/记忆/错误回流/插件输出）统一视为不可信、注入标界 | 07 §6.7；MCP annotations 红线同源 |
| B4 | **出口控制** | 能力执行容器默认无网、宿主白名单代理、静态 IP/域名逐包放行；权限判定只认平台 scope+ACL 与容器内进程身份无关；v2 才上 eBPF 动态语义 | DSec §6.5；07 §7.5 v1 降级承诺 |
| B5 | **审批路由** | waiting_tool 的人工审批语义（参数哈希绑定、聚合审批、超时默认拒绝）；审批是内核路由，**审批策略（谁审）按治理档位**由 08 篇配置，不是插件 | Agent 篇；08 §2.4 |

### 2.3 C 类：状态与账本（T2/T4）

| # | 机制 | 内容 | 参照 |
| ---- | ---- | ---- | ---- |
| C1 | **写回跨库协议** | ABox=执行状态语义主本，PG=审计/恢复账本（步快照+工具原始输出指针+StateChange 归因）+Outbox 幂等写透；写意图锁；**内核可重启可替换而状态不丢** | 07 §6.2-3；DSec 双组件「重连续跑替代日志重放」 |
| C2 | **trace/审计/成本记账** | trace_id 贯穿、每步落账、token/成本归因 | 锚点 §6.7 |
| C3 | **多租户 scoping** | 租户上下文由平台侧注入扩展点参数，**插件不得自取** | 07 §7.4 契约 |

> 内核规模目标：A+B+C 合计代码量可控在单包（`agent_runtime/kernel/`），**CI 断言内核不 import 任何能力实现**（import-linter 契约，standards/01 §2.1 补条）。

### 2.4 取消完整性：取消清单化传播（2026-09-26 痛点优化）

取消不是改一行状态。取消传播不彻底会留下子 Run 孤儿、在途工具调用与悬挂租约（[台账](../architecture/11-链路排查与模块痛点台账.md)「内核×取消」P1）。裁决：取消是状态主权机制（T2/T4），**归内核**——插件不得自判「已取消」，取消按固定清单顺序传播：

| 步 | 清单项 | 动作 |
| ---- | ---- | ---- |
| 1 | 子 Run 级联取消 | 递归向全部子 Run/子代理下发取消，各自走同一清单 |
| 2 | 在途工具调用中止 | 中止执行中的 tool call（不可中止的只读调用放行收尾）；未闭合 tool_call 以取消错误闭合（04 篇 task 不变式：每个 tool_call 必须闭合才进终态） |
| 3 | exclusive 租约 / Pooled 实例强制释放 | 强制释放 exclusive 资源租约、归还 Pooled 实例——**不等持有方优雅释放** |
| 4 | 工作区标记 cancelled | 工作区（[04-工作区与文件管理](./04-工作区与文件管理.md)）标记 cancelled，剩余善后交回收任务 |

两条纪律：

- **cancelled 为终态但资源释放先行**（run 状态机见 04 篇 §3）：清单执行完毕才落终态，落态即代表零残留；
- **取消动作自身带超时兜底（5s 强制）**：任一步卡死即强制置 cancelled，未完成清单项交后台回收任务按同一清单补扫——取消不能被取消卡死。

取消全过程留审计与 trace（C2）。验收：**取消后租约表零残留用例**——取消一个含多层子 Run 与 exclusive 租约的任务，断言租约表无该任务残留行；含「工具调用卡住取消、5s 被强制」分支。

## 3. 可下放机制清单（下放到哪条通道）

先给通道速查（07 §7.3，权威细则在 [Skills 篇](../Skills/技能与插件设计.md) §四通道）：**L0 MCP 工具**（纯工具）/ **L1 SKILL.md**（程序性知识，纯提示层）/ **L2 能力包**（server.json 扩展：工具+规则包+供给器+校验器+技能成套捆绑）/ **L3 内置扩展点**（平台原生 Python API）。

| 机制（07 六模块→细分） | 裁决 | 下放通道/扩展点 | 边界（内核保留的部分） |
| ---- | ---- | ---- | ---- |
| **工具实现**（业务 API、查询） | ✅ 全量下放 | L0 MCP / L2 包内 tools → `tools.bindings` | 行动类枚举与语义标注归本体；scope 校验归内核 B1 |
| **技能/程序性知识**（操作流程、领域 SOP） | ✅ 全量下放 | L1 SKILL.md / L2 包内 skills | 与判据/状态机交互的技能过专项审核（07 §6.7-4） |
| **上下文供给器**（外部数据源、专用召回） | ✅ 下放 | L2 包 / L3 → `context.providers` | 组装器骨架（冻结前缀三断点+遮蔽式 schema+绝对预算）归内核；平台内置供给器（GraphRAG/记忆/TBox 摘要）= L3 首批实现 |
| **规划策略**（模板库、领域规划法） | ✅ 下放 | L1（模板即技能）/ L2 包 → `planning.strategies` | 规划**阶段**与产物三层校验归内核；计划模板库的固化通道（pack_diff 精神）归平台运营 |
| **规则与校验器（增量）**（行业规则包、shape 包） | ✅ 下放 | L2 包（gates 项默认禁用待平台复核）→ `gates.pre/post` | **基线门禁 B1 只增不替**；包 gate 在投影上毫秒级、结构化报告 |
| **记忆策略**（沉淀判定、衰减、召回权重） | ⚠ 部分下放 | L2/L3 → `memory.policies` | **L2→L3 升级隐私门禁不外包**；四层结构与不可物理删除底线归平台（memory 篇） |
| **推理引擎（确定性侧）**（外挂 reasoner、规则物化） | ⚠ 部分下放 | L3 → `reasoning.engines`（ADR-6 替换位） | 引擎可换、**路由器与分级宪法不可换**（05 篇：调用方禁自选引擎）；外部引擎默认禁用、平台复核 |
| **执行后端**（Docker/microVM/远程沙箱） | ⚠ 部分下放 | L3（`execution.backends`） | 出口控制 B4 与审批路由 B5 不随后端走；v1 仅 Docker，DSec 四后端 v2 评估 |
| **事件汇/通知**（webhook、钉钉） | ✅ 全量下放 | L0/L2 → `event.sinks` | Outbox 与审计 sink 是内核必选，不可卸载 |
| **模型渠道** | ✅ 已下放 | L7 LiteLLM（ModelPort） | 预算与 fallback 策略归内核 A4 |
| **子代理编排策略**（workers 拓扑、Artifact 契约） | ⚠ 部分下放 | L3（AgentSlot 协议实现） | AgentSlot 协议（结构化 Artifact 回传、窗口隔离）归内核；拓扑策略可配置 |
| **沙箱代码行动类执行器**（executionMode=code） | ✅ 下放 | L3 执行后端 + L2 代码包 | 最严出口（默认无网+白名单+产物过 shape）硬编码 |

## 4. 八扩展点 × 内核/能力契约（一页速查）

| 扩展点 | 内核提供 | 能力提供 | 通道 |
| ---- | ---- | ---- | ---- |
| `context.providers` | 组装器骨架（三断点冻结/遮蔽/预算）、标界注入 | ContextBlock 内容 | L2/L3 |
| `planning.strategies` | 规划阶段调用、三层校验 | 计划图候选 | L1/L2 |
| `gates.pre` / `gates.post` | 基线校验、求值载体（投影）、合成顺序（基线先包后） | 增量校验器 | L2（默认禁用待复核） |
| `tools.bindings` | 行动类注册表、scope 校验、审批路由 | 工具实现 | L0/L2 |
| `reasoning.engines` | 路由器与分级宪法 | 引擎实现 | L3 |
| `memory.policies` | 四层结构、隐私门禁、遗忘底线 | 判定/衰减/权重策略 | L2/L3 |
| `event.sinks` | Outbox、审计 sink（必选） | 外部通知 | L0/L2 |
| `execution.backends`（07 执行阶段的落点） | 出口控制、审批语义 | 沙箱实现 | L3 |

### 4.1 Protocol 签名（draft v0.1，M3 冻结）——2026-09-26 设计定稿

> 状态：**draft v0.1，随 M3 内核骨架实现冻结**（§8 里程碑表）；冻结前签名可随实现微调，冻结后按 §7.1 CI 静态契约管理——**八扩展点 Protocol 签名变更 = 破坏性变更，须锚点评审**。落点：`agent_runtime/kernel/extensions.py`。编号对应 §4 表八行（gates 一行含 pre/post 两个 Protocol，共九个类）。
>
> 全部签名的公共纪律：① 每个方法必带 `ctx: TenantContext`（C3：租户上下文由平台侧从 JWT/任务装载注入，**实现不得自取、不得跨任务缓存**）；② 每个方法必带 `timeout_ms` 关键字参数（默认值=建议上限，**硬上限由内核按 A4 预算与循环阶段钳制**，超时按失败分支处理、走 08 篇 §5 降级矩阵）；③ 每个实现携带 `meta: ExtensionMeta`（名称/版本/**本体语义标注**/来源包 id）——无语义标注不上架（§7.4）。

```python
# agent_runtime/kernel/extensions.py —— 八扩展点 Protocol 签名（draft v0.1）
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

# ── 内核持有的公共值对象（内核定义、能力只消费；TaskRef/StepResult 等
#    领域值对象随 A1/A2 内核聚合定义，此处不重复） ─────────────────────
@dataclass(frozen=True, slots=True)
class TenantContext:
    """租户上下文（C3）：平台侧注入，插件不得自取。"""
    tenant_id: UUID
    user_id: UUID | None
    roles: tuple[str, ...]
    scopes: tuple[str, ...]
    trace_id: str

@dataclass(frozen=True, slots=True)
class ExtensionMeta:
    name: str                              # 命名空间.名称，如 coldchain.iot_feed
    version: str                           # semver
    semantic_annotation: dict[str, Any]    # 绑定的本体 IRI（行动类/规则类/概念类）
    provider_pack_id: str | None = None    # 来源能力包；L3 内置实现为 None


# ① context.providers —— 上下文供给器
@runtime_checkable
class ContextProvider(Protocol):
    """上下文供给器（`context.providers`，通道 L2/L3；组装器骨架归内核 A1/B3）。

    职责：在装载（Grounding+确认）与逐步循环的「组装」断点，把外部数据源 /
    专用召回（IoT 流、业务库、领域检索器）产出为 ContextBlock，交内核组装器
    做三断点冻结、遮蔽式 schema 裁剪与绝对预算分配；平台内置供给器
    （GraphRAG/记忆/TBox 摘要）即本接口的 L3 首批实现。

    契约（违反即拒载）：
    - 产出内容一律视为**不可信外部输入**：ContextBlock.trust_level 不得自称
      externally_verified，注入标界与降权由内核负责（B3）；
    - budget_tokens 是**绝对上限**：超预算的块由内核截断或丢弃，供给器自报
      token 数仅供核对；
    - 幂等：同 (task, step) 重复调用应返回等价内容（漂移检测会重放）；
    - 禁止直写任何存储（写回走 C1 协议）、禁止发起工具调用。
    """
    meta: ExtensionMeta

    def provide(
        self,
        task: "TaskRef",                   # 任务装载引用（任务本体实例 IRI + 计划上下文）
        step: "StepRef | None",            # 步循环内调用给当前步；装载期为 None
        ctx: TenantContext,
        *,
        budget_tokens: int,                # 绝对预算（组装器骨架分配）
        timeout_ms: int = 3_000,           # 建议上限 3s；硬上限随组装断点预算钳制
    ) -> "ContextBlock": ...


# ② planning.strategies —— 规划策略
@runtime_checkable
class PlanningStrategy(Protocol):
    """规划策略（`planning.strategies`，通道 L1/L2；规划阶段与三层校验归内核 T2/T3）。"""
    meta: ExtensionMeta

    def plan(
        self,
        task: "TaskRef",
        ctx: TenantContext,
        *,
        mode: str = "template",            # template | composite | free 三档
        timeout_ms: int = 10_000,
    ) -> "PlanCandidate": ...              # 计划图候选：仍是本体实例候选，过三层校验


# ③ gates.pre / gates.post —— 增量门禁（基线只增不替）
@runtime_checkable
class PreGate(Protocol):
    """前置门禁（`gates.pre`，通道 L2、上架默认禁用待复核；基线校验 B1 归内核）。

    职责：在「门禁」断点对待执行动作做增量前置校验（行业规则包、SHACL
    shape 包），在任务级投影上毫秒级完成。

    契约（违反即拒载）：
    - **只增不替**：本 gate 只能新增违例，不得覆盖/放行基线校验结果——合成
      顺序固定「基线先、包后」，包 gate 无权改写基线 GateReport（铁律 1）；
    - 输入 decision 由内核从 ABox 投影构造，gate 不得反查外部状态做判定；
    - GateReport 必须结构化（focus node + 规则 IRI + severity + message），
      供「错误即反馈」回流与审计；
    - 确定性：同输入必同输出（禁随机、禁 LLM 调用）——高频逻辑走规则
      （推理分级宪法）。
    """
    meta: ExtensionMeta

    def check(
        self,
        decision: "ActionDecision",        # 行动类 IRI + 槽位参数 + executionMode
        ctx: TenantContext,
        *,
        timeout_ms: int = 100,             # 投影上毫秒级：硬上限 1_000
    ) -> "GateReport": ...


@runtime_checkable
class PostGate(Protocol):
    """后验校验器（`gates.post`，通道 L2、上架默认禁用待复核）。

    契约同 PreGate（只增不替、结构化报告、确定性），对象从「待执行动作」
    换为「已执行步产物」（含工具原始输出指针）：ValidationReport 的错误供
    重生成与降级安全回答分支消费；**不得修改 StepState**（状态主权 T2）。
    """
    meta: ExtensionMeta

    def validate(
        self,
        result: "StepResult",
        ctx: TenantContext,
        *,
        timeout_ms: int = 1_000,
    ) -> "ValidationReport": ...


# ④ tools.bindings —— 工具绑定
@runtime_checkable
class ToolBinding(Protocol):
    """工具绑定（`tools.bindings`，通道 L0/L2；行动类注册表、scope 校验与
    审批路由 B1/B5 归内核）。

    职责：把本体**行动类**绑定到具体实现（MCP 工具/本地函数/规则执行器/
    沙箱代码），在「执行」断点被内核调用；行动类枚举与语义标注归本体。

    契约（违反即拒载）：
    - 实现必须声明 required_scopes（授权唯一依据）；MCP annotations 仅
      UI 提示、不参与授权（红线）；
    - 高风险（executionMode 分级）动作：内核先行审批路由（B5），未携有效
      ApprovalTicket 的调用一律拒绝——实现不得自查自放；
    - 参数遵循「值不经采样」：数字/ID/枚举值由调用方槽位填充，实现不得
      改造为自由文本（幻觉防线 §5）；
    - ToolResult 一律视为不可信外部输入（B3 标界）；失败必须结构化错误
      返回（供错误回喂 LLM 自行决策），不得抛裸异常逃逸循环；
    - 沙箱类实现（executionMode=code）另见 ExecutionBackend：出口控制 B4
      硬编码、不随后端走。
    """
    meta: ExtensionMeta

    def invoke(
        self,
        call: "ToolCall",                  # 行动类 IRI + 槽位参数 + 参数哈希
        ctx: TenantContext,
        *,
        approval: "ApprovalTicket | None" = None,   # B5 放行回执（需审批动作必填）
        timeout_ms: int = 30_000,          # 单工具调用建议上限 30s
    ) -> "ToolResult": ...


# ⑤ reasoning.engines —— 确定性推理引擎（ADR-6 替换位）
@runtime_checkable
class ReasoningEngine(Protocol):
    """推理引擎（`reasoning.engines`，通道 L3；路由器与分级宪法不可换——调用方禁自选引擎）。"""
    meta: ExtensionMeta

    def run(
        self,
        request: "ReasoningRequest",       # consistency | classification | entailment | 规则物化
        ctx: TenantContext,
        *,
        timeout_ms: int = 60_000,
    ) -> "ReasoningResult": ...


# ⑥ memory.policies —— 记忆策略（L2→L3 升级隐私门禁不外包）
@runtime_checkable
class MemoryPolicy(Protocol):
    """记忆策略（`memory.policies`，通道 L2/L3；四层结构与遗忘底线归平台，docs/memory 篇）。"""
    meta: ExtensionMeta

    def judge(
        self,
        candidates: "list[MemoryCandidate]",
        ctx: TenantContext,
        *,
        timeout_ms: int = 5_000,
    ) -> "list[ConsolidationDecision]": ...   # ADD/UPDATE/DELETE/升级（升级仅产工单，不直写 L3）

    def weight(
        self,
        item: "RecallItem",
        ctx: TenantContext,
    ) -> float: ...                           # 召回加权（w_layer 之上的策略项）


# ⑦ event.sinks —— 事件汇（Outbox 与审计 sink 是内核必选，不可卸载）
@runtime_checkable
class EventSink(Protocol):
    """事件汇（`event.sinks`，通道 L0/L2；只做外部通知，不替代 Outbox/审计 sink）。"""
    meta: ExtensionMeta

    def handle(
        self,
        events: "list[DomainEvent]",
        ctx: TenantContext,
        *,
        timeout_ms: int = 5_000,
    ) -> "SinkAck": ...                       # 至少一次语义，按 event_id 幂等


# ⑧ execution.backends —— 执行后端（07 执行阶段落点）
@runtime_checkable
class ExecutionBackend(Protocol):
    """执行后端（`execution.backends`，通道 L3；出口控制 B4 与审批语义 B5 不随后端走；v1 仅 Docker）。"""
    meta: ExtensionMeta

    def acquire(
        self,
        spec: "SandboxSpec",              # 镜像/配额/出口白名单（B4 硬编码项不可覆盖）
        ctx: TenantContext,
        *,
        timeout_ms: int = 60_000,
    ) -> "SandboxLease": ...

    def run(
        self,
        lease: "SandboxLease",
        action: "CodeAction",             # executionMode=code 的沙箱代码行动类
        ctx: TenantContext,
        *,
        timeout_ms: int = 120_000,
    ) -> "ExecutionResult": ...

    def release(
        self,
        lease: "SandboxLease",
        ctx: TenantContext,
        *,
        timeout_ms: int = 5_000,
    ) -> None: ...                        # 取消清单第 3 步：强制释放不等优雅回收（§2.4）
```

补充三点：

1. **注册面**：L3 通道的注册函数族（`register_tool / register_gate / register_context_provider / on(event)`，对标 pi extensions）即上述 Protocol 的注册面，由 A3 扩展点分发器提供，随 M3 实现与签名一并冻结；
2. **超时与预算的关系**：`timeout_ms` 是单调用上限，仍受 A4 四维预算（token/步数/时长/成本）总量约束——任一维度耗尽即终止，不认模型自述；
3. **异步形态**：M3 全部为同步调用签名；`event.sinks` 与长时 `execution.backends.run` 在 M4+ 如需异步化，以返回 `Awaitable[...]` 的子协议扩展（向后兼容，不算破坏性变更）。

## 5. 灰区机制的裁决理由（三例争议项）

1. **规划为什么不全下放**：策略（怎么规划）下放，但「Plan 是本体实例、过三层校验、可回滚版本化」这个**形态**是内核的——否则漂移检测（从 ABox 重读计划）与恢复（对账已执行步）失去载体（T2/T3）。
2. **推理引擎为什么只开 L3**：引擎替换是低频、深集成、影响全平台语义正确性的变更，四通道里只有 L3（随版本发布、平台复核）担得起；L2 市场卖「规则包」（数据）而不卖「引擎」（代码）（T1）。
3. **记忆策略为什么留隐私门禁**：L2→L3 升级涉及他人数据共享，治理属平台责任；策略（阈值/权重）市场化，**门禁（谁能批）平台化**（T1/T4）——与治理档位三档的分工一致。

## 6. 参照对照（DSec / Hermes 机制 → 本平台裁决）

| 参照机制 | 来源 | 本平台裁决 |
| ---- | ---- | ---- |
| agent sandbox + worker container 双组件、状态单一事实源、重连续跑 | DSec V4.1 | C1 写回跨库协议同构：ABox（执行状态主本）+ PG（账本）；内核可抢占可重启 |
| 可组合环境层（base/workspace/toolkit 版本化叠加） | DSec §5.1 | 能力层栈：内核→平台标准层→租户层→任务级 toolkit；装载时 pin 版本（07 §7.5） |
| 默认拒绝出口（eBPF 白名单、AppArmor 对 root 生效） | DSec §6.5 | B4：v1 容器无网+宿主白名单代理（降级承诺）；权限与进程身份解耦 |
| pack_diff 增量快照复用环境 | DSec | 计划模板库的固化通道（已验证计划→模板）+ L2 包的环境快照工件（v2 评估） |
| 7 种终端后端（local/Docker/SSH/Modal/Daytona/Vercel） | Hermes | `execution.backends` L3 扩展点的候选实现集（v2 按任务选型） |
| 学习循环（自动技能创建/自改进） | Hermes | **受控吸收**：经验沉淀走「记忆候选→审核→技能上架」（候选非成品宪法）；自动直通上架禁止，solo 档自动通过须留痕可回滚（08 §2.4） |
| MCP 双向（client+server） | Hermes | 已有：网关双向（出口+接入） |
| 30+ 消息平台网关、FTS5 会话检索 | Hermes | 不进内核：渠道接入走 L0/L2（event.sinks/通道适配），会话检索用平台记忆服务替代（避免记忆孤岛，研究整理 04 §4） |

## 7. 反腐化保障（内核如何不随时间烂掉）

1. **CI 静态契约**：内核包 import 白名单（仅 stdlib+pydantic+domain+ports）；八扩展点 Protocol 签名变更 = 破坏性变更须锚点评审；
2. **行为符合性夹具**：能力上架门禁跑本体公理自动生成的正反例，验证包语义与标注一致（07 §7.4 铁律 2）——**夹具生成器本身是内核资产**；
3. **内核不改测试**：A/B/C 十二项机制各有「绕过即失败」的负向测试（如：伪造 externally_verified、包 gate 尝试替换基线、插件自取租户上下文）进 CI 常驻；
4. **无语义标注不上架**：所有通道能力必须绑定本体元素（行动类/规则类/概念类），机器可验证——这是「本体为骨架」在能力层的落点。

## 8. 落地与里程碑

| 项 | 里程碑 | 说明 |
| ---- | ---- | ---- |
| 内核骨架（A1~A4 + B1/B3 + C2/C3）+ builtin 适配器 | M3 | 随对话编排交付；扩展点分发器先支持 L3（进程内注册） |
| B2 判据求值 + 投影 | M3 | 重型本体优化 §5.1 同步交付（PoC⑥） |
| C1 写回跨库协议完整版 | M4 | 随业务回写；此前 M3 用 PG 单账本简化版 |
| L0/L1 通道治理 | M2 起已有 | 工具/MCP 门禁、技能上架（Skills 篇现行设计） |
| L2 能力包 + 符合性夹具 + 出口控制机制就绪 | M5 | **市场开放前置门禁 = 白名单代理机制就绪**（07 §7.5） |
| L3 全扩展点 API 稳定化 | M5 | Protocol 冻结评审 |

## 9. 验收标准

- [ ] 内核包零能力 import（CI import-linter 断言）+ 十二项机制负向测试全绿；
- [ ] 八扩展点各有至少一个 L3 内置实现与一个测试桩（含 gates「只增不替」合成顺序断言）；
- [ ] 信任级注入测试：agent_attested 事实参与判据求值被 B2 拒绝；外部回执到达后判据转可求值；
- [ ] 出口控制演练：L0 容器出网被拒、白名单代理放行、权限判定不随容器内 uid 变化；
- [ ] 能力包上下架不影响运行中任务（装载 pin 版本验证）；
- [ ] Hermes 式「自动技能创建」在 solo 档走留痕通道、enterprise 档走人工审核（档位联动测试）。

## 10. 待办与开放问题

- [x] ~~八扩展点 Protocol 的完整签名定稿（随 M3 实现，冻结前标 draft）~~（2026-09-26 设计定稿：draft v0.1 签名已入 §4.1——九 Protocol 类覆盖八行扩展点，含 TenantContext 注入、timeout_ms 上限与三处详细 docstring；M3 随内核实现冻结，冻结后变更走 §7.1 破坏性变更评审）；
- [ ] B4 白名单代理的具体实现选型（宿主 sidecar vs 网关代理路由）；
- [ ] 子代理 AgentSlot 协议与本文 A3 分发器的合并设计（避免两套插槽概念）；
- [ ] L2 能力包清单 schema（server.json 扩展字段）与 Skills 篇 §四通道细则的联合定稿；
- [ ] 学习循环（Hermes 模式）的沉淀通道细化：记忆候选→技能的转换工单字段。

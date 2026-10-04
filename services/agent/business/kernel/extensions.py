"""八扩展点 Protocol 契约面（docs/Agent/02 §4.1 draft，随 M3 内核骨架实现冻结）。

落点说明：02 §4.1 草案落 ``agent_runtime/kernel/extensions.py``，按本仓模块轴映射为
``services/agent/business/kernel/extensions.py``（02 §2.3 内核单包纪律同构）。
与 §4.1 草案的两处偏差（冻结前微调，§4.1「随 M3 实现冻结」条款内）：

1. **异步形态**：M3 签名定为 ``async def``——锚点 §3.4 依赖倒置与本仓唯一既有端口
   ModelPort（services/platform/ports/model_port.py）同为 async，standards/01 §2.5
   异步优先；§4.1 注 3 的同步草案让位于仓库既有事实，冻结后变更仍走破坏性评审。
2. **ToolBinding 命名**：`tools.bindings` 扩展点协议命名 ``ToolPort``，对齐
   standards/01 §2.2 端口命名纪律（XxxPort）与 ModelPort 同构。

公共纪律（§4.1）：① 每方法必带 ``ctx: TenantContext``（C3 平台侧注入）；② 每方法必带
``timeout_ms`` 关键字（建议上限，硬上限由内核按 A4 预算钳制，超时按失败分支处理）；
③ 每实现携带 ``meta: ExtensionMeta``（无语义标注不上架，§7.4）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from services.agent.domain.model.kernel_actions import (
    ActionDecision,
    ApprovalTicket,
    CodeAction,
    ExecutionResult,
    SandboxLease,
    SandboxSpec,
    StepResult,
    ToolCall,
    ToolResult,
)
from services.agent.domain.model.kernel_context import (
    ContextBlock,
    ExtensionMeta,
    KernelEvent,
    SinkAck,
    StepRef,
    TaskRef,
    TenantContext,
)
from services.agent.domain.model.kernel_gates import GateReport, ValidationReport
from services.agent.domain.model.kernel_planning import (
    ConsolidationDecision,
    MemoryCandidate,
    PlanCandidate,
    PlanMode,
    ReasoningRequest,
    ReasoningResult,
    RecallItem,
)


# ① context.providers —— 上下文供给器（通道 L2/L3；组装器骨架/标界/预算归内核 A1/B3）
@runtime_checkable
class ContextProvider(Protocol):
    """上下文供给器：外部数据源/专用召回 → ContextBlock，交内核组装器裁剪。

    契约（违反即拒载）：产出一律不可信外部输入（B3）；budget_tokens 为绝对上限；
    幂等（漂移检测会重放）；禁直写存储、禁发起工具调用。
    """

    meta: ExtensionMeta

    async def provide(
        self,
        task: TaskRef,
        step: StepRef | None,
        ctx: TenantContext,
        *,
        budget_tokens: int,
        timeout_ms: int = 3_000,
    ) -> ContextBlock: ...  # pragma: no cover — Protocol 方法无实现


# ② planning.strategies —— 规划策略（通道 L1/L2；规划阶段与三层校验归内核 T2/T3）
@runtime_checkable
class PlanningStrategy(Protocol):
    """规划策略：产计划图候选（仍是本体实例候选，过内核三层校验）。"""

    meta: ExtensionMeta

    async def plan(
        self,
        task: TaskRef,
        ctx: TenantContext,
        *,
        mode: PlanMode = PlanMode.TEMPLATE,
        timeout_ms: int = 10_000,
    ) -> PlanCandidate: ...  # pragma: no cover — Protocol 方法无实现


# ③ gates.pre —— 增量前置门禁（通道 L2，上架默认禁用待复核；基线 B1 归内核、只增不替）
@runtime_checkable
class PreGate(Protocol):
    """前置门禁：只能新增违例，不得覆盖/放行基线 GateReport（铁律 1）；确定性（禁随机/LLM）。"""

    meta: ExtensionMeta

    async def check(
        self,
        decision: ActionDecision,
        ctx: TenantContext,
        *,
        timeout_ms: int = 100,
    ) -> GateReport: ...  # pragma: no cover — Protocol 方法无实现


# ③ gates.post —— 后验校验器（通道 L2，上架默认禁用待复核）
@runtime_checkable
class PostGate(Protocol):
    """后验校验器：对象为已执行步产物；不得修改 StepState（状态主权 T2）。"""

    meta: ExtensionMeta

    async def validate(
        self,
        result: StepResult,
        ctx: TenantContext,
        *,
        timeout_ms: int = 1_000,
    ) -> ValidationReport: ...  # pragma: no cover — Protocol 方法无实现


# ④ tools.bindings —— 工具绑定（通道 L0/L2；行动类注册表/scope 校验/审批路由归内核 B1/B5）
@runtime_checkable
class ToolPort(Protocol):
    """工具绑定端口：把本体行动类绑定到具体实现（MCP 工具/本地函数/规则执行器）。

    契约（违反即拒载）：required_scopes 声明于计划步（授权唯一依据，MCP annotations
    仅 UI 提示）；需审批动作未携有效 ApprovalTicket 一律拒绝（B5，实现不得自查自放）；
    值不经采样；ToolResult 一律不可信（B3 标界）；失败必须结构化返回，禁裸异常逃逸循环。
    """

    meta: ExtensionMeta

    async def invoke(
        self,
        call: ToolCall,
        ctx: TenantContext,
        *,
        approval: ApprovalTicket | None = None,
        timeout_ms: int = 30_000,
    ) -> ToolResult: ...  # pragma: no cover — Protocol 方法无实现


# ⑤ reasoning.engines —— 确定性推理引擎（通道 L3；路由器与分级宪法不可换，ADR-6 替换位）
@runtime_checkable
class ReasoningEngine(Protocol):
    """推理引擎实现位：引擎可换、路由器与分级宪法不可换（05 篇：调用方禁自选引擎）。"""

    meta: ExtensionMeta

    async def run(
        self,
        request: ReasoningRequest,
        ctx: TenantContext,
        *,
        timeout_ms: int = 60_000,
    ) -> ReasoningResult: ...  # pragma: no cover — Protocol 方法无实现


# ⑥ memory.policies —— 记忆策略（通道 L2/L3；四层结构与遗忘底线归平台）
@runtime_checkable
class MemoryPolicy(Protocol):
    """记忆策略：沉淀判定与召回加权；L2→L3 升级隐私门禁不外包（升级仅产工单）。"""

    meta: ExtensionMeta

    async def judge(
        self,
        candidates: list[MemoryCandidate],
        ctx: TenantContext,
        *,
        timeout_ms: int = 5_000,
    ) -> list[ConsolidationDecision]: ...  # pragma: no cover — Protocol 方法无实现

    def weight(self, item: RecallItem, ctx: TenantContext) -> float:
        """召回加权（w_layer 之上的策略项）。"""
        ...  # pragma: no cover — Protocol 方法无实现


# ⑦ event.sinks —— 事件汇（通道 L0/L2；Outbox 与审计 sink 是内核必选，不可卸载）
@runtime_checkable
class EventSink(Protocol):
    """事件汇：只做外部通知，不替代 Outbox/审计 sink；至少一次语义，按 event_id 幂等。"""

    meta: ExtensionMeta

    async def handle(
        self,
        events: list[KernelEvent],
        ctx: TenantContext,
        *,
        timeout_ms: int = 5_000,
    ) -> SinkAck: ...  # pragma: no cover — Protocol 方法无实现


# ⑧ execution.backends —— 执行后端（通道 L3；出口控制 B4 与审批语义 B5 不随后端走；v1 仅 Docker）
@runtime_checkable
class ExecutionBackend(Protocol):
    """执行后端：沙箱 acquire/run/release；出口白名单（B4 硬编码项）不可被 spec 覆盖。"""

    meta: ExtensionMeta

    async def acquire(
        self,
        spec: SandboxSpec,
        ctx: TenantContext,
        *,
        timeout_ms: int = 60_000,
    ) -> SandboxLease: ...  # pragma: no cover — Protocol 方法无实现

    async def run(
        self,
        lease: SandboxLease,
        action: CodeAction,
        ctx: TenantContext,
        *,
        timeout_ms: int = 120_000,
    ) -> ExecutionResult: ...  # pragma: no cover — Protocol 方法无实现

    async def release(
        self,
        lease: SandboxLease,
        ctx: TenantContext,
        *,
        timeout_ms: int = 5_000,
    ) -> None: ...  # pragma: no cover — Protocol 方法无实现


# ⑨ AgentSlot —— 子代理插槽（02 §4.2，agent.slots，通道 L3 内核内置；v1 仅冻结注册面，
#    消费随 M3-2 chat_orchestrator 接通）
@runtime_checkable
class AgentSlot(Protocol):
    """子代理插槽：结果只经 Artifact 回传（结构化 JSON，禁自由文本摘要）；窗口隔离。"""

    meta: ExtensionMeta

    async def spawn_sub(
        self,
        task: TaskRef,
        ctx: TenantContext,
        *,
        context_budget: int,
        artifact_schema: dict[str, Any],
        timeout_ms: int = 600_000,
        label: str | None = None,
        index: int | None = None,
        total: int | None = None,
    ) -> str: ...  # pragma: no cover — Protocol 方法无实现（v1 返回子 Run 句柄 id）

# label/index/total（40 篇 §8 R2，2026-10-04 批）：SUBRUN_STARTED 发射面元数据（子代理
# 显示名 / 本批并行批次序号 / 批次总量）——仅进事件载荷与回执审计，不参与裁决，缺省 None
# 向后兼容（既有实现可忽略；BuiltinAgentSlot/ChatAdapter 已同步签名）。

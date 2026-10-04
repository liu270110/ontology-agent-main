"""内核单次运行上下文（内核私有聚合）：状态/产物/账本/预算/取消协调 + 审计发射口签名。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from services.agent.business.kernel.budget import Budget, BudgetTracker
from services.agent.business.kernel.cancellation import CancellationCoordinator
from services.agent.business.kernel.ledger import KernelLedger, LedgerSink
from services.agent.business.kernel.plan import PlanProjection
from services.agent.domain.model.kernel_actions import ApprovalTicket, StepResult
from services.agent.domain.model.kernel_context import ContextBlock, TaskRef, TenantContext
from services.agent.domain.model.step_state import StepState

# 审计事件发射口签名（AgentKernel._emit，C2）：各阶段共用，账本统一校验归属
Emit = Callable[[KernelLedger, TenantContext, UUID, str, dict[str, Any]], None]


class RunContext:
    """单次运行的可变执行上下文（内核私有）：各阶段经它读写状态与产物。"""

    def __init__(
        self,
        task: TaskRef,
        ctx: TenantContext,
        budget: Budget,
        *,
        clock: Callable[[], float],
        approvals: tuple[ApprovalTicket, ...],
        ledger_sink: LedgerSink | None = None,
    ) -> None:
        self.task = task
        self.ctx = ctx
        self.ledger = KernelLedger(tenant_id=ctx.tenant_id, trace_id=ctx.trace_id, sink=ledger_sink)
        self.tracker = BudgetTracker(budget, clock=clock)
        self.coordinator = CancellationCoordinator(self.ledger)
        self.approvals = approvals
        self.states: dict[int, StepState] = {}
        self.results: dict[int, StepResult] = {}
        # M4.5-B 前缀稳定断言状态（每 Run 独立，grounding 组装器读写）：首组装冻结前缀
        # 基线（canonical sha256）与逐块哈希（漂移定位）；None/空=尚未组装（12 §2 批次 B）。
        self.prefix_fingerprint: str | None = None
        self.prefix_block_hashes: tuple[tuple[str, str], ...] = ()
        # M4.5-A：运行中输入面的组装面（docs/Agent/12 §1.1）——grounding 产出后由 loop 回填；
        # 段边界 drain 的 steer/inject 文本包装为 ContextBlock（source="user_steer"，B3 标界
        # agent_attested，tier=3 易变尾）追加于此，供后续组装消费方就近读取。
        self.context_blocks: tuple[ContextBlock, ...] = ()
        # 40 篇 R4（2026-10-04）：计划投影（规划阶段产出后填充；规划前 None=零发射）。
        # items=内核执行步整表，步推进时由 loop/tool_dispatch 经 begin/finish 推进并发快照。
        self.plan: PlanProjection | None = None

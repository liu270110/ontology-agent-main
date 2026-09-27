"""内核单次运行上下文（内核私有聚合）：状态/产物/账本/预算/取消协调 + 审计发射口签名。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from services.agent.business.kernel.budget import Budget, BudgetTracker
from services.agent.business.kernel.cancellation import CancellationCoordinator
from services.agent.business.kernel.ledger import KernelLedger
from services.agent.domain.model.kernel_actions import ApprovalTicket, StepResult
from services.agent.domain.model.kernel_context import TaskRef, TenantContext
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
    ) -> None:
        self.task = task
        self.ctx = ctx
        self.ledger = KernelLedger(tenant_id=ctx.tenant_id, trace_id=ctx.trace_id)
        self.tracker = BudgetTracker(budget, clock=clock)
        self.coordinator = CancellationCoordinator(self.ledger)
        self.approvals = approvals
        self.states: dict[int, StepState] = {}
        self.results: dict[int, StepResult] = {}

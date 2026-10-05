"""内核单次运行上下文（内核私有聚合）：状态/产物/账本/预算/取消协调 + 审计发射口签名。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ProgressHeartbeat:
    """K12-a 心跳快照（值对象，docs/Agent/13 §18）：记账点观测到的进展面。

    tokens=预算检查口径累计（tracker.tokens_effective，真实回执+锚定缩放估算）；
    tool_results=已完成步结果累计（rc.results 条数——门禁拒绝步无结果不计入）。
    两指纹较上次记账零增长=该记账区间无实质进展（K12-b 停滞判据，loop_guard.StuckWatch）。
    """

    step_seq: int  # 记账点步号（并行段=段首步）
    at: float  # 记账时刻（tracker.elapsed_s 同源单调时钟，Run 级）
    tokens: int
    tool_results: int

    def as_payload(self) -> dict[str, Any]:
        """进展指纹的事件载荷形态（kernel.run_stuck payload 用；纯计数不含参数原文）。"""
        return {
            "step_seq": self.step_seq,
            "at": round(self.at, 6),
            "tokens": self.tokens,
            "tool_results": self.tool_results,
        }


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
        # K11-b 步间压缩风暴帽计数（docs/Agent/13 §17）：本 Run 步间水位复判已实际触发的
        # 压缩次数（Run 级，达 Settings.kernel_watermark_recheck_max 即不再步间压缩）。
        self.recheck_compactions = 0
        # K11-b capped 事件去重：达帽警告每 Run 只发一次（首达帽置位并发事件，后续边界静默）。
        self.recheck_capped_emitted = False
        # K12-a 心跳记账（docs/Agent/13 §18）：最近一次记账点进展快照（None=尚无记账）。
        # 由 loop_guard 记账点经 StuckWatch.beat 刷新——串行步循环与并行段边界同源。
        self.last_progress: ProgressHeartbeat | None = None
        # K12-b STUCK 观测态（只观测不迁移：Run/Step 状态机枚举零改动，04 §3 状态主权不变）：
        # stall_count=连续无实质进展记账次数；stuck_emitted=当前 stuck 期已发
        # kernel.run_stuck（照 recheck_capped_emitted 每期一次去重先例）；有实质进展即
        # 解除（计数清零、标记复位）可再次置位。is_stuck=外部只读观测面。
        self.stall_count = 0
        self.stuck_emitted = False

    @property
    def is_stuck(self) -> bool:
        """Run 级 stuck 观测标记（K12-b 只读面）：当前处于 stuck 期（事件已发、未因进展解除）。

        只观测不迁移：状态机枚举与迁移表零改动，前端/治理面据 kernel.run_stuck 事件
        与本标记展示「疑似卡死」，不改变任何状态迁移语义（A-6 原文红线）。
        """
        return self.stuck_emitted

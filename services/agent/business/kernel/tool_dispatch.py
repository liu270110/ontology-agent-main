"""B-① Run 内并行工具调度器（docs/Agent/10 §3/§4）：分段与段执行，A1 执行段增补。

分段（:func:`segment_steps`）：连续步并入同一并行段当且仅当全部满足——① 规划显式声明
``parallelizable``；② ``execution_mode`` 为 READ（EXTERNAL_WRITE/CODE 恒串行，天然
barrier；③ 审批需求被 ② 蕴含）；④ 段大小 ≤ 并发度上限。不满足任一条件的步单步成段
（loop 走现状串行路径，零行为差异）。段间严格按序：前一段全部结算后才进下一段。

段执行不变式（§4）：
- 门禁先行：段内每步顺序过 ``gate``；被拒步 FAILED 留在段外（单步拒绝不炸整段）；
- 池执行：``asyncio.Semaphore`` 包住每步既有完整执行管线（复用 ExecutionStage，不复制
  其逻辑）；每步独立收敛，一步异常=该步结构化失败、其余步不受影响（禁异常逃逸）；
- 顺序确定性：任务按声明序创建＋信号量 FIFO ⇒ open_tool_call 按声明序落；close_tool_call
  各自完成时落；段全部完成后按声明序逐步跑 observation 与步数记账（与串行同序）；
- 取消：invoke 任务仍经 coordinator.track_tool_task 挂清单；段任务在取消时收尾后才上抛，
  未闭合调用由清单以取消错误闭合（§2.4，assert_no_open_calls 零悬挂）；
- 审计：kernel.group_started / kernel.group_finished（transport-only，不含工具输出正文）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence

from services.agent.business.kernel.execution import ExecutionStage
from services.agent.business.kernel.loop_guard import LoopGuard, StuckWatch, register_step
from services.agent.business.kernel.plan import KERNEL_PLAN_UPDATED, plan_updated_data
from services.agent.business.kernel.run_context import Emit, RunContext
from services.agent.domain.model.kernel_actions import ExecutionMode
from services.agent.domain.model.kernel_planning import PlanCandidate, PlanStep
from services.agent.domain.model.step_state import LoopStage, StepStatus
from services.platform.errors import ErrorCode

logger = logging.getLogger(__name__)

# 内核阶段函数签名（loop 注入，状态主权仍在内核 T2）
GateFn = Callable[[RunContext, PlanCandidate, PlanStep], Awaitable[None]]
ObserveFn = Callable[[RunContext, PlanStep], Awaitable[None]]


def segment_steps(steps: Sequence[PlanStep], *, parallelism: int) -> list[tuple[PlanStep, ...]]:
    """把计划步切成调度段（确定性纯函数）：可并行 READ 步的连续游程入段，段长 ≤ 上限。

    其余步（非 parallelizable / 非 READ / 段满）单步成段=天然 barrier；返回顺序=声明顺序。
    """
    groups: list[tuple[PlanStep, ...]] = []
    current: list[PlanStep] = []
    for step in steps:
        if step.parallelizable and step.execution_mode is ExecutionMode.READ and len(current) < parallelism:
            current.append(step)
            continue
        if current:
            groups.append(tuple(current))
            current = []
        groups.append((step,))
    if current:
        groups.append(tuple(current))
    return groups


class ToolGroupDispatcher:
    """并行段执行器：门禁先行 → 有界并发池复用单步管线 → 声明序收口（§4 不变式载体）。"""

    def __init__(self, execution_stage: ExecutionStage, emit: Emit, *, gate: GateFn, observe: ObserveFn) -> None:
        self._execution_stage = execution_stage
        self._emit = emit
        self._gate = gate
        self._observe = observe

    async def run_group(
        self,
        rc: RunContext,
        candidate: PlanCandidate,
        steps: tuple[PlanStep, ...],
        *,
        parallelism: int,
        loop_guard: LoopGuard,
        stuck_watch: StuckWatch,
    ) -> None:
        """执行一个并行调度段：被拒步留段外，入池步并发跑既有管线，完成后声明序收口。

        A-1 循环记账（docs/Agent/13 §2 K1-a）：**池执行前**按声明序对段内每步逐步记账
        （与串行「步前记账」同位——被拒/失败的重复动作同样计入循环形态）；同签名连续
        重复第 1 次注入 kernel.loop_nudge 软警告，达阈值抛 LoopDetectedError 硬终止
        （发生在池启动前 ⇒ 整段零工具调用，比串行逐步拦截更保守）。

        K12-a/b 心跳记账（docs/Agent/13 §18）：**段前记账一次**（段为一个调度单元——
        若按池前逐步记账，同段多步会在池产出任何结果前被误计为连续停滞；段边界与
        串行步边界同位同源，进展指纹/停滞计数语义一致）。
        """
        ctx = rc.ctx
        if steps:
            stuck_watch.beat(rc, steps[0].seq)
        for step in steps:
            register_step(rc, loop_guard, step, emit=self._emit)
        admitted: list[PlanStep] = []
        for step in steps:  # 门禁先行（顺序过 gate，事件/快照与串行同序）
            await self._gate(rc, candidate, step)
            if rc.states[step.seq].status is StepStatus.GATED:
                admitted.append(step)
        concurrency = max(1, min(parallelism, len(admitted)))
        # R4 计划推进（40 篇 §8）：入池步整批 begin 后发一次快照（in_progress；批内合并
        # =低频整表快照语义，40 篇 §4.1 PLAN_UPDATED 不节流也无高频风险）
        plan = rc.plan
        began = [s.seq for s in admitted if plan is not None and plan.begin(s.seq)]
        if began:
            self._emit(
                rc.ledger,
                ctx,
                rc.task.run_id,
                KERNEL_PLAN_UPDATED,
                plan_updated_data(plan, str(rc.task.run_id)),  # type: ignore[arg-type]  # began 非空蕴含 plan 非空
            )
        self._emit(
            rc.ledger,
            ctx,
            rc.task.run_id,
            "kernel.group_started",
            {
                "step_seqs": [s.seq for s in steps],
                "admitted_seqs": [s.seq for s in admitted],
                "concurrency": concurrency,
                "stage": str(LoopStage.EXECUTION),
            },
        )
        if admitted:
            semaphore = asyncio.Semaphore(concurrency)

            async def _run_one(step: PlanStep) -> None:
                async with semaphore:
                    try:
                        await self._execution_stage.run(rc, step)  # 既有单步完整管线（B3/C3/截断/spill/记账）
                    except asyncio.CancelledError:
                        raise  # 取消传播：在途调用由取消清单以取消错误闭合（§2.4 步骤 2）
                    except Exception as exc:  # 一步异常=该步结构化失败，其余步不受影响
                        self._structure_step_failure(rc, step, exc)

            # 声明序创建 ⇒ 各任务首片（至 open_tool_call）按声明序执行 ⇒ 落账序确定
            tasks = [asyncio.create_task(_run_one(s), name=f"kernel.group[{s.seq}]") for s in admitted]
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                for t in tasks:
                    t.cancel()  # 父任务取消不传播到子任务：显式取消在途步，收尾时间才有界（对齐 §2.4 清单 5s 纪律）
                await asyncio.gather(*tasks, return_exceptions=True)  # 段任务走完取消路径再上抛（零悬挂）
                raise
        finished = False
        for step in steps:  # 声明序收口：observation＋步数记账（与串行执行完全一致的顺序）
            if rc.states[step.seq].status is StepStatus.EXECUTING:
                await self._observe(rc, step)
            if plan is not None and plan.finish(step.seq):  # R4：步终态（validated/failed）→ completed
                finished = True
            rc.tracker.add_step()
        if finished:  # R4：整批终态合并为一发快照（零变化零事件）
            self._emit(
                rc.ledger,
                ctx,
                rc.task.run_id,
                KERNEL_PLAN_UPDATED,
                plan_updated_data(plan, str(rc.task.run_id)),  # type: ignore[arg-type]  # finished 蕴含 plan 非空
            )
        self._emit(
            rc.ledger,
            ctx,
            rc.task.run_id,
            "kernel.group_finished",
            {
                "step_seqs": [s.seq for s in steps],
                "statuses": {str(s.seq): rc.states[s.seq].status.value for s in steps},
                "concurrency": concurrency,
                "stage": str(LoopStage.OBSERVATION),
            },
        )

    @staticmethod
    def _structure_step_failure(rc: RunContext, step: PlanStep, exc: Exception) -> None:
        """池内单步异常收敛：状态落 FAILED＋该步在途未闭合调用兜底闭合（零悬挂，§4 不变式 b）。"""
        logger.warning("并行段步 %s 执行异常转义: %s", step.seq, exc)
        state = rc.states[step.seq]
        if state.status is StepStatus.EXECUTING:
            state.transition(StepStatus.FAILED, stage=LoopStage.EXECUTION)
            state.error = f"{int(ErrorCode.INTERNAL_ERROR)} 段内步执行异常: {type(exc).__name__}"
            rc.ledger.record_step(state)
        for record in rc.ledger.tool_calls:  # 正常管线已闭合；此处只兜底异常逃逸路径
            if record.step_seq == step.seq and not record.closed:
                rc.ledger.close_tool_call(record.call_id, error_code=int(ErrorCode.INTERNAL_ERROR))

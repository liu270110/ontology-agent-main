"""A1 七阶段主循环 + A4 预算终止 + 取消完整性挂接（docs/Agent/02 §2 A1/A4、§2.4）。

七阶段（LoopStage 枚举，术语对照 02 §2 A1）：
① grounding 感知＝装载（Grounding+确认）→ ② retrieval 检索＝上下文组装（绝对预算裁剪）
→ ③ planning 规划（策略/模型回退，三层校验）→ ④ gate 门禁（基线 B1 先、包 gate 后）
→ ⑤ execution 执行（tools.bindings / execution.backends，B5 审批路由；见 execution.py；
   B-① 增补：parallelizable READ 步按段并行调度，见 tool_dispatch.py）
→ ⑥ observation 观察（后验 gates.post＋漂移对账＋写回）
→ ⑦ settlement 沉淀（闭环落账：assert_no_open_calls＋事件汇＋B2 判据求值）。

终止语义（A4）：终止只认判据求值与预算耗尽，不认模型自述；预算（token/步数/时长）
任一耗尽即优雅终止并产出终态 StepState；时长维另有 ``asyncio.timeout`` 硬兜底，
中断路径统一走取消清单（§2.4，资源释放先行）——不可卡死、终态可追溯。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.compaction import CompactionTrigger, estimate_tokens
from services.agent.business.kernel.criteria import CriterionEvaluator
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.errors import BudgetExhaustedError, KernelContractError, KernelError
from services.agent.business.kernel.execution import ExecutionStage
from services.agent.business.kernel.gate_baseline import BaselineGate, canonical_param_hash
from services.agent.business.kernel.grounding import ContextAssemblyStage
from services.agent.business.kernel.inbox import InboxItem, KernelInbox
from services.agent.business.kernel.ledger import KernelLedger, LedgerSink
from services.agent.business.kernel.loop_guard import (
    DEFAULT_LOOP_ABORT_THRESHOLD,
    LoopGuard,
    register_step,
)
from services.agent.business.kernel.plan import KERNEL_PLAN_UPDATED, PlanProjection, plan_updated_data
from services.agent.business.kernel.run_context import RunContext
from services.agent.business.kernel.spill import SpillStore
from services.agent.business.kernel.subagent import ParentBindable
from services.agent.business.kernel.tool_dispatch import ToolGroupDispatcher, segment_steps
from services.agent.domain.model.kernel_actions import ActionDecision, ApprovalTicket, ExecutionMode
from services.agent.domain.model.kernel_context import ContextBlock, KernelEvent, TaskRef, TenantContext, TrustLevel
from services.agent.domain.model.kernel_gates import GateFinding, GateReport, GateVerdict, RunOutcome
from services.agent.domain.model.kernel_planning import PlanCandidate, PlanStep
from services.agent.domain.model.step_state import LoopStage, StepState, StepStatus
from services.agent.domain.model.task import RunStatus
from services.platform.config import get_settings
from services.platform.errors import ErrorCode

logger = logging.getLogger(__name__)

# 模型回退规划的结构化产物 Schema（推理分级宪法：输出必须过确定性校验才可用）
_PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["steps"],
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["seq", "action_iri"],
                "properties": {
                    "seq": {"type": "integer"},
                    "action_iri": {"type": "string"},
                    "execution_mode": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        }
    },
}


def _err(code: ErrorCode, message: str) -> str:
    return f"{int(code)} {message}"


class AgentKernel:
    """自研 Agent 内核（v1）：能力经八扩展点分发器注册注入，内核只认 Protocol（A3）。"""

    def __init__(
        self,
        dispatcher: ExtensionDispatcher,
        *,
        clock: Callable[[], float] = time.monotonic,
        tool_timeout_s: float | None = None,
        spill_store: SpillStore | None = None,
        tool_parallelism: int | None = None,
        loop_abort_threshold: int | None = None,
        watermark_recheck_max: int | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._baseline = BaselineGate()
        # E-4 K1-c：判据投影端口经分发器注入（内核禁直连 pyshacl，A3 依赖倒置）
        self._evaluator = CriterionEvaluator(projection=dispatcher.criterion_projection)
        self._clock = clock
        # A-1 循环检测两段式（docs/Agent/13 §2 K1-a）：阈值显式注入优先，缺省=常量 2
        self._loop_abort_threshold = (
            loop_abort_threshold if loop_abort_threshold is not None else DEFAULT_LOOP_ABORT_THRESHOLD
        )
        # K11-b 步间压缩风暴帽（docs/Agent/13 §17）：每 Run 步间压缩触发次数上限，
        # 显式注入优先，缺省读 Settings（D2 纪律同 loop_abort_threshold）
        self._watermark_recheck_max = (
            watermark_recheck_max if watermark_recheck_max is not None else get_settings().kernel_watermark_recheck_max
        )
        # B-① 并行段并发度：显式注入优先，缺省读 Settings（T6 唯一事实源；=1 退化为串行）
        self._tool_parallelism = (
            tool_parallelism if tool_parallelism is not None else get_settings().kernel_tool_parallelism
        )
        self._last_ledger: KernelLedger | None = None
        self._last_run_context: RunContext | None = None  # M4.5-A：验收/验收测试取组装面（验收口径=账本事件）
        # 阶段执行器（内核私有；依赖注入同一分发器，禁直连能力实现）
        self._context_stage = ContextAssemblyStage(dispatcher, self._emit)
        # B-③ 批（docs/Agent/10 §8.2）：tool_timeout_s 直传执行阶段——显式注入优先
        # （测试与组合根直传通道，D2/F-4 同款纪律），缺省 None 由 ExecutionStage
        # 运行期读 Settings（配置层唯一事实源）。
        self._execution_stage = ExecutionStage(
            dispatcher, self._emit, tool_timeout_s=tool_timeout_s, spill_store=spill_store
        )
        self._tool_dispatch = ToolGroupDispatcher(
            self._execution_stage,
            self._emit,
            gate=self._stage_gate,
            observe=self._stage_observation,
        )

    @property
    def last_ledger(self) -> KernelLedger | None:
        """最近一次运行的账本（C2 v1 内存形态；M4 切 PG 台账，取消/审计验收取数口）。"""
        return self._last_ledger

    @property
    def last_run_context(self) -> RunContext | None:
        """最近一次运行的执行上下文（M4.5-A：运行中输入面组装面 rc.context_blocks 的取数口）。"""
        return self._last_run_context

    # ── 配置层解析（B-③ 批，docs/Agent/10 §8.2：规划/门禁/事件汇+排水无显式构造参数，
    # 运行期统一读 Settings；数值默认=原模块级常量 10/1/5 逐位一致）───────────────
    def _resolve_planning_timeout_s(self) -> float:
        return get_settings().kernel_planning_timeout_s

    def _resolve_gate_timeout_s(self) -> float:
        return get_settings().kernel_gate_timeout_s

    def _resolve_sink_timeout_s(self) -> float:
        return get_settings().kernel_sink_timeout_s

    # ── 主入口 ───────────────────────────────────────────────────────────
    async def run(
        self,
        task: TaskRef,
        ctx: TenantContext,
        *,
        budget: Budget,
        approvals: tuple[ApprovalTicket, ...] = (),
        ledger_sink: LedgerSink | None = None,
        spill_store: SpillStore | None = None,
        inbox: KernelInbox | None = None,
        control_gate: Callable[[], str | None] | None = None,
        resumed_validated: tuple[dict[str, Any], ...] = (),
    ) -> RunOutcome:
        """执行一次 Run（七阶段）。取消传播下状态一致：终态经账本可追溯后重抛取消。

        ``ledger_sink``（C1 PG 台账投影，组合根注入）：内核锚点事件在入账同时异步投影
        到持久层，终态前排水（先落库后终态的可追溯口径）；投影失败不阻断运行。

        M4.5-A 运行中输入面（docs/Agent/12-M4.5运行中输入面与模型韧性设计（主仓本地）§1）：

        - ``inbox``（每 Run 一个 KernelInbox，组合根经运行注册表挂入）：B-① 分段驱动的
          **段边界**先 drain_steerable()——steer/inject 文本包装为 ContextBlock（B3 标界
          agent_attested）追加进运行组装面（rc.context_blocks），followup 留存步中不生效、
          终态后由编排器 take_followups()；每笔 submit/drain 落账本事件
          kernel.inbox_spliced / kernel.inbox_drained（payload：kind/source/seq/text）。
        - ``control_gate``（紧急停止闸门探针，同步 callable）：**段边界**先于 drain 查询，
          返回非 None（=激活原因）即走 :meth:`_finalize_interrupted` 优雅中断
          （reason_code=4104 ESTOP_ACTIVE，``run_checklist=False``）。
          **estop 与 cancel 语义区别（钉死，docs/Agent/12 §1.2 + 2026-10-04 真 vLLM 实测裁决）**：
          estop=**暂停闸（pause gate），不是删除**——激活期新 Run 于认领/首个段边界被 4104
          拒绝（零执行），解除后 retryable 的任务经既有重试监督自然恢复；cancel 才是杀在途
          （取消清单 4 步：子 Run 级联/在途工具中止/租约强制释放/工作区标记）。本闸门只挡
          新段调度，不打断任何在途调用，在途工具自然收敛后运行落终态（A-7「只挡新工作」）。
        - ``resumed_validated``（P-4 resume 计划对账锚点：seq/action_iri/param_hash 全等
          匹配且 execution_mode=READ 才跳过）：规划完成后对账，命中步走 planned→validated
          特批迁移（kernel.step_resumed_validated，resumed=true，不产生消息行）；存在偏差
          （计划变更/不匹配）则**全量重放**并落 kernel.resume_mismatch 审计事件。
        """
        if not ctx.trace_id:
            raise KernelContractError("TenantContext.trace_id 为空，拒绝运行（C2 可追溯底线）")
        rc = RunContext(task, ctx, budget, clock=self._clock, approvals=approvals, ledger_sink=ledger_sink)
        loop_guard = LoopGuard(abort_threshold=self._loop_abort_threshold)  # A-1：每 Run 独立记账（状态不跨 Run）
        self._last_ledger = rc.ledger

        def emit_budget_anchor(payload: dict[str, Any]) -> None:
            # M4.5-B（docs/Agent/12 §2 批次 B）：锚定系数首立/显著变化 → 账本
            # kernel.budget_anchor（ratio/estimated/real）；统一走 _emit（账本+钩子广播同源）。
            self._emit(rc.ledger, ctx, task.run_id, "kernel.budget_anchor", payload)

        rc.tracker.anchor_sink = emit_budget_anchor  # 锚定事件接线（真实 usage 到达时由记账器上抛）
        self._last_run_context = rc
        self._execution_stage.spill_store = spill_store  # per-run spill 注入（02 §11.2-11）
        if inbox is not None:  # M4.5-A：splice 审计挂账本发射口（注册窗口内 submit 由 attach 补记）

            def _inbox_auditor(event_type: str, payload: dict[str, Any], _rc: RunContext = rc) -> None:
                self._emit(_rc.ledger, _rc.ctx, _rc.task.run_id, event_type, payload)

            inbox.attach_auditor(_inbox_auditor)
        slot = self._dispatcher.agent_slot()  # agent.slots（02 §4.2）：可绑定实现挂父作用域（分账+级联）
        if isinstance(slot, ParentBindable):
            slot.bind_parent(task.run_id, rc.tracker, rc.coordinator, emit=self._slot_emitter(rc))
        try:
            # 时长维硬兜底（A4）；未设时长预算按 24h 封顶。超限中断统一走取消清单收敛。
            hard_cap = budget.duration_s if budget.duration_s is not None else 86_400.0
            async with asyncio.timeout(hard_cap):
                blocks = await self._context_stage.run(rc)
                rc.context_blocks = blocks  # M4.5-A：组装面回填（段边界 steer/inject 追加于此）
                candidate = await self._stage_planning(rc, blocks)
                rc.states = {
                    step.seq: StepState(
                        run_id=task.run_id,
                        seq=step.seq,
                        stage=LoopStage.PLANNING,
                        status=StepStatus.PLANNED,
                        action_iri=step.action_iri,
                        budget_watermark=rc.tracker.watermark(),
                    )
                    for step in candidate.steps
                }
                for state in rc.states.values():
                    rc.ledger.record_step(state)  # 每阶段产出 StepState：计划态入账
                # P-4 resume 计划对账（§1.3）：规划完成后、段循环前——命中 READ 步特批迁移，
                # 存在偏差则全量重放（对账后的执行集=未 validated 步）
                if resumed_validated:
                    self._reconcile_resumed_anchors(rc, candidate, resumed_validated)
                execution_steps = [s for s in candidate.steps if rc.states[s.seq].status is not StepStatus.VALIDATED]
                for group in segment_steps(execution_steps, parallelism=self._tool_parallelism):
                    # M4.5-A 段边界控制面（先 estop 闸门、后 steering 拼接，次序即优先级）：
                    stop_reason = control_gate() if control_gate is not None else None
                    if stop_reason is not None:
                        return await self._finalize_interrupted(
                            rc,
                            status=RunStatus.CANCELLED,
                            reason_code=int(ErrorCode.ESTOP_ACTIVE),
                            reason=f"紧急停止生效（estop: {stop_reason}），段边界优雅中断（A-7 只挡新工作，在途不杀）",
                            run_checklist=False,  # estop≠cancel：不进取消清单，在途工具自然收敛
                        )
                    if inbox is not None:
                        self._splice_inbox_blocks(rc, inbox)
                    # K11-a 步间水位复判（docs/Agent/13 §17）：段=步边界统一挂点（串行单步段
                    # 与并行多步段同位）——组装每 Run 仅一次，运行中 steer 注入与真实消耗使
                    # 水位增长而组装级压缩门不复判；此处超水位先压缩再执行该步（K11-b 帽内）。
                    await self._recheck_watermark(rc)
                    if len(group) == 1:  # 单步段=完全现状串行路径（B-① 零行为差异面）
                        step = group[0]
                        rc.tracker.check()  # A4 检查点：步前预算断言（超限优雅终止）
                        # A-1 循环记账（docs/Agent/13 §2 K1-a）：步前逐步记账——同签名连续
                        # 重复第 1 次注入 kernel.loop_nudge 软警告，达阈值抛 LoopDetectedError
                        #（KernelError 家族 → run() 结构化终止，账本可追溯）
                        register_step(rc, loop_guard, step, emit=self._emit)
                        state = rc.states[step.seq]
                        await self._stage_gate(rc, candidate, step)
                        if state.status is StepStatus.GATED:
                            if rc.plan is not None and rc.plan.begin(step.seq):
                                self._emit_plan_snapshot(rc)  # R4：步开跑 → in_progress
                            await self._execution_stage.run(rc, step)
                        if state.status is StepStatus.EXECUTING:
                            await self._stage_observation(rc, step)
                        if rc.plan is not None and rc.plan.finish(step.seq):
                            self._emit_plan_snapshot(rc)  # R4：步终态（validated/failed）→ completed
                        rc.tracker.add_step()  # 步数预算记账（步后累计，下一步检查点生效）
                    else:  # 多步段：段前预算检查点一次，段执行器负责门禁先行+池执行+声明序收口
                        rc.tracker.check()
                        remaining = rc.tracker.remaining_steps
                        planned = len(group)
                        # 段截断至剩余步预算：尾部步不执行=与串行逐步检查点语义等价（预算耗尽后串行同样不执行它们）
                        group = group if remaining is None else group[:remaining]
                        # A-1：并行段路径同步记账（run_group 段内按声明序门禁前逐步记账）
                        await self._tool_dispatch.run_group(
                            rc, candidate, group, parallelism=self._tool_parallelism, loop_guard=loop_guard
                        )
                        if len(group) < planned:  # 尾部步被截断=计划仍有未执行步：补段边界检查点（步数已耗尽必抛）
                            rc.tracker.check()
                return await self._stage_settlement(rc, candidate)
        except BudgetExhaustedError as exc:  # 须先于 KernelError（子类）
            return await self._finalize_interrupted(
                rc,
                status=RunStatus.FAILED,
                reason_code=int(ErrorCode.RETRY_BUDGET_EXHAUSTED),
                reason=f"预算耗尽（{exc.dimension}），优雅终止（A4）",
                run_checklist=False,  # 检查点中断：无在途资源
            )
        except TimeoutError:
            return await self._finalize_interrupted(
                rc,
                status=RunStatus.TIMEOUT,
                reason_code=int(ErrorCode.RETRY_BUDGET_EXHAUSTED),
                reason="时长预算超限（asyncio.timeout 硬兜底），经取消清单收敛后终止（A4/§2.4）",
                run_checklist=True,  # 硬超时可能打断在途调用：清单先行
            )
        except KernelError as exc:  # 规划/沙箱规格等内核错误 → 结构化终止（禁异常逃逸）
            return await self._finalize_interrupted(
                rc, status=RunStatus.FAILED, reason_code=exc.code, reason=exc.message, run_checklist=True
            )
        except asyncio.CancelledError:
            report = await rc.coordinator.execute(reason="run_cancelled")  # 资源释放先行（§2.4）
            for state in rc.states.values():
                if not state.is_terminal:
                    state.cancel(reason="运行被取消（清单完毕落终态，02 §2.4）")
                rc.ledger.record_step(state)
            self._emit_plan_terminal_sweep(rc)  # R4：已开跑未终态项随取消收敛推进 completed
            rc.ledger.assert_no_open_calls()  # 清单已强制闭合：落终态即零未闭合调用
            self._emit(
                rc.ledger,
                ctx,
                task.run_id,
                "kernel.cancelled",
                {
                    "completed": list(report.completed),
                    "forced": list(report.forced),
                    "residuals": list(rc.ledger.residuals),
                },
            )
            await rc.ledger.drain_sink(timeout_s=self._resolve_sink_timeout_s())  # C1 投影排水（取消亦不丢锚点）
            raise  # 取消语义向上传播；终态与零残留已经账本可追溯

    # ── ③ 规划（策略优先、模型回退；三层校验）────────────────────────────
    async def _stage_planning(self, rc: RunContext, context: tuple[ContextBlock, ...]) -> PlanCandidate:
        strategy = self._dispatcher.planning_strategy
        if strategy is not None:
            candidate = await asyncio.wait_for(
                strategy.plan(rc.task, rc.ctx), timeout=self._resolve_planning_timeout_s()
            )
        else:
            model = self._dispatcher.model
            if model is None:
                raise KernelContractError("既无 PlanningStrategy 也无 ModelPort：规划无法产出计划（A1）")
            raw = await asyncio.wait_for(
                model.complete_structured(
                    system="你是规划器：只输出 JSON 计划（steps 数组，元素含 seq/action_iri）。",
                    user=rc.task.objective,
                    json_schema=_PLAN_JSON_SCHEMA,
                    trace_id=rc.ctx.trace_id,
                ),
                timeout=self._resolve_planning_timeout_s(),
            )
            candidate = self._candidate_from_model(raw)
        self._validate_candidate(candidate)  # 三层校验（结构/绑定/分级）
        self._emit(
            rc.ledger,
            rc.ctx,
            rc.task.run_id,
            "kernel.planned",
            {
                "strategy": candidate.strategy_name,
                "steps": [s.seq for s in candidate.steps],
                "context_blocks": len(context),
                "watermark": rc.tracker.watermark().model_dump(),
            },
        )
        # R4 计划投影（40 篇 §4.2/§8）：规划产出即建 items 整表（全 pending）并发
        # revision=1 快照（kernel.plan_updated → 转译 PLAN_UPDATED）；后续步推进增量快照。
        rc.plan = PlanProjection(candidate.steps)
        self._emit_plan_snapshot(rc)
        return candidate

    # ── M4.5-A：P-4 resume 计划对账（docs/Agent/12 §1.3）──────────────────
    def _reconcile_resumed_anchors(
        self, rc: RunContext, candidate: PlanCandidate, anchors: tuple[dict[str, Any], ...]
    ) -> None:
        """规划完成后对账：锚点 (seq, action_iri, param_hash) 全等且 execution_mode=READ
        的步走 planned→validated 特批迁移（A2 迁移表白名单条目，kernel.step_resumed_validated
        落账、resumed=true，**不产生消息行**——会话历史不变式不变）。

        偏差口径（任一命中即**全量重放**并落 kernel.resume_mismatch 审计事件）：
        锚点 seq 在新计划不存在 / action_iri 不等 / param_hash 不等（含锚点缺 hash 的
        存量事件形态——无法核验即不核验，安全侧退化）。EXTERNAL_WRITE/CODE 步即使
        三元组全等也**恒不跳**（幂等安全门），但不记偏差（正常重放走既有门禁/审批链）。
        """
        by_seq = {s.seq: s for s in candidate.steps}
        skips: list[PlanStep] = []
        deviations: list[dict[str, Any]] = []
        for anchor in anchors:
            seq = anchor.get("seq", anchor.get("step_seq"))  # 兼容 worker step_seq 形态
            step = by_seq.get(seq) if isinstance(seq, int) else None
            anchor_iri = anchor.get("action_iri")
            anchor_hash = anchor.get("param_hash")
            if (
                step is None
                or not isinstance(anchor_iri, str)
                or step.action_iri != anchor_iri
                or not isinstance(anchor_hash, str)
                or anchor_hash != canonical_param_hash(step.parameters)
            ):
                deviations.append({"seq": seq, "action_iri": anchor_iri, "param_hash": anchor_hash})
                continue
            if step.execution_mode is not ExecutionMode.READ:  # 幂等安全门：恒不跳、非偏差
                continue
            skips.append(step)
        if deviations:  # 计划变更/不匹配 → 全量重放（现状），审计事件记偏差步
            self._emit(
                rc.ledger,
                rc.ctx,
                rc.task.run_id,
                "kernel.resume_mismatch",
                {"deviations": deviations, "replay": "full", "stage": str(LoopStage.PLANNING)},
            )
            return
        for step in skips:
            state = rc.states[step.seq]
            state.transition(StepStatus.VALIDATED, stage=LoopStage.PLANNING)  # 特批迁移（白名单条目）
            state.resumable = True  # 对账续跑锚点口径一致（C1）
            rc.ledger.record_step(state)
            self._emit(
                rc.ledger,
                rc.ctx,
                rc.task.run_id,
                "kernel.step_resumed_validated",
                {
                    "step_seq": step.seq,
                    "action_iri": step.action_iri,
                    "param_hash": canonical_param_hash(step.parameters),
                    "resumed": True,
                    "stage": str(LoopStage.PLANNING),
                },
            )

    # ── M4.5-A：段边界 steering/inject 拼接（docs/Agent/12 §1.1）───────────
    def _splice_inbox_blocks(self, rc: RunContext, inbox: KernelInbox) -> None:
        """段边界 drain（steer+inject 全取、followup 留存）：文本包装为 ContextBlock
        （source="user_steer"，B3 标界 agent_attested，tier=3 易变尾）追加进运行组装面
        （rc.context_blocks，grounding 供给器通道就近接入）；每笔落 kernel.inbox_drained
        （payload：kind/source/seq/text——审计必需内容，非工具正文）。
        """
        drained: tuple[InboxItem, ...] = inbox.drain_steerable()
        if not drained:
            return
        blocks: list[ContextBlock] = list(rc.context_blocks)
        for item in drained:
            # B3：用户 steer 文本同为不可信外部输入，信任级由内核标界 agent_attested；
            # tokens=组装器同源估算（K11-a 收编：原零成本留痕口径不入水位，steer 注入
            # 增长对步间复判不可见——现按 estimate_tokens 计入块与 tracker 估算账，
            # 压缩可回收、水位可复判；M4.5-B 注释遗留的「成本锚定收编」就此闭环）。
            tokens = estimate_tokens(item.text)
            blocks.append(
                ContextBlock(
                    source="user_steer", content=item.text, tokens=tokens, trust_level=TrustLevel.AGENT_ATTESTED
                )
            )
            rc.tracker.add_estimated(tokens)  # M4.5-B 锚定估算账：steer 注入增长入账（复判/预算检查口径）
            self._emit(
                rc.ledger,
                rc.ctx,
                rc.task.run_id,
                "kernel.inbox_drained",
                {"kind": item.kind, "source": item.source, "seq": item.seq, "text": item.text},
            )
        rc.context_blocks = tuple(blocks)

    # ── K11-a 步间水位复判（docs/Agent/13 §17；研究 12/gemini-cli §4 请求前溢出预判）──
    async def _recheck_watermark(self, rc: RunContext) -> None:
        """段边界步/段执行前水位复判：组装每 Run 仅一次，运行中 steer 注入与真实消耗使
        ``tracker.tokens_effective``（M4.5-B 锚定估算，含 steer 增长）持续增长而组装级
        压缩门不再生效——此处按压缩同源 0.8 比率语义（CompactionTrigger）复判：

        - 未超水位：零开销直通（不触压缩器、零事件）；
        - 超水位且帽内：走组装阶段压缩等价路径（K1-b 降级链：策略→提取式→截断）压缩
          运行组装面（只动易变尾，不重新全量组装），压缩回收的估算 tokens 经
          ``tracker.compress_estimated`` 回冲锚定账（重算水位）后再执行该步；
        - 超水位且达帽（K11-b 防压缩风暴）：不再步间压缩，落
          ``kernel.watermark_recheck_capped`` 警告事件，超水位步照常执行（预算检查点/
          硬终止兜底）；压缩后仍超限的步同样照常执行（帽是风暴唯一防线，兜底终断语义）。
        - 复判失败不阻断（A-8 同款：异常结构化转义留警告日志，该步照常执行）。

        无 token 上限（max_tokens=None）=无水位可言，直接返回（零开销）。
        """
        budget_tokens = rc.tracker.max_tokens
        if budget_tokens is None or budget_tokens <= 0:
            return  # 无 token 预算：无水位可言（A4 其他维兜底照常）
        trigger = CompactionTrigger(threshold=self._context_stage.resolve_compaction_threshold())
        effective = rc.tracker.tokens_effective
        if not trigger.should_compact(effective, budget_tokens):
            return  # 未超水位：零开销直通（压缩器不被调用）
        if rc.recheck_compactions >= self._watermark_recheck_max:
            # K11-b：达帽——不再压缩，警告事件后照常执行（后续靠预算兜底/硬终止）
            self._emit(
                rc.ledger,
                rc.ctx,
                rc.task.run_id,
                "kernel.watermark_recheck_capped",
                {
                    "cap": self._watermark_recheck_max,
                    "compactions": rc.recheck_compactions,
                    "tokens_effective": effective,
                    "watermark_line": trigger.target_tokens(budget_tokens),
                    "stage": str(LoopStage.EXECUTION),
                },
            )
            return
        try:
            compacted, event = await self._context_stage.compact_volatile_tail(
                rc, list(rc.context_blocks), budget_tokens=budget_tokens
            )
        except Exception as exc:  # 降级矩阵：复判压缩失败不阻断执行（结构化转义留痕）
            logger.warning("步间水位复判压缩失败（不阻断执行）: %s", exc)
            return
        rc.recheck_compactions += 1
        before = sum(max(b.tokens, 0) for b in rc.context_blocks)
        after = sum(max(b.tokens, 0) for b in compacted)
        reclaimed = before - after
        if reclaimed > 0:
            rc.tracker.compress_estimated(reclaimed)  # 压缩后重算水位：回收估算回冲锚定账
        rc.context_blocks = tuple(compacted)  # 运行组装面就地收敛（段边界 steer 追加通道不受影响）
        self._emit(
            rc.ledger,
            rc.ctx,
            rc.task.run_id,
            "kernel.context_compacted",
            {
                **event,
                "scope": "step_recheck",  # 与组装级压缩区分（additive 字段）
                "reclaimed_estimated": reclaimed,
                "tokens_effective_after": rc.tracker.tokens_effective,
            },
        )

    def _candidate_from_model(self, raw: dict[str, Any]) -> PlanCandidate:
        """模型结构化产物 → 计划候选（值不经采样：逐字段确定性收窄，非法字段丢弃）。"""
        steps: list[PlanStep] = []
        raw_steps = raw.get("steps")
        if isinstance(raw_steps, list):
            for item in raw_steps:
                if not isinstance(item, dict):
                    continue
                seq = item.get("seq")
                action_iri = item.get("action_iri")
                if not isinstance(seq, int) or not isinstance(action_iri, str):
                    continue
                mode_raw = item.get("execution_mode", ExecutionMode.READ.value)
                mode = (
                    ExecutionMode(mode_raw)
                    if isinstance(mode_raw, str) and mode_raw in {m.value for m in ExecutionMode}
                    else ExecutionMode.READ
                )
                steps.append(PlanStep(seq=seq, action_iri=action_iri, execution_mode=mode))
        return PlanCandidate(strategy_name="kernel.model_fallback", steps=tuple(steps))

    def _validate_candidate(self, candidate: PlanCandidate) -> None:
        """计划三层校验 v1：① 结构层（pydantic 形状 + parameter_schema 域校验）；
        ② 绑定层（行动类有执行载体、seq 严格递增）；③ 分级层（executionMode 合法枚举，
        pydantic StrEnum 保证）。

        绑定层载体：read/write 行动类须有 tools.bindings 绑定；code 行动类须有
        execution.backends 注册（02 §3：沙箱代码行动类执行器走执行后端）。
        parameter_schema 在**计划验收期**校验（hermes 勘察细节 1，02 §11.2-1：坏 schema
        验收期报错，不拖到运行期门禁才炸）。"""
        seqs = [s.seq for s in candidate.steps]
        if len(seqs) != len(set(seqs)):
            raise KernelContractError("计划步 seq 重复（绑定层校验失败）")
        if seqs != sorted(seqs):
            raise KernelContractError("计划步 seq 非严格递增（绑定层校验失败）")
        for step in candidate.steps:
            self._validate_parameter_schema(step)
            if step.execution_mode is ExecutionMode.CODE:
                if self._dispatcher.execution_backend() is None:
                    raise KernelContractError("code 行动类需注册 ExecutionBackend（L3 执行后端）")
            elif self._dispatcher.tool_for(step.action_iri) is None:
                raise KernelContractError(f"计划引用未绑定行动类: {step.action_iri}（绑定层校验失败）")

    @staticmethod
    def _validate_parameter_schema(step: PlanStep) -> None:
        """参数域 Schema 最小形状校验（结构层）：对象 Schema + required ⊆ properties。"""
        schema = step.parameter_schema
        if not schema:
            return  # 空 Schema=无参数域约束（合法，chat 模板档形态）
        if not isinstance(schema, dict) or ("type" in schema and schema["type"] != "object"):
            raise KernelContractError(f"计划步 {step.seq} parameter_schema 须为 object Schema（结构层校验失败）")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise KernelContractError(f"计划步 {step.seq} parameter_schema properties/required 形状非法（结构层）")
        unknown = [key for key in required if key not in properties]
        if unknown:
            raise KernelContractError(
                f"计划步 {step.seq} parameter_schema required 引用未声明属性: {unknown}（结构层校验失败）"
            )

    # ── ④ 门禁（基线先、包后，只增不替；B1）──────────────────────────────
    async def _stage_gate(self, rc: RunContext, candidate: PlanCandidate, step: PlanStep) -> None:
        state = rc.states[step.seq]
        ctx = rc.ctx
        state.transition(StepStatus.GATED, stage=LoopStage.GATE)
        decision = ActionDecision(
            action_iri=step.action_iri,
            execution_mode=step.execution_mode,
            parameters=dict(step.parameters),
            step_seq=step.seq,
        )
        param_hash = canonical_param_hash(decision.parameters)
        approval = next((a for a in rc.approvals if a.param_hash == param_hash), None)
        # 执行载体（绑定层）：read/write=tools.bindings；code=execution.backends（02 §3）
        has_backend = step.execution_mode is ExecutionMode.CODE and self._dispatcher.execution_backend() is not None
        baseline = self._baseline.check(
            decision,
            step,
            ctx,
            tool_bound=self._dispatcher.tool_for(step.action_iri) is not None or has_backend,
            approval=approval,
            param_hash=param_hash,
        )
        pack_reports: list[GateReport] = []
        for gate in self._dispatcher.pre_gates:  # 包 gate：投影上毫秒级、确定性（§4 表）
            try:
                pack_reports.append(
                    await asyncio.wait_for(gate.check(decision, ctx), timeout=self._resolve_gate_timeout_s())
                )
            except TimeoutError:  # 降级矩阵：包 gate 超时按拒绝合成（基线不受影响）
                pack_reports.append(self._pack_timeout_report(gate.meta.name, decision))
        report = BaselineGate.compose(baseline, pack_reports)  # 篡改基线（自称基线）在此被拒
        state.gate_verdict = report.verdict.value
        state.budget_watermark = rc.tracker.watermark()
        if not report.passed:
            state.transition(StepStatus.FAILED, stage=LoopStage.GATE)
            state.error = _err(
                ErrorCode.PARAM_INVALID,
                f"门禁拒绝: {report.findings[0].code} {report.findings[0].message}",
            )
        rc.ledger.record_step(state)
        self._emit(
            rc.ledger,
            ctx,
            state.run_id,
            "kernel.gated",
            {
                "step_seq": state.seq,
                "verdict": state.gate_verdict,
                "strategy": candidate.strategy_name,
                "stage": str(LoopStage.GATE),
            },
        )

    def _pack_timeout_report(self, reporter: str, decision: ActionDecision) -> GateReport:
        return GateReport(
            verdict=GateVerdict.REJECT,
            is_baseline=False,
            reporter=reporter,
            findings=(
                GateFinding(
                    focus=decision.action_iri,
                    rule_iri="pack.gate.timeout",
                    severity="error",
                    code=int(ErrorCode.PARAM_INVALID),
                    message="包 gate 超时，按拒绝合成（降级矩阵）",
                ),
            ),
        )

    # ── ⑥ 观察（后验＋漂移对账＋写回）───────────────────────────────────
    async def _stage_observation(self, rc: RunContext, step: PlanStep) -> None:
        state, ctx, ledger = rc.states[step.seq], rc.ctx, rc.ledger
        if state.action_iri != step.action_iri:  # 漂移检测：执行动作与计划对账（A1 阶段五）
            state.transition(StepStatus.FAILED, stage=LoopStage.OBSERVATION)
            state.error = _err(ErrorCode.PARAM_INVALID, "漂移检测失败：执行动作偏离计划")
            ledger.record_step(state)
            return
        result = rc.results.get(step.seq)
        ok = result is not None and result.ok
        findings: list[str] = []
        if result is not None:
            for gate in self._dispatcher.post_gates:  # gates.post：只报违例，不改状态（T2）
                try:
                    report = await asyncio.wait_for(gate.validate(result, ctx), timeout=self._resolve_gate_timeout_s())
                    if not report.ok:
                        findings.append(report.validator or "post_gate")
                except TimeoutError:
                    findings.append("post_gate_timeout")
        if ok and not findings:
            state.transition(StepStatus.VALIDATED, stage=LoopStage.OBSERVATION)
            state.resumable = True  # C1 重连续跑锚点：validated 步可对账续跑
        else:
            state.transition(StepStatus.FAILED, stage=LoopStage.OBSERVATION)
            state.error = _err(
                ErrorCode.PARAM_INVALID,
                "后验失败" if not ok else f"后验校验未过: {','.join(findings)}",
            )
        state.budget_watermark = rc.tracker.watermark()
        ledger.record_step(state)
        event_type = "kernel.step_validated" if state.status is StepStatus.VALIDATED else "kernel.step_failed"
        tool_result = result.tool_result if result is not None else None
        self._emit(
            ledger,
            ctx,
            state.run_id,
            event_type,
            {
                "step_seq": state.seq,
                "action_iri": step.action_iri,
                # M4.5-A additive（docs/Agent/12 §1.3）：P-4 resume 计划对账锚点三元组之
                # param_hash（canonical JSON sha256，B5 同源函数）——重试/续跑 spawn 据此
                # 与新计划步做全等匹配，匹配且 READ 才可特批跳过。
                "param_hash": canonical_param_hash(step.parameters),
                "trust_level": tool_result.trust_level.value if tool_result else None,
                "claimed_trust_level": (
                    tool_result.claimed_trust_level.value if tool_result and tool_result.claimed_trust_level else None
                ),
                "stage": str(LoopStage.OBSERVATION),
            },
        )

    # ── ⑦ 沉淀（闭环落账）───────────────────────────────────────────────
    async def _stage_settlement(self, rc: RunContext, candidate: PlanCandidate) -> RunOutcome:
        ledger, ctx, task = rc.ledger, rc.ctx, rc.task
        ledger.assert_no_open_calls()  # C1 不变式：未闭合 tool_call 禁进终态
        # E-4 K1-c（docs/Agent/13 §2）：判据全路径求值——回执优先（M3 口径不变），回执缺失
        # 且判据声明投影求值面时经 CriterionProjection 端口补充分支（无端口/无声明=纯回执，行为不变）
        criteria = await self._evaluator.evaluate_with_projection(candidate.success_criteria, ledger, ctx)
        states_in_run = list(rc.states.values())
        any_failed = any(s.status is StepStatus.FAILED or s.status is StepStatus.CANCELLED for s in states_in_run)
        blocked_by_trust = any(c.blocked_by_trust for c in criteria)
        # E-4 K1-c：投影可求值且未满足（satisfied=False 且非 blocked）=确定性负结论——
        # 不可判完成（终止只认判据求值，A4）；纯回执口径下该形态不存在（零行为变化面）
        unsatisfied_evaluable = any(c.satisfied is False and not c.blocked_by_trust for c in criteria)
        if any_failed or unsatisfied_evaluable:
            status, reason_code = RunStatus.FAILED, int(ErrorCode.PARAM_INVALID)
            reason = (
                "存在未通过步（门禁/审批/执行/后验失败）"
                if any_failed
                else "存在可求值判据未满足（B2 投影求值不通过，agent 自述不采信）"
            )
        elif blocked_by_trust:
            status, reason_code = RunStatus.WAITING_TOOL, None
            reason = "判据暂不可求值：等待外部回执（B2，agent 自述不采信；重连续跑锚点）"
        else:
            status, reason_code = RunStatus.COMPLETED, None
            reason = "全部步 validated 且判据满足（或无判据）"
        for sink in self._dispatcher.event_sinks:  # 事件汇（Outbox/审计 sink 为内核必选，不经此）
            try:
                await asyncio.wait_for(sink.handle(list(ledger.events), ctx), timeout=self._resolve_sink_timeout_s())
            except Exception as exc:  # 通知失败不阻断落账（结构化转义留痕）
                logger.warning("事件汇失败（不阻断落账）: %s", exc)
        await ledger.drain_sink(timeout_s=self._resolve_sink_timeout_s())  # C1 投影排水：先落库后终态（可追溯口径）
        self._emit(
            ledger,
            ctx,
            task.run_id,
            "kernel.settled",
            {
                "status": str(status),
                "criteria": [c.model_dump() for c in criteria],
                "watermark": ledger.steps[-1].budget_watermark.model_dump() if ledger.steps else {},
                "stage": str(LoopStage.SETTLEMENT),
            },
        )
        await ledger.drain_sink(
            timeout_s=self._resolve_sink_timeout_s()
        )  # C1 投影排水：settled 锚点亦投影，先落库后终态（可追溯口径）
        return RunOutcome(
            run_id=task.run_id,
            status=str(status),
            reason_code=reason_code,
            reason=reason,
            terminal_states=tuple(rc.states.values()),
            criteria=criteria,
        )

    # ── 中断收敛（预算/超时/内核错误共用；清单先行，落终态）───────────────
    async def _finalize_interrupted(
        self,
        rc: RunContext,
        *,
        status: RunStatus,
        reason_code: int | None,
        reason: str,
        run_checklist: bool,
    ) -> RunOutcome:
        if run_checklist:  # §2.4：资源释放先行（在途调用中止/租约强制释放/工作区标记）
            await rc.coordinator.execute(reason=reason)
        await rc.ledger.drain_sink(
            timeout_s=self._resolve_sink_timeout_s()
        )  # C1 投影排水：终态可追溯（超时残留已在账本留痕）
        for state in rc.states.values():
            if not state.is_terminal:
                state.cancel(reason=reason)
            rc.ledger.record_step(state)
        self._emit_plan_terminal_sweep(rc)  # R4：已开跑未终态项随中断收敛推进 completed
        rc.ledger.assert_no_open_calls()  # 清单执行完毕才落终态（落态即零未闭合调用）
        self._emit(
            rc.ledger,
            rc.ctx,
            rc.task.run_id,
            "kernel.interrupted",
            {"status": str(status), "reason": reason, "residuals": list(rc.ledger.residuals)},
        )
        return RunOutcome(
            run_id=rc.task.run_id,
            status=str(status),
            reason_code=reason_code,
            reason=reason,
            terminal_states=tuple(rc.states.values()),
        )

    # ── 审计事件（C2）────────────────────────────────────────────────────
    def _emit_plan_snapshot(self, rc: RunContext) -> None:
        """计划整表快照发射（R4，40 篇 §4.2/§4.3）：kernel.plan_updated 锚点 →
        H-0a 广播转译 PLAN_UPDATED（revision 自 1 严格递增，last-wins 整表替换）。
        规划前（rc.plan=None）零发射；调用方在 items 有变化时才调用（零变化零事件）。"""
        if rc.plan is None:
            return
        self._emit(
            rc.ledger,
            rc.ctx,
            rc.task.run_id,
            KERNEL_PLAN_UPDATED,
            plan_updated_data(rc.plan, str(rc.task.run_id)),
        )

    def _emit_plan_terminal_sweep(self, rc: RunContext) -> None:
        """中断/取消路径的计划终局收敛（R4）：状态已终态的已开跑项推进 completed 后补一发
        快照（无变化零发射）——防取消收敛后前端计划卡滞留 in_progress 旋转。"""
        if rc.plan is None:
            return
        if rc.plan.finish_terminal(seq for seq, state in rc.states.items() if state.is_terminal):
            self._emit_plan_snapshot(rc)

    def _slot_emitter(self, rc: RunContext) -> Any:
        """子 Run 事件发射通道（40 篇 §8 R2，2026-10-04 批）：闭包父 Run 账本+上下文。

        交 AgentSlot.bind_parent 注入 BuiltinAgentSlot：spawn/close 发射的 SUBRUN 锚点
        事件经本闭包走与内核锚点同一 :meth:`_emit` 口——父 Run 账本入账（C2 同 trace，
        归属 run=父 Run，SUBRUN_STARTED 恒落在父 RUN_STARTED 与父 RUN_FINISHED 之间，
        40 篇 §4.3-5）+ H-0a 广播（ExecEventTranslator 转译为 ChatEvent，发射点=
        kernel/subagent.py）。
        """

        def emit(event_type: str, data: dict[str, Any]) -> None:
            self._emit(rc.ledger, rc.ctx, rc.task.run_id, event_type, data)

        return emit

    def _emit(
        self,
        ledger: KernelLedger,
        ctx: TenantContext,
        run_id: UUID,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        event = KernelEvent(
            event_type=event_type,
            tenant_id=ctx.tenant_id,
            run_id=run_id,
            trace_id=ctx.trace_id,
            data=data,
        )
        ledger.append_event(event)
        # H-0a on_kernel_event：锚点事件落账时同步广播给注册 observer（fire-and-forget，
        # 空注册零开销；hook 回调禁止再调内核——嵌套广播由注册表守卫丢弃）
        self._dispatcher.hooks.broadcast_kernel_event(event)

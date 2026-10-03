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
from services.agent.business.kernel.criteria import CriterionEvaluator
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.errors import BudgetExhaustedError, KernelContractError, KernelError
from services.agent.business.kernel.execution import ExecutionStage
from services.agent.business.kernel.gate_baseline import BaselineGate, canonical_param_hash
from services.agent.business.kernel.grounding import ContextAssemblyStage
from services.agent.business.kernel.ledger import KernelLedger, LedgerSink
from services.agent.business.kernel.run_context import RunContext
from services.agent.business.kernel.spill import SpillStore
from services.agent.business.kernel.subagent import ParentBindable
from services.agent.business.kernel.tool_dispatch import ToolGroupDispatcher, segment_steps
from services.agent.domain.model.kernel_actions import ActionDecision, ApprovalTicket, ExecutionMode
from services.agent.domain.model.kernel_context import ContextBlock, KernelEvent, TaskRef, TenantContext
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
    ) -> None:
        self._dispatcher = dispatcher
        self._baseline = BaselineGate()
        self._evaluator = CriterionEvaluator()
        self._clock = clock
        # B-③ 批（docs/Agent/10 §8.2）：缺省 None=运行期从配置层解析（Settings 唯一事实
        # 源）；显式注入优先（测试与组合根直传通道，D2/F-4 同款纪律）。
        self._tool_timeout_s = tool_timeout_s
        # B-① 并行段并发度：显式注入优先，缺省读 Settings（T6 唯一事实源；=1 退化为串行）
        self._tool_parallelism = (
            tool_parallelism if tool_parallelism is not None else get_settings().kernel_tool_parallelism
        )
        self._last_ledger: KernelLedger | None = None
        # 阶段执行器（内核私有；依赖注入同一分发器，禁直连能力实现）
        self._context_stage = ContextAssemblyStage(dispatcher, self._emit)
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
    ) -> RunOutcome:
        """执行一次 Run（七阶段）。取消传播下状态一致：终态经账本可追溯后重抛取消。

        ``ledger_sink``（C1 PG 台账投影，组合根注入）：内核锚点事件在入账同时异步投影
        到持久层，终态前排水（先落库后终态的可追溯口径）；投影失败不阻断运行。
        """
        if not ctx.trace_id:
            raise KernelContractError("TenantContext.trace_id 为空，拒绝运行（C2 可追溯底线）")
        rc = RunContext(task, ctx, budget, clock=self._clock, approvals=approvals, ledger_sink=ledger_sink)
        self._last_ledger = rc.ledger
        self._execution_stage.spill_store = spill_store  # per-run spill 注入（02 §11.2-11）
        slot = self._dispatcher.agent_slot()  # agent.slots（02 §4.2）：可绑定实现挂父作用域（分账+级联）
        if isinstance(slot, ParentBindable):
            slot.bind_parent(task.run_id, rc.tracker, rc.coordinator)
        try:
            # 时长维硬兜底（A4）；未设时长预算按 24h 封顶。超限中断统一走取消清单收敛。
            hard_cap = budget.duration_s if budget.duration_s is not None else 86_400.0
            async with asyncio.timeout(hard_cap):
                blocks = await self._context_stage.run(rc)
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
                for group in segment_steps(candidate.steps, parallelism=self._tool_parallelism):
                    if len(group) == 1:  # 单步段=完全现状串行路径（B-① 零行为差异面）
                        step = group[0]
                        rc.tracker.check()  # A4 检查点：步前预算断言（超限优雅终止）
                        state = rc.states[step.seq]
                        await self._stage_gate(rc, candidate, step)
                        if state.status is StepStatus.GATED:
                            await self._execution_stage.run(rc, step)
                        if state.status is StepStatus.EXECUTING:
                            await self._stage_observation(rc, step)
                        rc.tracker.add_step()  # 步数预算记账（步后累计，下一步检查点生效）
                    else:  # 多步段：段前预算检查点一次，段执行器负责门禁先行+池执行+声明序收口
                        rc.tracker.check()
                        remaining = rc.tracker.remaining_steps
                        planned = len(group)
                        # 段截断至剩余步预算：尾部步不执行=与串行逐步检查点语义等价（预算耗尽后串行同样不执行它们）
                        group = group if remaining is None else group[:remaining]
                        await self._tool_dispatch.run_group(rc, candidate, group, parallelism=self._tool_parallelism)
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
        return candidate

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
        criteria = self._evaluator.evaluate(candidate.success_criteria, ledger)
        states_in_run = list(rc.states.values())
        any_failed = any(s.status is StepStatus.FAILED or s.status is StepStatus.CANCELLED for s in states_in_run)
        blocked_by_trust = any(c.blocked_by_trust for c in criteria)
        if any_failed:
            status, reason_code = RunStatus.FAILED, int(ErrorCode.PARAM_INVALID)
            reason = "存在未通过步（门禁/审批/执行/后验失败）"
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

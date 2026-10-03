# tests/agent/test_kernel_parallel_dispatch.py
"""B-① Run 内并行工具调度器验收测试（docs/Agent/10 §3/§4）。

分段规则（parallelizable READ 连续游程、段长 ≤ 并发度）、段执行不变式（门禁先行/
池执行/声明序收口/单步超时与异常隔离/预算与取消闭合）与组审计事件
（kernel.group_started/finished，transport-only）逐条验收；并行度为 1 与默认
parallelizable=False 两档显式断言零行为变化。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.gate_baseline import canonical_param_hash
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_actions import (
    ActionDecision,
    ApprovalTicket,
    ExecutionMode,
    ToolCall,
    ToolResult,
)
from services.agent.domain.model.kernel_context import ExtensionMeta, KernelEvent, TenantContext
from services.agent.domain.model.kernel_gates import GateFinding, GateReport, GateVerdict
from services.agent.domain.model.kernel_planning import PlanCandidate, PlanStep
from services.agent.domain.model.step_state import LoopStage, StepStatus
from services.agent.domain.model.task import RunStatus
from services.platform.errors import ErrorCode
from tests.agent.conftest import WRITE_ACTION_IRI, FakePlanner, make_candidate, make_ctx, make_step, make_task

_PAR_BASE_IRI = "http://ontology.example/action/par"


def _iri(seq: int) -> str:
    return f"{_PAR_BASE_IRI}_{seq}"


def _par_step(seq: int) -> PlanStep:
    """parallelizable READ 计划步（规划策略显式声明的形态，B-① §2）。"""
    return make_step(seq=seq, action_iri=_iri(seq)).model_copy(update={"parallelizable": True})


class _ProbeTool:
    """局部 Fake（conftest FakeTool 族同风格）：时延执行＋并发峰值/完成序观测面（内核不感知）。"""

    def __init__(
        self,
        action_iri: str,
        *,
        sleep_s: float = 0.0,
        usage: dict[str, int] | None = None,
        raise_exc: Exception | None = None,
        probe: dict[str, object] | None = None,
    ) -> None:
        self.meta = ExtensionMeta(
            name=f"fixture.tool.{action_iri.rsplit('/', 1)[-1]}",
            version="1.0.0",
            semantic_annotation={"action_iri": action_iri},
        )
        self.sleep_s = sleep_s
        self.usage = usage or {}
        self.raise_exc = raise_exc
        self.probe: dict[str, object] = probe if probe is not None else {}
        self.calls: list[ToolCall] = []

    async def invoke(
        self,
        call: ToolCall,
        ctx: TenantContext,
        *,
        approval: ApprovalTicket | None = None,
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        self.calls.append(call)
        probe = self.probe
        active = probe.get("active", 0)
        probe["active"] = active + 1
        probe["peak"] = max(probe.get("peak", 0), active + 1)
        try:
            if self.sleep_s:
                await asyncio.sleep(self.sleep_s)
            if self.raise_exc is not None:
                raise self.raise_exc
            return ToolResult(ok=True, output={"rows": 1}, usage=self.usage)
        finally:
            probe["active"] = probe.get("active", 1) - 1
            completed = probe.setdefault("completed", [])
            completed.append(call.step_seq)  # type: ignore[attr-defined]


class _CancelAwareTool(_ProbeTool):
    """取消观测桩（FakeTool 捕获 CancelledError 既有模式）：invoke 收到取消登记步号后重抛，任务必终结。"""

    async def invoke(
        self,
        call: ToolCall,
        ctx: TenantContext,
        *,
        approval: ApprovalTicket | None = None,
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        try:
            return await super().invoke(call, ctx, approval=approval, timeout_ms=timeout_ms)
        except asyncio.CancelledError:
            cancelled = self.probe.setdefault("cancelled", [])
            cancelled.append(call.step_seq)  # type: ignore[attr-defined]
            raise


class _SeqRejectGate:
    """包 gate 桩：仅对指定 seq 拒绝（驱动「段内单步拒绝不炸整段」场景）。"""

    def __init__(self, target_seq: int) -> None:
        self.meta = ExtensionMeta(
            name="fixture.seq_gate",
            version="1.0.0",
            semantic_annotation={"rule_iri": "http://ontology.example/rule/段内单步拒绝"},
        )
        self.target_seq = target_seq

    async def check(self, decision: ActionDecision, ctx: TenantContext, *, timeout_ms: int = 100) -> GateReport:
        if decision.step_seq == self.target_seq:
            return GateReport(
                verdict=GateVerdict.REJECT,
                is_baseline=False,
                reporter=self.meta.name,
                findings=(
                    GateFinding(
                        focus=decision.action_iri,
                        rule_iri="pack.gate.seq",
                        severity="error",
                        code=3001,
                        message=f"步 {self.target_seq} 被包 gate 拒绝",
                    ),
                ),
            )
        return GateReport(verdict=GateVerdict.ALLOW, is_baseline=False, reporter=self.meta.name)


def _kernel_with(candidate: PlanCandidate, tools: list[_ProbeTool], **kernel_kw: object) -> AgentKernel:
    dispatcher = ExtensionDispatcher()
    for tool in tools:
        dispatcher.register_tool(tool)
    dispatcher.register_planning_strategy(FakePlanner(candidate))
    pre_gate = kernel_kw.pop("pre_gate", None)
    if pre_gate is not None:
        dispatcher.register_pre_gate(pre_gate)
    return AgentKernel(dispatcher, **kernel_kw)  # type: ignore[arg-type]


def _events(kernel: AgentKernel, event_type: str) -> list[KernelEvent]:
    assert kernel.last_ledger is not None
    return [e for e in kernel.last_ledger.events if e.event_type == event_type]


async def test_连续可并行读步_真并发执行_并发峰值达段内步数():
    # arrange：3 个 parallelizable READ 步（各 0.2s），并发度默认 4 ⇒ 单段 3 步
    probe: dict[str, object] = {}
    tools = [_ProbeTool(_iri(seq), sleep_s=0.2, probe=probe) for seq in (1, 2, 3)]
    kernel = _kernel_with(make_candidate(tuple(_par_step(seq) for seq in (1, 2, 3))), tools)
    # act
    started = time.monotonic()
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))
    wall_s = time.monotonic() - started
    # assert：三步同时在途（峰值=3），墙钟显著小于串行总和（0.6s）
    assert outcome.status == str(RunStatus.COMPLETED)
    assert probe["peak"] == 3
    assert wall_s < 0.6
    group_started = _events(kernel, "kernel.group_started")
    assert [e.data["step_seqs"] for e in group_started] == [[1, 2, 3]]
    assert group_started[0].data["concurrency"] == 3
    group_finished = _events(kernel, "kernel.group_finished")
    assert [e.data["statuses"] for e in group_finished] == [{"1": "validated", "2": "validated", "3": "validated"}]
    # transport-only：组事件只含段元数据，不含工具输出正文
    assert set(group_started[0].data) == {"step_seqs", "admitted_seqs", "concurrency", "stage"}


async def test_段内完成顺序倒置_观察与记账仍按声明顺序收口():
    # arrange：sleep 反向（步 1 最长）⇒ 完成序 3→2→1，收口须仍按声明序
    probe: dict[str, object] = {}
    sleeps = {1: 0.3, 2: 0.2, 3: 0.01}
    tools = [_ProbeTool(_iri(seq), sleep_s=sleeps[seq], probe=probe) for seq in (1, 2, 3)]
    kernel = _kernel_with(make_candidate(tuple(_par_step(seq) for seq in (1, 2, 3))), tools)
    # act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))
    # assert
    assert outcome.status == str(RunStatus.COMPLETED)
    assert probe["completed"] == [3, 2, 1]  # 前置：完成序确实倒置
    ledger = kernel.last_ledger
    assert ledger is not None
    validated = [e.data["step_seq"] for e in _events(kernel, "kernel.step_validated")]
    assert validated == [1, 2, 3]  # observation 事件按声明序
    assert [s.seq for s in outcome.terminal_states] == [1, 2, 3]
    # 记账序：observation 水位在各自 add_step 之前打点 ⇒ 声明序对应 0/1/2
    assert [s.budget_watermark.steps_done for s in outcome.terminal_states] == [0, 1, 2]
    assert [c.step_seq for c in ledger.tool_calls] == [1, 2, 3]  # open_tool_call 按声明序落


async def test_外部写步插段中_并行段以写步为界切分_写步走审批串行():
    # arrange：par(1) par(2) externalWrite(3) par(4) par(5) ⇒ 段 [1,2] / [3] / [4,5]
    probe: dict[str, object] = {}
    write_tool = _ProbeTool(WRITE_ACTION_IRI)
    tools = [
        _ProbeTool(_iri(1), sleep_s=0.15, probe=probe),
        _ProbeTool(_iri(2), sleep_s=0.15, probe=probe),
        write_tool,
        _ProbeTool(_iri(4), sleep_s=0.15, probe=probe),
        _ProbeTool(_iri(5), sleep_s=0.15, probe=probe),
    ]
    steps: tuple[PlanStep, ...] = (
        _par_step(1),
        _par_step(2),
        make_step(seq=3, action_iri=WRITE_ACTION_IRI, mode=ExecutionMode.EXTERNAL_WRITE),
        _par_step(4),
        _par_step(5),
    )
    kernel = _kernel_with(make_candidate(steps), tools)
    ticket = ApprovalTicket(param_hash=canonical_param_hash({"q": "线路A"}))
    # act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30), approvals=(ticket,))
    # assert：写步不进任何段（天然 barrier），携回执照常执行
    assert outcome.status == str(RunStatus.COMPLETED)
    assert [s.status for s in outcome.terminal_states] == [StepStatus.VALIDATED] * 5
    assert len(write_tool.calls) == 1
    group_started = _events(kernel, "kernel.group_started")
    assert [e.data["step_seqs"] for e in group_started] == [[1, 2], [4, 5]]
    assert all(3 not in e.data["step_seqs"] for e in group_started)


async def test_默认parallelizable为False_多读步不分段_走既有串行路径():
    # arrange：两个 READ 步，不声明 parallelizable（既有构造形态）
    probe: dict[str, object] = {}
    tools = [
        _ProbeTool(_iri(1), sleep_s=0.05, probe=probe),
        _ProbeTool(_iri(2), sleep_s=0.05, probe=probe),
    ]
    steps = (make_step(seq=1, action_iri=_iri(1)), make_step(seq=2, action_iri=_iri(2)))
    kernel = _kernel_with(make_candidate(steps), tools)
    # act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))
    # assert：零组事件＋零并发（峰值=1）＋完成序=声明序（零行为变化另由全量回归覆盖）
    assert outcome.status == str(RunStatus.COMPLETED)
    assert _events(kernel, "kernel.group_started") == []
    assert _events(kernel, "kernel.group_finished") == []
    assert probe["peak"] == 1
    assert probe["completed"] == [1, 2]


async def test_段内单步超时_该步5003其余正常_零悬挂调用():
    # arrange：tool_timeout_s=0.1；步 2 慢（1.0s）触发单调用超时
    probe: dict[str, object] = {}
    tools = [
        _ProbeTool(_iri(1), sleep_s=0.01, probe=probe),
        _ProbeTool(_iri(2), sleep_s=1.0, probe=probe),
        _ProbeTool(_iri(3), sleep_s=0.01, probe=probe),
    ]
    kernel = _kernel_with(make_candidate(tuple(_par_step(seq) for seq in (1, 2, 3))), tools, tool_timeout_s=0.1)
    # act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))
    # assert：超时步 5003 结构化失败、其余照常、零悬挂
    assert outcome.status == str(RunStatus.FAILED)
    assert [s.status for s in outcome.terminal_states] == [
        StepStatus.VALIDATED,
        StepStatus.FAILED,
        StepStatus.VALIDATED,
    ]
    ledger = kernel.last_ledger
    assert ledger is not None
    slow_call = next(c for c in ledger.tool_calls if c.step_seq == 2)
    assert slow_call.error_code == int(ErrorCode.MCP_TARGET_UNAVAILABLE)
    assert ledger.open_call_ids() == ()
    assert [e.data["step_seq"] for e in _events(kernel, "kernel.step_failed")] == [2]
    await asyncio.sleep(1.2)  # 被 shield 留在途的慢任务自行收尾（既有僵尸排空口径，防循环悬挂告警）


async def test_段内单步工具抛异常_该步结构化失败其余照常():
    # arrange：步 2 的工具实现裸异常
    probe: dict[str, object] = {}
    tools = [
        _ProbeTool(_iri(1), sleep_s=0.05, probe=probe),
        _ProbeTool(_iri(2), sleep_s=0.05, probe=probe, raise_exc=RuntimeError("能力实现崩溃")),
        _ProbeTool(_iri(3), sleep_s=0.05, probe=probe),
    ]
    kernel = _kernel_with(make_candidate(tuple(_par_step(seq) for seq in (1, 2, 3))), tools)
    # act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))
    # assert：一步异常=该步结构化失败，其余步不受影响（禁异常逃逸）
    assert outcome.status == str(RunStatus.FAILED)
    assert [s.status for s in outcome.terminal_states] == [
        StepStatus.VALIDATED,
        StepStatus.FAILED,
        StepStatus.VALIDATED,
    ]
    ledger = kernel.last_ledger
    assert ledger is not None
    bad_call = next(c for c in ledger.tool_calls if c.step_seq == 2)
    assert bad_call.error_code == int(ErrorCode.INTERNAL_ERROR)
    assert bad_call.closed
    assert ledger.open_call_ids() == ()
    assert probe["completed"] == [1, 2, 3]  # 三次调用全部收尾（步 2 收尾=抛异常）；隔离面由上方状态断言承载


async def test_段后预算耗尽_剩余步合成闭合_run走中断终态():
    # arrange：token 预算 100；段内两步各回传 80 ⇒ 段后检查点耗尽；后续单步段不得执行
    probe: dict[str, object] = {}
    tools = [
        _ProbeTool(_iri(1), usage={"total_tokens": 80}, probe=probe),
        _ProbeTool(_iri(2), usage={"total_tokens": 80}, probe=probe),
        _ProbeTool(_iri(3), probe=probe),
    ]
    steps: tuple[PlanStep, ...] = (_par_step(1), _par_step(2), make_step(seq=3, action_iri=_iri(3)))
    kernel = _kernel_with(make_candidate(steps), tools)
    # act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_tokens=100, max_steps=10, duration_s=30))
    # assert：剩余步取消闭合、run 走既有 BudgetExhausted 终态路径
    assert outcome.status == str(RunStatus.FAILED)
    assert outcome.reason_code == int(ErrorCode.RETRY_BUDGET_EXHAUSTED)
    assert [s.status for s in outcome.terminal_states] == [
        StepStatus.VALIDATED,
        StepStatus.VALIDATED,
        StepStatus.CANCELLED,
    ]
    assert tools[2].calls == []  # 预算耗尽步未触达工具
    ledger = kernel.last_ledger
    assert ledger is not None
    assert ledger.open_call_ids() == ()
    interrupted = _events(kernel, "kernel.interrupted")
    assert interrupted and "预算耗尽" in interrupted[0].data["reason"]


async def test_取消中止并行段_全段零悬挂调用闭环():
    # arrange：三步全慢（10s），派发后在途取消（复用取消清单断言风格）
    probe: dict[str, object] = {}
    tools = [_ProbeTool(_iri(seq), sleep_s=10.0, probe=probe) for seq in (1, 2, 3)]
    kernel = _kernel_with(make_candidate(tuple(_par_step(seq) for seq in (1, 2, 3))), tools)
    run_task = asyncio.create_task(kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=60)))
    await asyncio.sleep(0.1)  # 段已派发、三调用在途
    assert probe["peak"] == 3
    # act
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task  # 取消语义保留：清理完毕后重抛
    # assert：清单步骤 2 全段闭合，零悬挂零残留
    ledger = kernel.last_ledger
    assert ledger is not None
    assert ledger.open_call_ids() == ()
    assert all(rec.closed_as_cancelled for rec in ledger.tool_calls)
    assert [s.status for s in ledger.steps[-3:]] == [StepStatus.CANCELLED] * 3
    assert ledger.residuals == ()
    cancelled = _events(kernel, "kernel.cancelled")
    assert cancelled and cancelled[0].data["forced"] == []


async def test_并发度为1时退化为串行_与既有路径等价():
    # arrange：parallelizable 三步 + 并发度注入 1（T6 配置档最小值）
    probe: dict[str, object] = {}
    tools = [_ProbeTool(_iri(seq), sleep_s=0.05, probe=probe) for seq in (1, 2, 3)]
    kernel = _kernel_with(make_candidate(tuple(_par_step(seq) for seq in (1, 2, 3))), tools, tool_parallelism=1)
    # act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))
    # assert：=1 时段全部退化为单步 ⇒ 无组事件、零并发、声明序执行
    assert outcome.status == str(RunStatus.COMPLETED)
    assert [s.status for s in outcome.terminal_states] == [StepStatus.VALIDATED] * 3
    assert _events(kernel, "kernel.group_started") == []
    assert _events(kernel, "kernel.group_finished") == []
    assert probe["peak"] == 1
    assert probe["completed"] == [1, 2, 3]


async def test_门禁拒绝段内一步_该步FAILED留段外其余照常并行():
    # arrange：包 gate 仅拒步 2；段 [1,2,3] 中被拒步留段外
    probe: dict[str, object] = {}
    tools = [
        _ProbeTool(_iri(1), sleep_s=0.15, probe=probe),
        _ProbeTool(_iri(2), sleep_s=0.15, probe=probe),
        _ProbeTool(_iri(3), sleep_s=0.15, probe=probe),
    ]
    kernel = _kernel_with(make_candidate(tuple(_par_step(seq) for seq in (1, 2, 3))), tools, pre_gate=_SeqRejectGate(2))
    # act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))
    # assert：被拒步 FAILED 于门禁阶段、未触达工具；其余两步照常并行
    assert outcome.status == str(RunStatus.FAILED)
    assert [s.status for s in outcome.terminal_states] == [
        StepStatus.VALIDATED,
        StepStatus.FAILED,
        StepStatus.VALIDATED,
    ]
    rejected = outcome.terminal_states[1]
    assert rejected.stage == LoopStage.GATE
    assert rejected.gate_verdict == "reject"
    assert tools[1].calls == []
    assert probe["peak"] == 2
    group_started = _events(kernel, "kernel.group_started")
    assert group_started[0].data["admitted_seqs"] == [1, 3]


async def test_段长不超过并发度上限_超限部分另起段且严格按序():
    # arrange：并发度 2 + 三个可并行步 ⇒ 段 [1,2] / [3]，段 2 须等段 1 全部结算
    probe: dict[str, object] = {}
    tools = [_ProbeTool(_iri(seq), sleep_s=0.1, probe=probe) for seq in (1, 2, 3)]
    kernel = _kernel_with(make_candidate(tuple(_par_step(seq) for seq in (1, 2, 3))), tools, tool_parallelism=2)
    # act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))
    # assert：步 3 落单步段（串行路径，无组事件），且在段 1 结算（group_finished）之后
    assert outcome.status == str(RunStatus.COMPLETED)
    assert [s.status for s in outcome.terminal_states] == [StepStatus.VALIDATED] * 3
    assert probe["peak"] == 2  # 段内并发 ≤ 上限
    assert probe["completed"] == [1, 2, 3]
    ledger = kernel.last_ledger
    assert ledger is not None
    group_started = _events(kernel, "kernel.group_started")
    assert [e.data["step_seqs"] for e in group_started] == [[1, 2]]
    events = ledger.events
    finish_idx = next(i for i, e in enumerate(events) if e.event_type == "kernel.group_finished")
    gated3_idx = next(i for i, e in enumerate(events) if e.event_type == "kernel.gated" and e.data["step_seq"] == 3)
    assert finish_idx < gated3_idx  # 段间严格按序：前段结算完才进下一段


async def test_步预算剩余小于段长_段截断至剩余步_尾部步不执行_预算耗尽优雅终止():
    # arrange：max_steps=2（段前预算剩余 2）；3 个可并行 READ 步成单段 ⇒ 段截断为前 2 步
    probe: dict[str, object] = {}
    tools = [
        _ProbeTool(_iri(1), sleep_s=0.05, probe=probe),
        _ProbeTool(_iri(2), sleep_s=0.05, probe=probe),
        _ProbeTool(_iri(3), sleep_s=0.05, probe=probe),
    ]
    kernel = _kernel_with(make_candidate(tuple(_par_step(seq) for seq in (1, 2, 3))), tools)
    # act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=2, duration_s=30))
    # assert：仅前 2 步触达工具（截断防段内超发，第 3 步不执行）；run 走既有预算耗尽优雅终态
    assert outcome.status == str(RunStatus.FAILED)
    assert outcome.reason_code == int(ErrorCode.RETRY_BUDGET_EXHAUSTED)
    assert [len(t.calls) for t in tools] == [1, 1, 0]
    assert probe["completed"] == [1, 2]
    statuses = {s.seq: s.status for s in outcome.terminal_states}
    assert statuses[1] is StepStatus.VALIDATED
    assert statuses[2] is StepStatus.VALIDATED
    assert statuses[3] is StepStatus.CANCELLED  # 未执行尾步落取消终态（与串行预算耗尽同口径）
    ledger = kernel.last_ledger
    assert ledger is not None
    assert ledger.open_call_ids() == ()
    interrupted = _events(kernel, "kernel.interrupted")
    assert interrupted and "预算耗尽" in interrupted[0].data["reason"]


async def test_取消中止在途段_段任务显式收取消_收口有界且零悬挂():
    # arrange：三步全慢（30s 假自然时长=最坏拖尾量级）；派发后在途取消
    probe: dict[str, object] = {}
    tools = [_CancelAwareTool(_iri(seq), sleep_s=30.0, probe=probe) for seq in (1, 2, 3)]
    kernel = _kernel_with(make_candidate(tuple(_par_step(seq) for seq in (1, 2, 3))), tools)
    run_task = asyncio.create_task(kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=60)))
    await asyncio.sleep(0.1)  # 段已派发、三调用在途
    assert probe["active"] == 3
    # act
    started = time.monotonic()
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task  # 取消语义保留：收尾完毕后重抛
    closing_s = time.monotonic() - started
    # assert：工具任务全部收到取消并终结（不等 30s 自然结束）；收口有界（不断言具体毫秒，只对齐清单 5s 纪律）
    assert closing_s < 5.0
    assert probe["cancelled"] == [1, 2, 3]
    assert probe["active"] == 0  # 三任务全部 terminated，零悬挂
    ledger = kernel.last_ledger
    assert ledger is not None
    assert ledger.open_call_ids() == ()
    assert all(rec.closed_as_cancelled for rec in ledger.tool_calls)
    assert ledger.residuals == ()

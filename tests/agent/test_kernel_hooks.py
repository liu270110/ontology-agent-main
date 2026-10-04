# tests/agent/test_kernel_hooks.py
"""H-0a hooks 机制测试（评审 2026-09-28 §4 补查批次；07 研究 §7.4/§332 ``on(event)`` 承诺）。

首批矩阵：pre/post_tool_call、on_kernel_event。纪律断言（hermes 同款）：observer-only、
唯一 block 点收敛 pre_tool_call；hook 无法改写 GateReport/判据/StepState（签名只收
冻结值对象的防御性副本，从不接触门禁报告与步状态——防 07 §6.7-2 校验器私有化）。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.execution import ExecutionStage
from services.agent.business.kernel.hooks import ALLOW, Block, HookName, HookRegistry
from services.agent.business.kernel.loop import AgentKernel
from services.agent.business.kernel.run_context import RunContext
from services.agent.domain.model.kernel_actions import ToolResult
from services.agent.domain.model.kernel_context import KernelEvent, TenantContext
from services.agent.domain.model.step_state import LoopStage, StepState, StepStatus
from services.agent.domain.model.task import RunStatus
from services.platform.errors import ErrorCode
from tests.agent.conftest import (
    FakePlanner,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)

# 单步成功运行的锚点事件名序列（grounding 三条 + planning + gate + observation + settlement；
# M4.5-B 增 kernel.prefix_fingerprint：前缀指纹随组装落账/广播，additive 锚点事件）
_ANCHOR_SEQUENCE = [
    "kernel.grounded",
    "kernel.context_assembled",
    "kernel.prefix_fingerprint",
    "kernel.planned",
    "kernel.gated",
    "kernel.step_validated",
    "kernel.settled",
]


def _dispatcher_with_tool() -> ExtensionDispatcher:
    """单工具 + 单步计划的标准分发器（含 FakePlanner，走模板规划路径）。"""
    return make_tool_dispatcher(
        FakeTool(),
        register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),),
    )


def _make_event(event_type: str = "kernel.grounded") -> KernelEvent:
    return KernelEvent(
        event_type=event_type,
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        trace_id="trace-hooks-test",
    )


# ── 注册面：闭合枚举 / 幂等 / 空注册 ─────────────────────────────────────


def test_on注册面_未登记事件名即拒_闭合枚举fail_fast():
    # Arrange
    dispatcher = make_tool_dispatcher(None)
    # Act / Assert：H-0a 全矩阵其余项（如 transform_llm_output）未入首批，注册即 ValueError
    with pytest.raises(ValueError):
        dispatcher.register_hook("transform_llm_output", lambda *args: None)


def test_空注册判空fast_path_取用面为空元组():
    # Arrange
    registry = HookRegistry()
    # Act / Assert：调用点以这些属性判空（无 hook 的运行零分配零调用）
    assert registry.pre_tool_call_hooks == ()
    assert registry.post_tool_call_hooks == ()
    assert registry.on_kernel_event_hooks == ()


async def test_同一hook重复注册幂等_每事件至多通知一次():
    # Arrange
    dispatcher = _dispatcher_with_tool()
    seen: list[str] = []

    def observer(event: KernelEvent) -> None:
        seen.append(event.event_type)

    dispatcher.register_hook(HookName.ON_KERNEL_EVENT, observer)
    dispatcher.register_hook(HookName.ON_KERNEL_EVENT, observer)  # 同函数重复注册（no-op）
    kernel = AgentKernel(dispatcher)
    # Act
    await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    # Assert：观察次数恰等于账本事件数（第二次注册未多通知）
    ledger = kernel.last_ledger
    assert ledger is not None
    assert len(seen) == len(ledger.events)


# ── pre_tool_call：唯一 block 点 ────────────────────────────────────────


async def test_pre_tool_call阻断_工具不执行_步走失败分支():
    # Arrange
    tool = FakeTool()
    dispatcher = make_tool_dispatcher(tool, register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),))
    dispatcher.register_hook(HookName.PRE_TOOL_CALL, lambda call, ctx: Block(reason="策略禁止：非工作时段执行该行动"))
    kernel = AgentKernel(dispatcher)
    # Act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    # Assert：工具未执行、run 失败（结构化拒绝回流=可修复失败分支）、调用闭合、审计留痕
    assert tool.calls == []  # 工具不执行（block 生效）
    assert outcome.status == str(RunStatus.FAILED)
    assert outcome.terminal_states[0].status is StepStatus.FAILED  # 步走失败分支
    ledger = kernel.last_ledger
    assert ledger is not None
    refused = [e for e in ledger.events if e.event_type == "kernel.hook_refused"]
    assert len(refused) == 1  # 记审计事件
    assert refused[0].data["reason"].startswith("策略禁止")
    assert all(rec.closed for rec in ledger.tool_calls)  # C1 不变式：拒绝路径调用亦闭合


async def test_pre_tool_call阻断_合成结构化拒绝带hook_refusal标记():
    # Arrange（执行阶段级：直接断言落入库的 StepResult/ToolResult 形态）
    tool = FakeTool()
    dispatcher = make_tool_dispatcher(tool, register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),))
    dispatcher.register_hook(
        HookName.PRE_TOOL_CALL,
        lambda call, ctx: Block(reason="高危行动", structured_message="该行动已被钩子策略拒绝，请改用只读通道"),
    )
    emitted: list[str] = []
    stage = ExecutionStage(dispatcher, lambda *args: emitted.append(args[3]))
    task, ctx = make_task(), make_ctx()
    rc = RunContext(task, ctx, Budget(max_steps=5), clock=lambda: 0.0, approvals=())
    rc.states[1] = StepState(
        run_id=task.run_id, seq=1, stage=LoopStage.GATE, status=StepStatus.GATED, action_iri=make_step().action_iri
    )
    # Act
    await stage.run(rc, make_step())
    # Assert
    result = rc.results[1]
    assert result.ok is False
    assert result.tool_result is not None
    assert result.tool_result.output["hook_refusal"] is True  # hook_refusal 标记
    assert result.tool_result.output["message"] == "该行动已被钩子策略拒绝，请改用只读通道"
    assert result.tool_result.error_code == int(ErrorCode.SCOPE_INSUFFICIENT)
    assert tool.calls == []  # 工具不执行
    assert "kernel.hook_refused" in emitted  # 审计事件经 Emit 落账


async def test_pre_tool_call放行_工具正常执行():
    # Arrange
    tool = FakeTool()
    dispatcher = make_tool_dispatcher(tool, register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),))
    dispatcher.register_hook(HookName.PRE_TOOL_CALL, lambda call, ctx: ALLOW)
    kernel = AgentKernel(dispatcher)
    # Act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    # Assert：放行路径零影响
    assert outcome.status == str(RunStatus.COMPLETED)
    assert len(tool.calls) == 1


async def test_pre_tool_call钩子异常_降级放行_安全权威在门禁基线():
    # Arrange
    tool = FakeTool()

    def boom(call, ctx):  # type: ignore[no-untyped-def]
        raise RuntimeError("钩子崩溃")

    dispatcher = make_tool_dispatcher(tool, register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),))
    dispatcher.register_hook(HookName.PRE_TOOL_CALL, boom)
    kernel = AgentKernel(dispatcher)
    # Act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    # Assert：异常不劫持执行面（hook 不构成第二安全面，安全权威恒在门禁基线 B1）
    assert outcome.status == str(RunStatus.COMPLETED)
    assert len(tool.calls) == 1


async def test_hook只拿到入参副本_改写不影响工具真实入参():
    # Arrange：observer 纪律——hook 无法改写判据/状态/真实调用参数（只可 Block）
    tool = FakeTool()

    def tamper(call, ctx):  # type: ignore[no-untyped-def]
        call.parameters["q"] = "被钩子篡改"  # 只能改到防御性副本
        return ALLOW

    dispatcher = make_tool_dispatcher(tool, register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),))
    dispatcher.register_hook(HookName.PRE_TOOL_CALL, tamper)
    kernel = AgentKernel(dispatcher)
    # Act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    # Assert：工具收到原始参数（值不经采样/不经钩子改写）
    assert tool.calls[0].parameters["q"] == "线路A"
    assert outcome.status == str(RunStatus.COMPLETED)


# ── post_tool_call：observer ───────────────────────────────────────────


async def test_post_tool_call观察者抛异常_结果不受影响():
    # Arrange
    def bad_observer(result, ctx):  # type: ignore[no-untyped-def]
        raise RuntimeError("observer 崩溃")

    dispatcher = _dispatcher_with_tool()
    dispatcher.register_hook(HookName.POST_TOOL_CALL, bad_observer)
    kernel = AgentKernel(dispatcher)
    # Act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    # Assert：异常吞掉留 WARNING，步照常 validated、run 照常 completed
    assert outcome.status == str(RunStatus.COMPLETED)
    assert outcome.terminal_states[0].status is StepStatus.VALIDATED


async def test_post_tool_call观察者收到执行后的最终结果():
    # Arrange
    seen: list[ToolResult] = []

    def observer(result: ToolResult, ctx: TenantContext) -> None:
        seen.append(result)

    dispatcher = _dispatcher_with_tool()
    dispatcher.register_hook(HookName.POST_TOOL_CALL, observer)
    kernel = AgentKernel(dispatcher)
    # Act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    # Assert：observer 收到的是结果管线（B3/C3/截断）完成后的最终 ToolResult
    assert outcome.status == str(RunStatus.COMPLETED)
    assert len(seen) == 1
    assert seen[0].ok is True and seen[0].output == {"rows": 3}


# ── on_kernel_event：observer 广播 ──────────────────────────────────────


async def test_on_kernel_event观察者收到锚点事件名序列():
    # Arrange
    dispatcher = _dispatcher_with_tool()
    seen: list[str] = []
    dispatcher.register_hook(HookName.ON_KERNEL_EVENT, lambda event: seen.append(event.event_type))
    kernel = AgentKernel(dispatcher)
    # Act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    # Assert：锚点事件按落账次序广播（grounded→assembled→planned→gated→validated→settled）
    assert outcome.status == str(RunStatus.COMPLETED)
    assert seen == _ANCHOR_SEQUENCE


async def test_异步on_kernel_event观察者亦收到广播_异常不外泄():
    # Arrange
    seen: list[str] = []

    async def observer(event: KernelEvent) -> None:
        await asyncio.sleep(0)  # 确证走异步派发路径（ensure_future 后台任务）
        seen.append(event.event_type)

    async def boom(event: KernelEvent) -> None:
        raise RuntimeError("异步 observer 崩溃")

    dispatcher = _dispatcher_with_tool()
    dispatcher.register_hook(HookName.ON_KERNEL_EVENT, observer)
    dispatcher.register_hook(HookName.ON_KERNEL_EVENT, boom)
    kernel = AgentKernel(dispatcher)
    # Act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    await dispatcher.hooks.drain_observers()  # fire-and-forget 收尾（测试辅助）
    # Assert：异步 observer 同序收到全部事件；崩溃 observer 不影响运行与其余 observer
    assert outcome.status == str(RunStatus.COMPLETED)
    assert seen == _ANCHOR_SEQUENCE


async def test_广播嵌套守卫_hook回调内再触发广播被丢弃():
    # Arrange：hook 回调禁止再调内核——违规尝试再触发广播时守卫生效（无递归风暴）
    registry = HookRegistry()
    calls: list[str] = []

    def observer(event: KernelEvent) -> None:
        calls.append(event.event_type)
        registry.broadcast_kernel_event(event)  # 违规嵌套广播（应被丢弃，事件已落账不丢）

    registry.register(HookName.ON_KERNEL_EVENT, observer)
    # Act
    registry.broadcast_kernel_event(_make_event("kernel.grounded"))
    # Assert：外层一次生效、嵌套一次丢弃
    assert calls == ["kernel.grounded"]

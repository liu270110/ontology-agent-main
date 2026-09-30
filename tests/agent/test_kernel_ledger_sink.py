# tests/agent/test_kernel_ledger_sink.py
"""C1 内核账本投影（sink）测试（2026-09-27 批：锚点事件 PG 落账的内核侧契约）。

覆盖：sink 捕获全部 kernel.* 锚点事件（终态前排水=断言确定性）、投影失败不阻断运行、
无 sink 行为不变、取消路径亦排水。
"""

from __future__ import annotations

import asyncio

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_actions import ToolResult
from services.agent.domain.model.kernel_context import KernelEvent, TrustLevel
from tests.agent.conftest import (
    FakePlanner,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)

_BUDGET = Budget(max_tokens=10_000, max_steps=5, duration_s=30.0)


def _kernel_with_sink(sink) -> AgentKernel:
    dispatcher = make_tool_dispatcher(FakeTool())
    dispatcher.register_planning_strategy(FakePlanner(make_candidate((make_step(),))))
    return AgentKernel(dispatcher)


async def test_sink_捕获全部锚点事件_终态前排水():
    received: list[KernelEvent] = []

    async def sink(event: KernelEvent) -> None:
        received.append(event)

    outcome = await _kernel_with_sink(sink).run(make_task(), make_ctx(), budget=_BUDGET, ledger_sink=sink)
    assert outcome.status == "completed"
    types = [e.event_type for e in received]
    # 七阶段锚点（02 §11.1 A1：planned/gated/step_validated/settled）全部投影
    assert "kernel.planned" in types and "kernel.gated" in types
    assert "kernel.step_validated" in types and "kernel.settled" in types
    assert received[-1].event_type == "kernel.settled"  # 排水在终态前：最后一条=落账收口


async def test_sink_投影失败_不阻断运行():
    async def broken_sink(event: KernelEvent) -> None:
        raise RuntimeError("PG 不可用")

    outcome = await _kernel_with_sink(broken_sink).run(make_task(), make_ctx(), budget=_BUDGET, ledger_sink=broken_sink)
    assert outcome.status == "completed"  # 审计投影失败不阻断主流程（02 §3 ⑥ 纪律）


async def test_无sink_行为不变():
    outcome = await _kernel_with_sink(None).run(make_task(), make_ctx(), budget=_BUDGET)
    assert outcome.status == "completed"


async def test_取消路径_亦排水锚点():
    received: list[KernelEvent] = []

    async def sink(event: KernelEvent) -> None:
        await asyncio.sleep(0)  # 让出事件循环（真实 PG 写形态）
        received.append(event)

    class SlowTool(FakeTool):
        async def invoke(self, call, ctx, *, approval=None, timeout_ms=30_000):
            self.calls.append(call)
            try:
                await asyncio.sleep(30.0)
            except asyncio.CancelledError:
                return ToolResult(ok=True, output={}, trust_level=TrustLevel.AGENT_ATTESTED)  # 取消收尾闭合调用

    dispatcher = make_tool_dispatcher(SlowTool())
    dispatcher.register_planning_strategy(FakePlanner(make_candidate((make_step(),))))
    kernel = AgentKernel(dispatcher)
    task = make_task()

    async def run_cancellable():
        await kernel.run(task, make_ctx(), budget=_BUDGET, ledger_sink=sink)

    run_task = asyncio.ensure_future(run_cancellable())
    await asyncio.sleep(0.05)  # 进入执行阶段
    run_task.cancel()
    try:
        await run_task
    except asyncio.CancelledError:
        pass
    assert received, "取消路径锚点亦投影（C1 不丢账）"
    assert any(e.event_type == "kernel.gated" for e in received)

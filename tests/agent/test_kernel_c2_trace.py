# tests/agent/test_kernel_c2_trace.py
"""C2 trace/审计/成本记账负向测试（02 §2 C2）：trace_id 贯穿、事件入账校验归属。"""

from __future__ import annotations

import uuid

import pytest
from conftest import FakePlanner, FakeTool, make_candidate, make_ctx, make_step, make_task, make_tool_dispatcher

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.errors import KernelContractError
from services.agent.business.kernel.ledger import KernelLedger
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_context import KernelEvent


async def test_空trace_id拒绝运行():
    kernel = AgentKernel(make_tool_dispatcher(FakeTool()))
    with pytest.raises(KernelContractError, match="trace_id"):
        await kernel.run(make_task(), make_ctx(trace_id=""), budget=Budget(max_steps=1, duration_s=5))


def test_跨trace事件入账被拒():
    ledger = KernelLedger(tenant_id=make_ctx().tenant_id, trace_id="trace-a")
    rogue = KernelEvent(
        event_type="x.y",
        tenant_id=ledger.tenant_id,
        run_id=uuid.uuid4(),
        trace_id="trace-b",
    )
    with pytest.raises(KernelContractError, match="trace_id"):
        ledger.append_event(rogue)


def test_跨租户事件入账被拒():
    ledger = KernelLedger(tenant_id=make_ctx().tenant_id, trace_id="trace-a")
    other_tenant = make_ctx().tenant_id
    rogue = KernelEvent(event_type="x.y", tenant_id=other_tenant, run_id=uuid.uuid4(), trace_id="trace-a")
    with pytest.raises(KernelContractError, match="租户"):
        ledger.append_event(rogue)


async def test_主路径全程事件带trace_每步落账():
    kernel = AgentKernel(
        make_tool_dispatcher(
            FakeTool(),
            register_planning_strategy=(
                FakePlanner(
                    make_candidate((make_step(), make_step(seq=2))),
                ),
            ),
        )
    )
    ctx = make_ctx(trace_id="trace-c2-main")
    outcome = await kernel.run(make_task(), ctx, budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == "completed"
    ledger = kernel.last_ledger
    assert ledger is not None
    assert ledger.events, "全程零事件=黑盒，违反 C2"
    for event in ledger.events:  # 每个事件可追溯：trace/tenant/run 三归属齐备
        assert event.trace_id == "trace-c2-main"
        assert event.tenant_id == ctx.tenant_id
        assert event.data  # 审计载荷非空
    assert len(ledger.steps) >= 6  # 每阶段产出 StepState：2 步 ×（计划+门禁+观察）快照

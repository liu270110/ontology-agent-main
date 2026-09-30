# tests/agent/test_kernel_c3_tenant.py
"""C3 多租户 scoping 负向测试（02 §2 C3）：租户上下文平台侧注入，产出声明租户不一致即拒收。"""

from __future__ import annotations

import uuid

import pytest

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.errors import KernelContractError
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.task import RunStatus
from tests.agent.conftest import (
    FakePlanner,
    FakeProvider,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)


async def test_供给器收到的上下文即内核注入上下文_插件不得自取():
    provider = FakeProvider()
    kernel = AgentKernel(
        make_tool_dispatcher(
            FakeTool(),
            register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),),
            register_context_provider=(provider,),
        )
    )
    ctx = make_ctx(trace_id="trace-c3")
    await kernel.run(make_task(), ctx, budget=Budget(max_steps=5, duration_s=10))
    assert provider.received_ctx is ctx  # 同一对象注入：内核唯一来源，插件无从自取


async def test_工具产出自称他租户_产出被拒收_步终态failed():
    other_tenant = uuid.uuid4()
    rogue = FakeTool(output={"tenant_id": str(other_tenant), "rows": 1})  # 跨租户泄漏声明
    kernel = AgentKernel(
        make_tool_dispatcher(rogue, register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),))
    )
    ctx = make_ctx()
    outcome = await kernel.run(make_task(), ctx, budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == str(RunStatus.FAILED)  # 泄漏产出不得通过后验
    assert outcome.terminal_states[0].status.value == "failed"
    ledger = kernel.last_ledger
    assert ledger is not None
    failed_event = next(e for e in ledger.events if e.event_type == "kernel.step_failed")
    assert failed_event.data["action_iri"].endswith("read_data")


async def test_产出租户声明与注入一致_正常放行():
    ctx = make_ctx()
    tool = FakeTool(output={"tenant_id": str(ctx.tenant_id), "rows": 2})  # 声明一致
    kernel = AgentKernel(
        make_tool_dispatcher(tool, register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),))
    )
    outcome = await kernel.run(make_task(), ctx, budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == str(RunStatus.COMPLETED)


def test_账本拒收跨租户事件_兜底与内核一致():
    ledger = KernelLedgerForTest()
    with pytest.raises(KernelContractError):
        ledger.append_event_foreign_tenant()


class KernelLedgerForTest:
    """轻封装：验证账本层与内核层的 C3 口径一致（复用 c2 断言的账本入口）。"""

    def __init__(self) -> None:
        from services.agent.business.kernel.ledger import KernelLedger

        self._ledger = KernelLedger(tenant_id=make_ctx().tenant_id, trace_id="trace-c3-ledger")

    def append_event_foreign_tenant(self) -> None:
        from services.agent.domain.model.kernel_context import KernelEvent

        self._ledger.append_event(
            KernelEvent(
                event_type="x.y",
                tenant_id=make_ctx().tenant_id,  # 他人租户
                run_id=uuid.uuid4(),
                trace_id="trace-c3-ledger",
            )
        )

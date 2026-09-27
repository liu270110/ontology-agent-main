# tests/agent/test_kernel_cancellation.py
"""取消完整性验收测试（02 §2.4）：清单化传播、5s 强制兜底、零残留、终态可追溯。"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from conftest import (
    CODE_ACTION_IRI,
    FakeBackend,
    FakePlanner,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)

from services.agent.business.kernel import cancellation as cancellation_module
from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.cancellation import CancellationCoordinator
from services.agent.business.kernel.gate_baseline import canonical_param_hash
from services.agent.business.kernel.ledger import KernelLedger
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_actions import ApprovalTicket, ExecutionMode
from services.agent.domain.model.step_state import StepStatus

_CODE_SCHEMA = {"properties": {"code": {"type": "string"}}}


def _code_step() -> make_step.__annotations__.get("x", object):
    return make_step(
        seq=1,
        action_iri=CODE_ACTION_IRI,
        mode=ExecutionMode.CODE,
        params={"code": "loop"},
        scopes=(),
        schema=_CODE_SCHEMA,
    )


@pytest.fixture(autouse=True)
def _fast_item_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """清单项 5s 兜底在测试内压缩为 0.2s（语义不变：超时即强制）。"""
    monkeypatch.setattr(cancellation_module, "ITEM_TIMEOUT_S", 0.2)


async def test_取消传播_在途调用以取消错误闭合_步落cancelled终态():
    tool = FakeTool(sleep_s=10.0)  # 会被取消收尾的正常实现
    kernel = AgentKernel(
        make_tool_dispatcher(tool, register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),))
    )
    run_task = asyncio.create_task(kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=60)))
    await asyncio.sleep(0.05)  # 让运行进入在途工具调用
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task  # 取消语义保留：清理完毕后重抛
    ledger = kernel.last_ledger
    assert ledger is not None
    assert ledger.open_call_ids() == ()  # 未闭合调用已以取消错误闭合（04 篇不变式）
    assert all(rec.closed_as_cancelled for rec in ledger.tool_calls)
    assert ledger.steps[-1].status is StepStatus.CANCELLED  # 终态可追溯（资源释放先行）
    assert ledger.residuals == ()  # 零残留
    cancelled_event = next(e for e in ledger.events if e.event_type == "kernel.cancelled")
    assert cancelled_event.data["forced"] == []


async def test_取消含exclusive租约的任务_租约表零残留():
    backend = FakeBackend(run_sleep_s=10.0)
    planner = FakePlanner(
        make_candidate(
            (_code_step(),)  # type: ignore[arg-type]
        )
    )
    kernel = AgentKernel(
        make_tool_dispatcher(FakeTool(), register_planning_strategy=(planner,), register_execution_backend=(backend,))
    )
    ticket = ApprovalTicket(param_hash=canonical_param_hash({"code": "loop"}))
    run_task = asyncio.create_task(
        kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=60), approvals=(ticket,))
    )
    await asyncio.sleep(0.05)  # 进入沙箱执行（租约已登记）
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert backend.active_leases == []  # 验收：取消后租约表零残留（清单步骤 3 强制释放）


async def test_取消时租约释放卡死_5s超限被强制_清单继续走完_残留登记():
    backend = FakeBackend(run_sleep_s=10.0, hang_release=True)  # 持有方不肯优雅释放
    planner = FakePlanner(
        make_candidate(
            (_code_step(),)  # type: ignore[arg-type]
        )
    )
    kernel = AgentKernel(
        make_tool_dispatcher(FakeTool(), register_planning_strategy=(planner,), register_execution_backend=(backend,))
    )
    ticket = ApprovalTicket(param_hash=canonical_param_hash({"code": "loop"}))
    run_task = asyncio.create_task(
        kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=60), approvals=(ticket,))
    )
    await asyncio.sleep(0.05)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    ledger = kernel.last_ledger
    assert ledger is not None
    cancelled_event = next(e for e in ledger.events if e.event_type == "kernel.cancelled")
    assert any(item.startswith("exclusive_leases/") for item in cancelled_event.data["forced"])
    assert ledger.residuals  # 未竟清单项交回收任务补扫（登记在案）
    assert ledger.steps[-1].status is StepStatus.CANCELLED  # 强制后仍落终态（不可被取消卡死）
    await asyncio.sleep(0.05)  # 让后台僵尸协程退出，避免事件循环悬挂告警


async def test_工具调用卡住取消_吞取消实现_清单强制兜底():
    zombie = FakeTool(zombie=True)  # 吞掉第一波取消的「不可中止」实现
    kernel = AgentKernel(
        make_tool_dispatcher(zombie, register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),))
    )
    run_task = asyncio.create_task(kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=60)))
    await asyncio.sleep(0.05)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    ledger = kernel.last_ledger
    assert ledger is not None
    assert ledger.open_call_ids() == ()  # 强制兜底后调用仍以取消错误闭合
    assert ledger.steps[-1].status is StepStatus.CANCELLED
    await asyncio.sleep(1.3)  # 僵尸实现自行收尾，避免悬挂


async def test_清单顺序_子Run级联先于租约释放():
    ledger = KernelLedger(tenant_id=uuid.uuid4(), trace_id="trace-cancel-order")
    order: list[str] = []

    async def child_hook() -> None:
        order.append("child")

    async def lease_hook() -> None:
        order.append("lease")

    async def workspace_hook() -> None:
        order.append("workspace")

    coordinator = CancellationCoordinator(ledger)
    coordinator.register_child_run("sub-1", child_hook)
    coordinator.register_lease("lease-1", lease_hook)
    coordinator.register_workspace(workspace_hook)
    report = await coordinator.execute(reason="test")
    assert report.finished
    assert order == ["child", "lease", "workspace"]  # 子 Run → 租约 → 工作区（固定清单序）


async def test_子Run取消钩子异常转义_清单不中断():
    ledger = KernelLedger(tenant_id=uuid.uuid4(), trace_id="trace-cancel-exc")

    async def broken_hook() -> None:
        raise RuntimeError("子代理实现崩溃")

    coordinator = CancellationCoordinator(ledger)
    coordinator.register_child_run("bad", broken_hook)
    report = await coordinator.execute(reason="test")
    assert not report.finished
    assert "child_runs/bad" in report.forced
    assert ledger.residuals  # 异常转义留痕

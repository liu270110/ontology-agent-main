# tests/agent/test_kernel_b5_approval.py
"""B5 审批路由负向测试（02 §2 B5）：参数哈希绑定、waiting_approval 支、超时默认拒绝。

W2-2b（2026-10-07）：缺回执缺省行为改为**挂起等待裁决**（run 落 waiting_tool，见
test_approval_suspend.py）；本文件断言的「立即 FAILED 默认拒绝」语义经
``kernel_approval_suspend=False`` 显式钉住（=开关关回退面，行为与 2026-10-07 前逐字一致）。
"""

from __future__ import annotations

import pytest

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.gate_baseline import canonical_param_hash
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_actions import ApprovalTicket, ExecutionMode
from services.agent.domain.model.step_state import StepStatus
from services.agent.domain.model.task import RunStatus
from tests.agent.conftest import (
    WRITE_ACTION_IRI,
    FakePlanner,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)


def _patch_suspend_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """钉 W2-2b 开关为关（默认拒绝语义回归面）：loop 与 execution 消费命名空间同源替换。"""
    from services.platform.config import Settings

    fake = Settings(kernel_approval_suspend=False)
    monkeypatch.setattr("services.agent.business.kernel.loop.get_settings", lambda: fake)
    monkeypatch.setattr("services.agent.business.kernel.execution.get_settings", lambda: fake)


def _write_step() -> object:
    return make_step(seq=1, action_iri=WRITE_ACTION_IRI, mode=ExecutionMode.EXTERNAL_WRITE)


async def test_external_write缺审批回执_waiting_approval后默认拒绝(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_suspend_off(monkeypatch)
    tool = FakeTool(action_iri=WRITE_ACTION_IRI)
    planner = FakePlanner(make_candidate((_write_step(),)))  # type: ignore[arg-type]
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    state = outcome.terminal_states[0]
    assert state.status is StepStatus.FAILED
    assert "默认拒绝" in (state.error or "")
    assert tool.calls == []  # 未获回执：工具从未被调用
    ledger = kernel.last_ledger
    assert ledger is not None
    assert any(e.event_type == "kernel.approval_denied" for e in ledger.events)
    # 状态机走完 waiting_approval 支：账本步快照含 waiting_approval
    assert any(s.status == StepStatus.WAITING_APPROVAL for s in ledger.steps)


async def test_审批回执参数哈希绑定_换参重放视同未获审批_默认拒绝(monkeypatch: pytest.MonkeyPatch) -> None:
    """持旧回执换新参数重放：参数哈希不匹配 ⇒ 内核不认该回执，走 waiting_approval→默认拒绝。"""
    _patch_suspend_off(monkeypatch)
    tool = FakeTool(action_iri=WRITE_ACTION_IRI)
    planner = FakePlanner(
        make_candidate(
            (
                make_step(  # type: ignore[list-item]
                    seq=1,
                    action_iri=WRITE_ACTION_IRI,
                    mode=ExecutionMode.EXTERNAL_WRITE,
                    params={"q": "换参后的新参数"},
                ),
            )
        )
    )
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))
    stale_ticket = ApprovalTicket(param_hash="与调用无关的旧哈希")
    outcome = await kernel.run(
        make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10), approvals=(stale_ticket,)
    )
    state = outcome.terminal_states[0]
    assert state.status is StepStatus.FAILED
    assert "默认拒绝" in (state.error or "")  # 哈希不匹配 ⇒ 回执无效 ⇒ B5 默认拒绝
    assert tool.calls == []


async def test_携有效回执_审批通过_执行完成():
    params = {"q": "审批通过的工单"}
    tool = FakeTool(action_iri=WRITE_ACTION_IRI)
    planner = FakePlanner(
        make_candidate(
            (
                make_step(  # type: ignore[list-item]
                    seq=1, action_iri=WRITE_ACTION_IRI, mode=ExecutionMode.EXTERNAL_WRITE, params=params
                ),
            )
        )
    )
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))
    ticket = ApprovalTicket(param_hash=canonical_param_hash(params), approved_by=None)
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10), approvals=(ticket,))
    assert outcome.status == str(RunStatus.COMPLETED)
    assert len(tool.calls) == 1
    ledger = kernel.last_ledger
    assert ledger is not None
    # 审批支完整留痕：waiting_approval 快照存在，最终 validated
    assert any(s.status == StepStatus.WAITING_APPROVAL for s in ledger.steps)
    assert outcome.terminal_states[0].status is StepStatus.VALIDATED

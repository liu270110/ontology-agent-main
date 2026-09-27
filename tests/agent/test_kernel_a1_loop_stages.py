# tests/agent/test_kernel_a1_loop_stages.py
"""A1 七阶段主循环负向测试（02 §2 A1）：阶段次序受内核断言、未过门禁不得执行、无计划不得执行。"""

from __future__ import annotations

import pytest
from conftest import (
    ACTION_IRI,
    FakeModel,
    FakePlanner,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.step_state import LoopStage, StepStatus
from services.agent.domain.model.task import RunStatus


async def test_七阶段主路径_全步validated并闭环落账():
    tool = FakeTool()
    kernel = AgentKernel(
        make_tool_dispatcher(tool, register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),)),
    )
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == str(RunStatus.COMPLETED)
    assert [s.status for s in outcome.terminal_states] == [StepStatus.VALIDATED]
    assert tool.calls and tool.calls[0].action_iri == ACTION_IRI
    ledger = kernel.last_ledger
    assert ledger is not None
    event_types = [e.event_type for e in ledger.events]
    # 七阶段事件锚点：装载/组装/规划/门禁/观察/沉淀 全程留痕（感知与检索=前两锚点）
    for anchor in (
        "kernel.grounded",
        "kernel.context_assembled",
        "kernel.planned",
        "kernel.gated",
        "kernel.step_validated",
        "kernel.settled",
    ):
        assert anchor in event_types
    # 每阶段产出 StepState：账本步快照覆盖 planned→gated→executing→validated 全程
    snapshot_statuses = [s.status for s in ledger.steps]
    assert snapshot_statuses.count(StepStatus.PLANNED) == 1
    assert snapshot_statuses.count(StepStatus.GATED) == 1
    assert snapshot_statuses.count(StepStatus.VALIDATED) == 1
    # 终态步的阶段=观察（后验产出），可恢复点已立（C1 重连续跑锚点）
    assert outcome.terminal_states[0].stage == LoopStage.OBSERVATION
    assert outcome.terminal_states[0].resumable is True


async def test_门禁拒绝的步_直接进failed终态_不得执行():
    tool = FakeTool()
    # scope 不足（ctx 无 tool.exec）→ 基线 R3 拒绝
    kernel = AgentKernel(
        make_tool_dispatcher(tool, register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),)),
    )
    ctx = make_ctx(scopes=())
    outcome = await kernel.run(make_task(), ctx, budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == str(RunStatus.FAILED)
    state = outcome.terminal_states[0]
    assert state.status is StepStatus.FAILED
    assert state.stage == LoopStage.GATE  # 终态由门禁阶段产出
    assert state.gate_verdict == "reject"
    assert tool.calls == []  # 未过门禁：执行阶段从未触达工具
    assert outcome.reason_code == 3001


async def test_计划引用未绑定行动类_三层校验拒绝_无步可执行():
    kernel = AgentKernel(make_tool_dispatcher(None))  # 无任何绑定
    kernel2 = AgentKernel(
        make_tool_dispatcher(
            None,
            register_planning_strategy=(
                FakePlanner(make_candidate((make_step(action_iri="http://ontology.example/action/未注册"),))),
            ),
        )
    )
    outcome = await kernel2.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == str(RunStatus.FAILED)
    assert "绑定层" in outcome.reason
    assert outcome.terminal_states == ()
    _ = kernel


async def test_既无规划策略也无模型_规划阶段拒绝():
    kernel = AgentKernel(make_tool_dispatcher(None))
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == str(RunStatus.FAILED)
    assert "规划" in outcome.reason


async def test_模型回退规划_结构化产物经校验成计划():
    model = FakeModel({"steps": [{"seq": 1, "action_iri": ACTION_IRI, "execution_mode": "read"}]})
    kernel = AgentKernel(make_tool_dispatcher(FakeTool(), register_model=(model,)))
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == str(RunStatus.COMPLETED)
    assert outcome.terminal_states[0].status is StepStatus.VALIDATED
    assert model.calls == 1  # ModelPort（平台既有端口）经分发器接通内核规划阶段


def test_非法迁移由状态机拒绝_循环内不可能跳过门禁执行():
    # A1×A2 交叉断言：直接把 planned 步推给执行阶段会被状态机拒绝（负向）
    from uuid import uuid4

    from services.agent.domain.model.step_state import StepState, StepStateError

    state = StepState(run_id=uuid4(), seq=1, stage=LoopStage.EXECUTION)
    with pytest.raises(StepStateError):
        state.transition(StepStatus.EXECUTING)  # planned → executing 非法（必经 gated）

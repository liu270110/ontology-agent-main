# tests/agent/test_kernel_step_snapshot.py
"""K33 A-4 半级：步初冻结快照验收测试（docs/Agent/13 §39；上游=研究整理/12 对标 01-codex §2）。

FrozenStepContext=串行单步段步初（register_step/nudge 注入前）对运行面的一次性冻结值对象，
经 rc.last_step_snapshot / kernel.last_run_context 可查（K11 watermark 取数口惯例）；门禁
事件 payload 附 snapshot_seq 锚（additive）。半级边界：不改变任何执行语义——rc.states 对象
身份、rc.results 写回、段边界三写点（splice/水位复判/nudge）原样保留；并行多步段不刷新快照。
"""

from __future__ import annotations

import dataclasses
import time
import uuid

import pytest

from services.agent.business.kernel.budget import Budget, BudgetWatermark
from services.agent.business.kernel.loop import AgentKernel
from services.agent.business.kernel.run_context import FrozenStepContext, RunContext
from services.agent.domain.model.kernel_actions import StepResult
from services.agent.domain.model.kernel_context import ContextBlock, TrustLevel
from services.agent.domain.model.step_state import LoopStage, StepState, StepStatus, deterministic_step_id
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


def _serial_kernel(steps: tuple, *, provider: FakeProvider | None = None) -> AgentKernel:
    """串行 READ 步内核（make_step 缺省 parallelizable=False → 恒单步段=串行路径）。"""
    extra = {"register_planning_strategy": (FakePlanner(make_candidate(steps)),)}
    dispatcher = make_tool_dispatcher(FakeTool(), **extra)
    if provider is not None:
        dispatcher.register_context_provider(provider)
    return AgentKernel(dispatcher)


async def test_步初快照构造字段全_含step_id确定性():
    """K33-a：快照六字段齐全且为步初纯净态基线——step_id=K32 确定性派生（run_id+seq
    uuid5）；state_snapshot=步初 planned 深拷贝（与终态对象脱钩）；watermark 步数未计；
    results_count=0；context_blocks=组装面 nudge 前引用快照。"""
    task = make_task()
    provider = FakeProvider()
    kernel = _serial_kernel((make_step(),), provider=provider)
    outcome = await kernel.run(task, make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == str(RunStatus.COMPLETED)

    rc = kernel.last_run_context
    snap = rc.last_step_snapshot
    assert snap is not None
    # 六字段（frozen dataclass）：步号/K32 派生 id/组装面快照/状态深拷贝/水位/结果计数
    fields = {f.name for f in dataclasses.fields(FrozenStepContext)}
    assert fields == {"step_seq", "step_id", "context_blocks", "state_snapshot", "watermark", "results_count"}
    assert snap.step_seq == 1
    assert snap.step_id == deterministic_step_id(str(task.run_id), 1)  # K32 同 run 同 seq 恒等
    # 步初状态基线：planned（快照先于门禁/执行），与终态 validated 步非同一对象（深拷贝脱钩）
    assert snap.state_snapshot.status is StepStatus.PLANNED
    assert snap.state_snapshot.stage is LoopStage.PLANNING
    assert outcome.terminal_states[0].status is StepStatus.VALIDATED
    assert snap.state_snapshot is not outcome.terminal_states[0]
    # 步初水位：frozen 值对象、步数预算未计（步后 add_step 才累计）
    assert isinstance(snap.watermark, BudgetWatermark)
    assert snap.watermark.steps_done == 0
    # 步初组装面=供给器块（nudge 尚未注入），引用快照内容与来源一致
    assert len(snap.context_blocks) == 1
    assert snap.context_blocks[0].source == provider.meta.name
    assert snap.context_blocks[0].content == "图谱检索结果"
    assert snap.results_count == 0


async def test_快照隔离_构造后步内rc变更不回渗快照():
    """K33-a 隔离性（单元级）：按 loop 同构口径构造快照后改 rc（组装面追加/状态迁移/
    结果累计），快照各字段保持原值——frozen 值对象+深拷贝+引用快照三重脱钩。"""
    run_id = uuid.uuid4()
    state = StepState(run_id=run_id, seq=1, stage=LoopStage.PLANNING)
    rc = RunContext(
        make_task(run_id=run_id),
        make_ctx(),
        Budget(max_steps=5, duration_s=10),
        clock=time.monotonic,
        approvals=(),
    )
    rc.states = {1: state}
    rc.context_blocks = ()  # 步初：空组装面
    rc.results = {}
    snapshot = FrozenStepContext(
        step_seq=1,
        step_id=str(rc.states[1].step_id),
        context_blocks=rc.context_blocks,
        state_snapshot=rc.states[1].model_copy(deep=True),
        watermark=rc.tracker.watermark(),
        results_count=len(rc.results),
    )
    # frozen 值对象：任何字段写操作被拒（dataclasses.FrozenInstanceError）
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.step_seq = 9
    # 步内变更：组装面换元追加、原状态对象迁移、结果累计——快照全部不回渗
    rc.context_blocks = (
        *rc.context_blocks,
        ContextBlock(source="user_steer", content="步内注入", tokens=1, trust_level=TrustLevel.AGENT_ATTESTED),
    )
    state.transition(StepStatus.VALIDATED, stage=LoopStage.OBSERVATION)
    rc.results[1] = StepResult(
        step_id=state.step_id, run_id=run_id, step_seq=1, action_iri="http://x", ok=True
    )
    assert snapshot.state_snapshot.status is StepStatus.PLANNED  # 深拷贝不随原对象迁移
    assert snapshot.context_blocks == ()  # 引用快照不随组装面换元
    assert snapshot.results_count == 0
    assert snapshot.step_id == deterministic_step_id(str(run_id), 1)  # K32 派生 id 恒等


async def test_nudge注入后快照保持步初纯净态_状态迁移不回渗():
    """K33-a 隔离性（内核级）：两步同签名——第 2 步 nudge 注入组装面，但第 2 步步初快照
    （构造点在 register_step 前）不含 nudge 块；步内状态迁移到 validated 也不回渗快照。
    nudge 属本步可见面，快照只记步初基线（§39 构造点裁决：纯净态）。"""
    kernel = _serial_kernel(tuple(make_step(seq=seq) for seq in (1, 2)))  # 同签名：第 2 步触发 nudge
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == str(RunStatus.COMPLETED)
    rc = kernel.last_run_context
    nudge_in_assembly = [b for b in rc.context_blocks if b.source == "kernel.loop_guard"]
    assert len(nudge_in_assembly) == 1  # 运行组装面最终含 1 块 nudge（现状行为不变）
    snap = rc.last_step_snapshot  # 第 2 步步初快照（nudge 注入前）
    assert snap.step_seq == 2
    assert not [b for b in snap.context_blocks if b.source == "kernel.loop_guard"]  # 快照无 nudge
    assert snap.state_snapshot.status is StepStatus.PLANNED  # 步初基线，不随执行迁移
    assert outcome.terminal_states[-1].status is StepStatus.VALIDATED  # 原对象已终态（对象身份保留）


async def test_last_run_context可查快照_gated事件附snapshot_seq():
    """K33-b：快照经 kernel.last_run_context 可查（K11 惯例）；多串行步逐覆盖，最终快照=
    最后串行步；kernel.gated payload 附 snapshot_seq 锚（additive）逐门禁对齐本步。"""
    steps = (make_step(seq=1, params={"q": "线路A"}), make_step(seq=2, params={"q": "线路B"}))
    kernel = _serial_kernel(steps)  # 参数不同→签名不同→零 nudge 干扰
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == str(RunStatus.COMPLETED)
    rc = kernel.last_run_context
    assert rc is not None and rc.last_step_snapshot is not None
    assert rc.last_step_snapshot.step_seq == 2  # 串行步逐步覆盖：最终=最后一步步初
    assert rc.last_step_snapshot.step_id == deterministic_step_id(str(outcome.run_id), 2)
    gated = [e for e in kernel.last_ledger.events if e.event_type == "kernel.gated"]
    assert [e.data["step_seq"] for e in gated] == [1, 2]
    assert [e.data["snapshot_seq"] for e in gated] == [1, 2]  # 每次门禁锚定当步步初快照

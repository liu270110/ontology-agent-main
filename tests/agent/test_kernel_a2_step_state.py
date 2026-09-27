# tests/agent/test_kernel_a2_step_state.py
"""A2 Run/Step 状态机负向测试（02 §2 A2；04 §3 权威状态机代码化）：非法迁移被拒绝。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from services.agent.domain.model.step_state import (
    BudgetWatermark,
    LoopStage,
    StepState,
    StepStateError,
    StepStatus,
)


def _state(status: StepStatus) -> StepState:
    return StepState(run_id=uuid.uuid4(), seq=1, stage=LoopStage.PLANNING, status=status)


def test_非法迁移_planned直达validated被拒_必经门禁与执行():
    state = _state(StepStatus.PLANNED)
    with pytest.raises(StepStateError):
        state.transition(StepStatus.VALIDATED)


def test_非法迁移_gated直达waiting_approval被拒():
    state = _state(StepStatus.GATED)
    with pytest.raises(StepStateError):
        state.transition(StepStatus.WAITING_APPROVAL)


def test_非法迁移_validated终态再迁移被拒():
    state = _state(StepStatus.VALIDATED)
    with pytest.raises(StepStateError):
        state.transition(StepStatus.EXECUTING)
    with pytest.raises(StepStateError):
        state.cancel()


def test_非法迁移_failed终态复活被拒():
    state = _state(StepStatus.FAILED)
    with pytest.raises(StepStateError):
        state.transition(StepStatus.PLANNED)


def test_合法迁移链_planned经门禁执行审批回环至validated():
    state = _state(StepStatus.PLANNED)
    state.transition(StepStatus.GATED, stage=LoopStage.GATE)
    state.transition(StepStatus.EXECUTING, stage=LoopStage.EXECUTION)
    state.transition(StepStatus.WAITING_APPROVAL, stage=LoopStage.EXECUTION)  # externalWrite/code 支
    state.transition(StepStatus.EXECUTING)  # 审批通过
    state.transition(StepStatus.VALIDATED, stage=LoopStage.OBSERVATION)
    assert state.is_terminal
    assert state.is_gate_passed


def test_门禁拒绝_gated直达failed():
    state = _state(StepStatus.GATED)
    state.transition(StepStatus.FAILED)
    assert state.is_terminal


def test_取消完整性_任意非终态可入cancelled_终态不可再取消():
    for status in (StepStatus.PLANNED, StepStatus.GATED, StepStatus.EXECUTING, StepStatus.WAITING_APPROVAL):
        state = _state(status)
        state.cancel(reason="清单化取消")
        assert state.status is StepStatus.CANCELLED
        assert state.error is not None
    with pytest.raises(StepStateError):
        _state(StepStatus.VALIDATED).cancel()


def test_预算水位快照_frozen不可原地改_替换式累计():
    watermark = BudgetWatermark(tokens_used=10, steps_done=1, duration_elapsed_s=0.5)
    with pytest.raises(ValueError):
        watermark.tokens_used = 20  # type: ignore[misc]  # frozen 校验：赋值即抛
    merged = watermark.merged(tokens=5, steps=1, elapsed_s=1.0)
    assert merged.tokens_used == 15
    assert merged.steps_done == 2
    assert watermark.tokens_used == 10  # 原快照不变（终态可追溯）


def test_步状态携带预算水位与门禁结论_终态可追溯():
    state = StepState(
        run_id=uuid.uuid4(),
        seq=3,
        stage=LoopStage.GATE,
        status=StepStatus.GATED,  # 已过门禁（进入执行前置态）
        budget_watermark=BudgetWatermark(tokens_used=42, steps_done=2),
        gate_verdict="allow",
        updated_at=datetime(2026, 9, 27, tzinfo=UTC),
    )
    state.transition(StepStatus.EXECUTING, stage=LoopStage.EXECUTION)
    assert state.budget_watermark.tokens_used == 42
    assert state.gate_verdict == "allow"

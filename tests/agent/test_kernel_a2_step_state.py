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
    deterministic_step_id,
)


def _state(status: StepStatus) -> StepState:
    return StepState(run_id=uuid.uuid4(), seq=1, stage=LoopStage.PLANNING, status=status)


def test_特批迁移_planned直达validated_仅限内核resume对账路径():
    """M4.5-A（docs/Agent/12 §1.3）：planned→validated 增特批白名单——**仅**内核 resume
    计划对账路径（锚点三元组全等且 READ，kernel.step_resumed_validated 审计承载）；
    非对账场景仍必经门禁与执行（执行旁路不存在，迁移本身不留旁路漏洞）。"""
    state = _state(StepStatus.PLANNED)
    state.transition(StepStatus.VALIDATED, stage=LoopStage.PLANNING)  # 特批迁移（对账命中步）
    assert state.is_terminal

    # gated 仍不得回退/跨迁 waiting_approval（既有负向面保持不变）
    gated = _state(StepStatus.GATED)
    with pytest.raises(StepStateError):
        gated.transition(StepStatus.WAITING_APPROVAL)


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


def test_预算水位合并_estimated口径标注随水位传递():
    watermark = BudgetWatermark(tokens_used=10, steps_done=1, duration_elapsed_s=0.5, estimated=True)
    # Act：累计合并（M4.5-B：聚合含估算口径的水位）
    merged = watermark.merged(tokens=5, steps=1, elapsed_s=1.0)
    # Assert：estimated 不回落默认 False（估算不得误报为实测，口径标注失真即消费方被误导）
    assert merged.estimated is True
    assert merged.tokens_used == 15  # 累计数值不受口径标注影响


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


# ── K32 A-5 确定性 step_id（docs/Agent/13 §38；恢复幂等根基）──────────────


def test_step_id_同run同seq派生稳定_两次构造等值():
    """K32-a：deterministic_step_id 纯函数两次调用等值；默认生成路径（不传 step_id）
    两次独立构造同 run 同 seq 得同一 id，且为合法 uuid 形态（UUID() 构造即证）。"""
    run_id = uuid.uuid4()
    first = StepState(run_id=run_id, seq=1, stage=LoopStage.PLANNING)
    second = StepState(run_id=run_id, seq=1, stage=LoopStage.PLANNING)
    expected = uuid.UUID(deterministic_step_id(str(run_id), 1))  # uuid5 字符串合法可解析
    assert deterministic_step_id(str(run_id), 1) == deterministic_step_id(str(run_id), 1)
    assert first.step_id == second.step_id == expected


def test_step_id_不同run同seq互异_同run不同seq互异():
    """K32-a：派生键=run_id+seq 二元组——跨 Run 隔离、Run 内按步序唯一。"""
    run_id, other_run = uuid.uuid4(), uuid.uuid4()
    assert deterministic_step_id(str(run_id), 1) != deterministic_step_id(str(other_run), 1)
    assert deterministic_step_id(str(run_id), 1) != deterministic_step_id(str(run_id), 2)
    s1 = StepState(run_id=run_id, seq=1, stage=LoopStage.PLANNING)
    s2 = StepState(run_id=other_run, seq=1, stage=LoopStage.PLANNING)
    assert s1.step_id != s2.step_id


def test_step_id_重放重建判等_按id去重命中():
    """A-5 端到端（恢复幂等根基）：恢复/重放场景同 run 同 seq 重建 StepState——
    step_id 与原步判等，按 id 去重可命中（重放去重的根基语义）。"""
    run_id = uuid.uuid4()
    original = StepState(run_id=run_id, seq=2, stage=LoopStage.PLANNING, action_iri="urn:ex:act")
    original.transition(StepStatus.GATED, stage=LoopStage.GATE)  # 原步已推进
    replay = StepState(run_id=run_id, seq=2, stage=LoopStage.PLANNING, action_iri="urn:ex:act")
    assert replay.step_id == original.step_id  # 状态不同不影响判等：id 只由 run_id+seq 决定
    assert len({original.step_id, replay.step_id}) == 1  # 恢复侧按 id 去重的最小形态


def test_step_id_显式直传保留_派生不覆盖():
    """K32-b 边界：显式直传 step_id（测试桩 uuid4 口径）原样保留——派生只在默认生成路径。"""
    explicit = uuid.uuid4()
    state = StepState(run_id=uuid.uuid4(), seq=1, stage=LoopStage.PLANNING, step_id=explicit)
    assert state.step_id == explicit


def test_run_id非规范拼写归一后恒等派生():
    """ocr/专家 P2：{花括号}/大写形态与规范形态必须派生同 id（同 run 恒等）。"""
    from services.agent.domain.model.step_state import deterministic_step_id
    rid = "6f9619ff-8b86-d011-b42d-00c04fc964ff"
    braced = "{" + rid + "}"
    upper = rid.upper()
    a = StepState(run_id=rid, seq=3)
    b = StepState(run_id=braced, seq=3)
    c = StepState(run_id=upper, seq=3)
    assert a.step_id == b.step_id == c.step_id

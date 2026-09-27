# tests/domain/test_memory_model.py
"""记忆领域模型单测（规格 06 篇 §3）。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from services.memory.domain.model.memory import (
    MemoryLayer,
    MemoryRecord,
    MemoryScope,
    MemoryStateError,
    MemoryType,
    RecordState,
)

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _rec(**kw) -> MemoryRecord:
    base = dict(
        id=uuid4(),
        tenant_id=uuid4(),
        layer=MemoryLayer.USER,
        record_type=MemoryType.FACT_CLAIM,
        subject_iri="http://example.org/ent/sys-a",
        content="A 系统负责人是张三",
        structured={"attribute": "owner", "value": "张三"},
        scope=MemoryScope.PERSONAL,
        confidence=0.8,
        source_ref=[{"session_id": str(uuid4()), "message_id": str(uuid4())}],
    )
    base.update(kw)
    return MemoryRecord(**base)


def test_memory_type_has_seven_classes():
    assert len(MemoryType) == 7  # 规格 §3.1 七类


def test_supersede_active_ok():
    r = _rec()
    new_id = uuid4()
    r.supersede(new_id, now=NOW)
    assert r.state is RecordState.SUPERSEDED
    assert r.superseded_by == new_id


def test_supersede_terminal_state_rejected():
    r = _rec(state=RecordState.EXPIRED)
    with pytest.raises(MemoryStateError):
        r.supersede(uuid4(), now=NOW)


def test_invalidate_ok():
    r = _rec()
    r.invalidate(now=NOW)
    assert r.state is RecordState.INVALIDATED


def test_is_injectable_active_and_valid():
    r = _rec(valid_to=NOW + timedelta(days=1))
    assert r.is_injectable(NOW) is True


def test_is_injectable_rejects_expired_valid_to():
    r = _rec(valid_to=NOW - timedelta(days=1))
    assert r.is_injectable(NOW) is False


def test_decay_score_half_life():
    r = _rec(confidence=1.0, created_at=NOW - timedelta(days=30))
    score = r.decay_score(now=NOW, half_life_days=30)
    assert score == pytest.approx(0.5, abs=1e-6)  # 一个半衰期减半

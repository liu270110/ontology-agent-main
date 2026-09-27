# tests/domain/test_consolidation.py
"""沉淀领域纯函数：冲突确定性判定 + Observation 固化（推理分级：规则先行）。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from services.memory.domain.model.consolidation import (
    VerdictKind,
    consolidate_observations,
    detect_conflicts,
)
from services.memory.domain.model.memory import MemoryLayer, MemoryRecord, MemoryScope, MemoryType

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _fact(attr: str, value: str | int, *, subject: str | None, conf: float = 0.8, age_days: int = 0):
    return MemoryRecord(
        id=uuid4(),
        tenant_id=uuid4(),
        layer=MemoryLayer.USER,
        record_type=MemoryType.FACT_CLAIM,
        subject_iri=subject,
        content=f"{attr}={value}",
        structured={"attribute": attr, "value": value},
        scope=MemoryScope.PERSONAL,
        confidence=conf,
        created_at=NOW - timedelta(days=age_days),
    )


def test_same_attribute_different_value_marks_conflict():
    existing = [_fact("owner", "张三", subject="http://e/s1")]
    verdict = detect_conflicts(
        new_attr="owner", new_value="李四", new_subject="http://e/s1", existing=existing, now=NOW
    )
    assert verdict.kind is VerdictKind.CONFLICT
    assert verdict.old_record_id == existing[0].id


def test_same_attribute_same_value_is_duplicate():
    existing = [_fact("owner", "张三", subject="http://e/s1")]
    verdict = detect_conflicts(
        new_attr="owner", new_value="张三", new_subject="http://e/s1", existing=existing, now=NOW
    )
    assert verdict.kind is VerdictKind.DUPLICATE


def test_numeric_value_duplicate_across_types():
    existing = [_fact("age", 42, subject="http://e/s1")]  # structured value 为 int（模拟 JSONB 回读）
    verdict = detect_conflicts("age", "42", "http://e/s1", existing, NOW)
    assert verdict.kind is VerdictKind.DUPLICATE


def test_no_subject_or_other_attribute_adds():
    assert detect_conflicts("owner", "李四", None, [], NOW).kind is VerdictKind.ADD
    other = [_fact("vendor", "XX", subject="http://e/s1")]
    assert detect_conflicts("owner", "李四", "http://e/s1", other, NOW).kind is VerdictKind.ADD


def test_consolidate_observations_groups_and_counts():
    a1 = _fact("owner", "张三", subject="http://e/s1", age_days=3)
    a2 = _fact("owner", "张三", subject="http://e/s1", age_days=1)
    solo = _fact("vendor", "XX", subject="http://e/s1", age_days=1)
    drafts = consolidate_observations([a1, a2, solo], min_proof=2, now=NOW)
    assert len(drafts) == 1
    d = drafts[0]
    assert d.proof_count == 2 and set(d.supported_by) == {a1.id, a2.id}
    assert "owner" in d.content


def test_consolidate_requires_distinct_records():
    a = _fact("owner", "张三", subject="http://e/s1")
    assert consolidate_observations([a], min_proof=2, now=NOW) == []

"""L2 事实领域模型单测（memory §7 数据模型 + §5.4 指纹幂等语义；零外部依赖）。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from services.memory.domain.model.l2_fact import (
    FactCategory,
    FactStateError,
    FactStatus,
    L2Fact,
    fact_fingerprint,
    normalize_content,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _fact(**kw) -> L2Fact:
    base: dict = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "user_id": uuid4(),
        "content": "用户偏好：工单摘要先给结论",
        "category": FactCategory.PREFERENCE,
        "confidence": 0.8,
    }
    base.update(kw)
    return L2Fact(**base)


def test_指纹_规范化文本折叠空白后哈希且同内容同category恒定():
    # Act / Assert：多空白与首尾空白归一 → 同指纹
    a = fact_fingerprint("停电 分析  先给结论", FactCategory.PREFERENCE)
    b = fact_fingerprint("  停电 分析 先给结论  ", FactCategory.PREFERENCE)
    assert a == b and len(a) == 64
    # Assert：category 参与指纹（同文本不同类不判重）
    assert fact_fingerprint("停电分析先给结论", FactCategory.PREFERENCE) != fact_fingerprint(
        "停电分析先给结论", FactCategory.FACT
    )


def test_事实构造期自动计算指纹_且随内容赋值重算():
    fact = _fact()
    assert fact.fingerprint == fact_fingerprint(fact.content, fact.category)
    fact.content = "新偏好：表格优先"  # validate_assignment 触发校验器重算
    assert fact.fingerprint == fact_fingerprint("新偏好：表格优先", FactCategory.PREFERENCE)


def test_invalidate_墓碑_置状态并写valid_to_终态不可再迁移():
    fact = _fact()
    fact.invalidate(NOW)
    assert fact.status is FactStatus.INVALIDATED and fact.valid_to == NOW
    with pytest.raises(FactStateError):
        fact.supersede(uuid4(), NOW)  # invalidated 为终态（不物理删除、不可再变）


def test_supersede_版本链_旧事实指向新事实():
    fact, new_id = _fact(), uuid4()
    fact.supersede(new_id, NOW)
    assert fact.status is FactStatus.SUPERSEDED and fact.supersedes_id == new_id


def test_衰减打分_半衰期30天():
    fact = _fact(confidence=1.0, created_at=NOW - timedelta(days=30))
    assert fact.decay_score_at(NOW, half_life_days=30) == pytest.approx(0.5, abs=1e-6)
    assert normalize_content("  a\t\nb ") == "a b"

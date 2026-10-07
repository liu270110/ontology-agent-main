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
    fact.invalidate(NOW, reason="信息过期")  # K2-a：reason 必填（§11.1）
    assert fact.status is FactStatus.INVALIDATED and fact.valid_to == NOW
    with pytest.raises(FactStateError):
        fact.supersede(uuid4(), NOW)  # invalidated 为终态（不物理删除、不可再变）


def test_invalidate_reason必填_空缺与空白一律ValueError():
    fact = _fact()
    with pytest.raises(ValueError, match="reason"):
        fact.invalidate(NOW, reason="")  # 缺失拒绝（§11.1 无 reason 拒绝失效）
    with pytest.raises(ValueError, match="reason"):
        fact.invalidate(NOW, reason="   ")  # 纯空白等价缺失（领域层 fail-closed）
    assert fact.status is FactStatus.ACTIVE and fact.valid_to is None  # 拒绝即无副作用


def test_supersede_版本链_旧事实指向新事实():
    fact, new_id = _fact(), uuid4()
    fact.supersede(new_id, NOW)
    assert fact.status is FactStatus.SUPERSEDED and fact.supersedes_id == new_id


def test_衰减打分_半衰期30天():
    fact = _fact(confidence=1.0, created_at=NOW - timedelta(days=30))
    assert fact.decay_score_at(NOW, half_life_days=30) == pytest.approx(0.5, abs=1e-6)
    assert normalize_content("  a\t\nb ") == "a b"


# ---------------------------------------------------------------- K22 D-6 软时效降权（Agent/13 §28）


def test_衰减打分_D6_无时效线等价纯半衰期_过期压至地板():
    """valid_to=None 既有语义零漂移；过点（自然时效）得分=纯半衰期×地板，不失效硬门。"""
    fact = _fact(confidence=1.0, created_at=NOW - timedelta(days=30))
    assert fact.decay_score_at(NOW, half_life_days=30) == pytest.approx(0.5, abs=1e-6)  # 无时效线乘子=1.0
    expired = _fact(confidence=1.0, created_at=NOW - timedelta(days=32), valid_to=NOW - timedelta(days=2))
    # 过期：0.5^(32/30)×0.1≈0.04774——得分压至地板，status 仍 active（软降权非硬门）
    assert expired.decay_score_at(NOW, half_life_days=30) == pytest.approx(0.1 * 0.5 ** (32 / 30), abs=1e-6)
    assert expired.status is FactStatus.ACTIVE


def test_衰减打分_D6_临近过期线性滑落_地板可配():
    # 时效窗 [NOW-10d, NOW+10d] 中点：乘子=1-(1-0.1)×0.5=0.55；地板可配面 floor=0.25 → 0.625
    fact = _fact(confidence=1.0, created_at=NOW - timedelta(days=10), valid_to=NOW + timedelta(days=10))
    pure = 0.5 ** (10 / 30)
    assert fact.decay_score_at(NOW, half_life_days=30) == pytest.approx(pure * 0.55)
    assert fact.decay_score_at(NOW, half_life_days=30, expiry_floor=0.25) == pytest.approx(pure * 0.625)


def test_invalidate的valid_to是失效时间戳_乘子口径统一但状态门另判():
    """双时间线区分（K22 红线）：invalidate 写 valid_to=失效时刻，decay_score_at 按同一乘子
    函数落到地板（now≥valid_to）；但失效事实不进召回候选（status 硬门，repo 查询口径）——
    乘子无排名效应，自然时效软过期的"仍可注入"不适用于人工失效（INVALIDATED 终态不动）。"""
    fact = _fact(confidence=1.0, created_at=NOW - timedelta(days=30))
    fact.invalidate(NOW, reason="信息过期")
    assert fact.valid_to == NOW  # 失效时间戳（K2-a 既有语义，零改动）
    assert fact.decay_score_at(NOW, half_life_days=30) == pytest.approx(0.05, abs=1e-6)  # 打分口径统一落地板

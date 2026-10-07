# tests/domain/test_memory_model.py
"""记忆领域模型单测（规格 06 篇 §3）。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from services.memory.domain.model.memory import (
    DEFAULT_EXPIRY_FLOOR,
    MemoryLayer,
    MemoryRecord,
    MemoryScope,
    MemoryStateError,
    MemoryType,
    RecordState,
    expiry_multiplier,
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
    # K22 D-6（Agent/13 §28）翻转：valid_to 过点不再是注入硬门——自然时效软过期仍可注入
    r = _rec(valid_to=NOW - timedelta(days=1))
    assert r.is_injectable(NOW) is True


def test_decay_score_half_life():
    r = _rec(confidence=1.0, created_at=NOW - timedelta(days=30))
    score = r.decay_score(now=NOW, half_life_days=30)
    assert score == pytest.approx(0.5, abs=1e-6)  # 一个半衰期减半


# ---------------------------------------------------------------- K22 D-6 软时效降权（Agent/13 §28）


def test_expiry_multiplier_无时效线恒一_未过期寿命零消耗为一():
    assert expiry_multiplier(None, NOW, start=NOW - timedelta(days=5), floor=0.1) == 1.0  # 无时效线不降权
    r = _rec(valid_to=NOW + timedelta(days=10), created_at=NOW)
    assert expiry_multiplier(r.valid_to, NOW, start=r.created_at, floor=0.1) == 1.0  # now==start 寿命零消耗


def test_expiry_multiplier_临近过期线性滑落():
    # 时效窗 [NOW-10d, NOW+10d]，now 恰在中点：剩余寿命消耗一半 → 1-(1-0.1)×0.5=0.55
    start, valid_to = NOW - timedelta(days=10), NOW + timedelta(days=10)
    assert expiry_multiplier(valid_to, NOW, start=start, floor=DEFAULT_EXPIRY_FLOOR) == pytest.approx(0.55)
    # 地板常量可配（Settings memory_expiry_floor 同参面）：floor=0.25 → 1-0.75×0.5=0.625
    assert expiry_multiplier(valid_to, NOW, start=start, floor=0.25) == pytest.approx(0.625)
    # 边界外推钳制：now 早于 start（时钟偏斜）不越 1.0
    assert expiry_multiplier(valid_to, NOW - timedelta(days=20), start=start, floor=0.1) == 1.0


def test_expiry_multiplier_过期取地板常量_越界floor拒绝():
    valid_to = NOW - timedelta(days=1)
    assert expiry_multiplier(valid_to, NOW, start=NOW - timedelta(days=30), floor=0.1) == 0.1
    assert expiry_multiplier(valid_to, NOW, start=NOW - timedelta(days=30), floor=0.25) == 0.25
    with pytest.raises(ValueError, match="floor"):
        expiry_multiplier(valid_to, NOW, start=NOW - timedelta(days=30), floor=1.5)  # 越界 fail-closed


def test_is_injectable_过期仍可注入_得分压至地板():
    """D-6 一体两面（Agent/13 §28）：过期=软降权非硬门；分=纯半衰期×地板。"""
    r = _rec(valid_to=NOW - timedelta(days=1), created_at=NOW - timedelta(days=2), confidence=1.0)
    assert r.is_injectable(NOW) is True  # 过期仍可注入（K22 放宽）
    assert r.decay_score(NOW, 30) == pytest.approx(r.half_life_score(NOW, 30) * 0.1)  # 地板乘子
    assert r.decay_score(NOW, 30) < r.half_life_score(NOW, 30)  # 被压低但仍非零


def test_is_injectable_INVALIDATED终态不随软化松动():
    """valid_to 双时间线只软化"时效到点"一支：人工失效（INVALIDATED）硬门绝不动（K22 红线）——
    即便 valid_to 未到也不因软化获得注入资格；SUPERSEDED/EXPIRED 终态同理。"""
    r = _rec(valid_to=NOW + timedelta(days=30))  # valid_to 未到
    r.invalidate(now=NOW)
    assert r.is_injectable(NOW) is False
    assert _rec(state=RecordState.EXPIRED).is_injectable(NOW) is False  # 衰减终局同理
    r2 = _rec()
    r2.supersede(uuid4(), now=NOW)
    assert r2.is_injectable(NOW) is False


def test_decay_score_无时效线等价纯半衰期_未过期线性滑落():
    """valid_to=None 既有语义零漂移；未过期记录按剩余寿命线性滑落（消费面按分吃）。"""
    r = _rec(confidence=1.0, created_at=NOW - timedelta(days=30))
    assert r.decay_score(NOW, 30) == pytest.approx(r.half_life_score(NOW, 30))  # 无时效线=乘子 1.0
    # 未过期：时效窗 [NOW-10d, NOW+10d] 中点 → 乘子 0.55
    r2 = _rec(confidence=0.8, created_at=NOW - timedelta(days=10), valid_to=NOW + timedelta(days=10))
    assert r2.decay_score(NOW, 30) == pytest.approx(r2.half_life_score(NOW, 30) * 0.55)

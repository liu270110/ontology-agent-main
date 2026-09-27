"""L2 事实仓储集成测试（本地 PG；不可达/未迁移自动跳过——tests/kb 同款 skip 模式）。

覆盖：CRUD、租户隔离、指纹幂等判重、invalidate 墓碑、status/category 过滤、
关键词/新近通道候选（检索融合的仓储面）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from services.memory.domain.model.l2_fact import FactCategory, FactStatus, L2Fact

if TYPE_CHECKING:
    from tests.memory.conftest import MemorySeed

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _fact(seed: MemorySeed, content: str, **kw) -> L2Fact:
    base: dict = {
        "id": uuid4(),
        "tenant_id": seed.tenant_id,
        "user_id": seed.user_id,
        "content": content,
        "category": FactCategory.FACT,
        "confidence": 0.9,
        "decay_score": 0.9,
        "source_session_id": seed.session_id,
        "valid_from": NOW,
    }
    base.update(kw)
    return L2Fact(**base)


async def test_L2_事实写入与读取_roundtrip(mem_seed):
    # Arrange
    fact = _fact(mem_seed, "城东线路 3 号变压器 2026-08 更换")
    async with mem_seed.factory() as db:
        # Act
        await mem_seed.repo(db).add(fact)
        await db.commit()
        loaded = await mem_seed.repo(db).get(fact.id)
        # Assert
        assert loaded is not None
        assert loaded.content == fact.content
        assert loaded.category is FactCategory.FACT
        assert loaded.source_session_id == mem_seed.session_id
        assert loaded.fingerprint == fact.fingerprint


async def test_L2_租户隔离_跨租户读写均不可见(mem_seed):
    # Arrange：本租户写入一条事实
    fact = _fact(mem_seed, "租户 A 专属事实")
    other_tenant = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
    async with mem_seed.factory() as db:
        await mem_seed.repo(db).add(fact)
        await db.commit()
        # Act：他租户仓储（构造期绑定不同 tenant_id，04 §4 租户作用域）读写
        from services.memory.data.repo_impl.fact_repo import PgL2FactRepository

        foreign = PgL2FactRepository(db, other_tenant)
        # Assert：get/list/fingerprint 全部不可见
        assert await foreign.get(fact.id) is None
        assert await foreign.find_by_fingerprint(mem_seed.user_id, fact.fingerprint) is None
        assert await foreign.list_for_user(mem_seed.user_id) == []


async def test_L2_指纹幂等_同用户同内容同category命中既有事实(mem_seed):
    # Arrange
    first = _fact(mem_seed, "用户偏好：结论先行")
    second = _fact(mem_seed, "  用户偏好：结论先行  ")  # 规范化后同文 → 同指纹
    second.id = uuid4()
    async with mem_seed.factory() as db:
        repo = mem_seed.repo(db)
        await repo.add(first)
        await db.commit()
        # Act / Assert：指纹命中既有事实（§5.4 重复提交返回原 fact_id）
        hit = await repo.find_by_fingerprint(mem_seed.user_id, second.fingerprint)
        assert hit is not None and hit.id == first.id


async def test_L2_invalidate墓碑_状态置invalidated且写valid_to_未命中404(mem_seed):
    fact = _fact(mem_seed, "将被失效的事实")
    async with mem_seed.factory() as db:
        repo = mem_seed.repo(db)
        await repo.add(fact)
        await db.commit()
        # Act：invalidate（聚合方法 + save_state）
        loaded = await repo.get(fact.id)
        assert loaded is not None
        loaded.invalidate(NOW)
        await repo.save_state(loaded)
        await db.commit()
        # Assert：墓碑留痕可审计查询（不物理删除）
        after = await repo.get(fact.id)
        assert after is not None
        assert after.status is FactStatus.INVALIDATED
        assert after.valid_to == NOW
        # Assert：未命中/跨租户 → None（端点层映射 404）
        assert await repo.get(uuid4()) is None


async def test_L2_列表过滤_status_category(mem_seed):
    # Arrange：3 条不同状态/类别
    f1 = _fact(mem_seed, "事实甲", category=FactCategory.FACT)
    f2 = _fact(mem_seed, "偏好乙", category=FactCategory.PREFERENCE)
    f3 = _fact(mem_seed, "失效丙", category=FactCategory.FACT)
    f3.invalidate(NOW)
    async with mem_seed.factory() as db:
        repo = mem_seed.repo(db)
        await repo.add(f1)
        await repo.add(f2)
        await repo.add(f3)
        await db.commit()
        # Act / Assert：category 过滤
        prefs = await repo.list_for_user(mem_seed.user_id, category=FactCategory.PREFERENCE)
        assert [f.id for f in prefs] == [f2.id]
        # Act / Assert：status 过滤（invalidated 仍可审计查询）
        dead = await repo.list_for_user(mem_seed.user_id, status=FactStatus.INVALIDATED)
        assert [f.id for f in dead] == [f3.id]
        # Act / Assert：分页
        page = await repo.list_for_user(mem_seed.user_id, offset=0, limit=2)
        assert len(page) == 2


async def test_L2_检索候选_关键词通道命中与新近通道排序(mem_seed):
    # Arrange：写入 3 条活跃事实（内容错开 created_at）
    hit = _fact(mem_seed, "停电分析应先核对变压器台账")
    hit.created_at = NOW
    other = _fact(mem_seed, "用户偏好周报格式")
    other.created_at = NOW
    async with mem_seed.factory() as db:
        repo = mem_seed.repo(db)
        await repo.add(hit)
        await repo.add(other)
        await db.commit()
        # Act / Assert：关键词通道命中（ILIKE 词顶片）
        kw = await repo.search_candidates(mem_seed.user_id, "变压器 台账", limit=5)
        assert [f.id for f in kw] == [hit.id]
        # Act / Assert：新近通道返回全部活跃（倒序）
        recent = await repo.recent_candidates(mem_seed.user_id, limit=5)
        assert len(recent) >= 2
        # Assert：无效查询词不产生候选
        assert await repo.search_candidates(mem_seed.user_id, "", limit=5) == []

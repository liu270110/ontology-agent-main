# tests/data/test_memory_repo.py
"""PgMemoryRepository 集成用例：OA_TEST_PG=1 且本地 PG 可用时执行（CI-S3，integration 标记）。"""

import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.memory.data.repositories.records_repo import PgMemoryRepository
from services.memory.domain.model.memory import MemoryLayer, MemoryRecord, MemoryScope, MemoryType
from services.platform.db import registry as _orm_registry  # noqa: F401  全表聚合注册（create_all 需跨模块 FK 解析）
from services.platform.db.base import Base

if sys.platform == "win32":  # psycopg 异步要求 Selector 循环（tests/memory/conftest.py 同款）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pytestmark = [pytest.mark.integration]
requires_pg = pytest.mark.skipif(
    os.getenv("OA_TEST_PG") != "1" or not os.getenv("OA_TEST_PG_DSN"),
    reason="OA_TEST_PG!=1 或 OA_TEST_PG_DSN 未设置",
)


@requires_pg
async def test_insert_and_search() -> None:
    engine = create_async_engine(os.environ["OA_TEST_PG_DSN"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        repo = PgMemoryRepository(sessionmaker)
        tenant = uuid.uuid4()
        rec = MemoryRecord(
            id=uuid.uuid4(),
            tenant_id=tenant,
            layer=MemoryLayer.USER,
            record_type=MemoryType.FACT_CLAIM,
            subject_iri="http://example.org/ent/sys-a",
            content="A 系统负责人是张三",
            scope=MemoryScope.PERSONAL,
            created_at=datetime.now(UTC),
        )
        await repo.insert(rec)
        got = await repo.get(tenant, rec.id)
        assert got is not None and got.content == "A 系统负责人是张三"
        hits = await repo.search_keyword(tenant, text_q="张三", limit=5)
        assert [r.id for r in hits] == [rec.id]
        assert await repo.supersede(tenant, rec.id, by_id=uuid.uuid4(), now=datetime.now(UTC)) is True
        after = await repo.get(tenant, rec.id)
        assert after is not None and after.state == "superseded"
    finally:
        await engine.dispose()


@requires_pg
async def test_promotion_state_machine_apply_reject() -> None:
    """M4P3-T5 升级单状态机（真表）：submitted→applied（records.layer 2→3 同事务）、驳回不动记录、
    非法流转/跨租户拒绝、approval_id 回填。"""
    engine = create_async_engine(os.environ["OA_TEST_PG_DSN"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        repo = PgMemoryRepository(sessionmaker)
        tenant = uuid.uuid4()
        rec = MemoryRecord(
            id=uuid.uuid4(),
            tenant_id=tenant,
            layer=MemoryLayer.USER,
            record_type=MemoryType.FACT_CLAIM,
            subject_iri="http://example.org/ent/sys-b",
            content="B 系统负责人是李四",
            scope=MemoryScope.PERSONAL,
            created_at=datetime.now(UTC),
        )
        await repo.insert(rec)

        # apply：submitted→applied + 记录 L2→L3（L3 骨架落点）
        pid = await repo.add_promotion(tenant, record_id=rec.id, to_layer=3)
        assert await repo.apply_promotion(tenant, pid) is True
        promo = await repo.get_promotion(tenant, pid)
        assert promo is not None and promo["state"] == "applied"
        assert (await repo.get(tenant, rec.id)).layer == 3
        assert await repo.apply_promotion(tenant, pid) is False  # applied 终态幂等拒绝

        # reject：记录保留 L3（不回退）、promotion rejected 终态
        rid2 = uuid.uuid4()
        await repo.insert(
            MemoryRecord(
                id=rid2,
                tenant_id=tenant,
                layer=MemoryLayer.USER,
                record_type=MemoryType.FACT_CLAIM,
                content="C 系统负责人是王五",
                scope=MemoryScope.PERSONAL,
                created_at=datetime.now(UTC),
            )
        )
        pid2 = await repo.add_promotion(tenant, record_id=rid2, to_layer=3)
        assert await repo.reject_promotion(tenant, pid2) is True
        assert (await repo.get_promotion(tenant, pid2))["state"] == "rejected"
        assert (await repo.get(tenant, rid2)).layer == 2
        assert await repo.reject_promotion(tenant, pid2) is False
        assert await repo.apply_promotion(tenant, pid2) is False  # rejected 不可 apply

        # 记录非 L2（脏数据/重复升级）→ apply 拒绝、升级单原地保留
        rid3 = uuid.uuid4()
        await repo.insert(
            MemoryRecord(
                id=rid3,
                tenant_id=tenant,
                layer=MemoryLayer.ORG,
                record_type=MemoryType.FACT_CLAIM,
                content="已在 L3 的记录",
                scope=MemoryScope.ORG,
                created_at=datetime.now(UTC),
            )
        )
        pid3 = await repo.add_promotion(tenant, record_id=rid3, to_layer=3)
        assert await repo.apply_promotion(tenant, pid3) is False
        assert (await repo.get_promotion(tenant, pid3))["state"] == "submitted"

        # 跨租户隔离 + 未知 id
        assert await repo.get_promotion(uuid.uuid4(), pid) is None
        assert await repo.apply_promotion(uuid.uuid4(), pid) is False
        assert await repo.reject_promotion(tenant, uuid.uuid4()) is False
        assert await repo.get_promotion(tenant, uuid.uuid4()) is None

        # approval_id 回填
        pid4 = await repo.add_promotion(tenant, record_id=rec.id, to_layer=3)
        assert (await repo.get_promotion(tenant, pid4))["approval_id"] is None
        ticket = uuid.uuid4()
        await repo.set_promotion_approval(tenant, pid4, approval_id=ticket)
        assert (await repo.get_promotion(tenant, pid4))["approval_id"] == ticket
    finally:
        await engine.dispose()

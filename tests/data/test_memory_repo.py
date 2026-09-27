# tests/data/test_memory_repo.py
"""PgMemoryRepository 集成用例：OA_TEST_PG=1 且本地 PG 可用时执行（CI-S3，integration 标记）。"""

import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.memory.data import orm_records  # noqa: F401  （导入即注册三表进 Base.metadata）
from services.memory.data.repositories.records_repo import PgMemoryRepository
from services.memory.domain.model.memory import MemoryLayer, MemoryRecord, MemoryScope, MemoryType
from services.platform.db.base import Base

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

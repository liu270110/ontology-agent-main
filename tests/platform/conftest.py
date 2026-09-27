"""platform LLM 补层测试夹具：本地 PG（llm_calls 落库用例）+ fakeredis（预算/降级用例）。"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from fakeredis import aioredis as fakeredis_aio
from sqlalchemy import delete, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.platform.config import Settings

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

if TYPE_CHECKING:
    from redis import asyncio as aioredis


@dataclass
class PlatformSeed:
    settings: Settings
    factory: async_sessionmaker[AsyncSession]
    redis: aioredis.Redis
    tenant_id: uuid.UUID


def fake_redis() -> fakeredis_aio.FakeRedis:
    return fakeredis_aio.FakeRedis(decode_responses=True)


@pytest.fixture
async def llm_seed() -> AsyncIterator[PlatformSeed]:
    """PG（llm_calls 已迁移）+ fakeredis；PG 不可达即跳过落库类用例。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect() as conn:
            await conn.execute(text("SELECT 1 FROM llm_calls LIMIT 1"))
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达或 llm_calls 未迁移，跳过 LLM 审计落库用例")
    await probe.dispose()

    engine = create_async_engine(settings.pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    async with factory() as db, db.begin():
        db.add(
            TenantORM(
                id=tenant_id, name="llm-it-租户", slug=f"llm-it-{uuid.uuid4().hex[:12]}", plan="free", status="active"
            )
        )
    seed = PlatformSeed(settings=settings, factory=factory, redis=fake_redis(), tenant_id=tenant_id)
    yield seed
    async with factory() as db, db.begin():
        from services.platform.llm.orm import LlmCall

        await db.execute(delete(LlmCall).where(LlmCall.tenant_id == tenant_id))
        await db.execute(delete(TenantORM).where(TenantORM.id == tenant_id))
    await engine.dispose()
    await seed.redis.aclose()

"""memory 测试夹具：fakeredis（L1/降级）+ 本地 PG（L2，不可达即跳过）。

每用例自建租户/用户/适配器/agent/会话，结束按 FK 逆序清理（standards/01 §2.9）。
psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略。
"""

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

from services.agent.data.orm import Agent as AgentORM
from services.agent.data.orm import AgentAdapter as AgentAdapterORM
from services.agent.data.orm import Session as SessionORM
from services.iam.data.orm import Tenant as TenantORM
from services.iam.data.orm import User as UserORM
from services.memory.data.repo_impl.fact_repo import PgL2FactRepository
from services.platform.config import Settings
from services.platform.deps import Principal

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

if TYPE_CHECKING:
    from redis import asyncio as aioredis


@dataclass
class MemorySeed:
    """一次用例的完整装配（ids + 会话工厂 + fakeredis + 测试主体）。"""

    settings: Settings
    factory: async_sessionmaker[AsyncSession]
    redis: aioredis.Redis
    principal: Principal
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    agent_id: uuid.UUID
    session_id: uuid.UUID

    def repo(self, db: AsyncSession) -> PgL2FactRepository:
        return PgL2FactRepository(db, self.tenant_id)


def fake_redis() -> fakeredis_aio.FakeRedis:
    return fakeredis_aio.FakeRedis(decode_responses=True)


@pytest.fixture
async def mem_seed() -> AsyncIterator[MemorySeed]:
    """PG + fakeredis 全装配；PG 不可达或 schema 未迁移即跳过。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect() as conn:
            await conn.execute(text("SELECT 1 FROM memory_l2_facts LIMIT 1"))
            # 会话种子前置列（27 篇 X15 并行批 ORM 已加 type/routing、迁移未入库时干净跳过，
            # 勿以 setup ERROR 染红门禁——本夹具契约「schema 未迁移即跳过」的组成部分）
            cols = await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'sessions' AND column_name IN ('type', 'routing')"
                )
            )
            if int(cols.scalar_one()) != 2:
                await probe.dispose()
                pytest.skip("本地 PG sessions 表缺 type/routing 列（并行批迁移未应用），跳过 memory 集成用例")
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达或 memory_l2_facts 未迁移，跳过 memory 集成用例")
    await probe.dispose()

    engine = create_async_engine(settings.pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, user_id, session_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with factory() as db, db.begin():
        tenant = TenantORM(
            id=tenant_id, name="mem-it-租户", slug=f"mem-it-{uuid.uuid4().hex[:12]}", plan="free", status="active"
        )
        db.add(tenant)
        await db.flush()
        user = UserORM(
            id=user_id, tenant_id=tenant_id, email=f"{uuid.uuid4().hex[:10]}@mem-it.local", password_hash="it-only"
        )
        adapter = AgentAdapterORM(agent_tool="nanobot", version=f"mem-it-{uuid.uuid4().hex[:8]}")
        db.add_all([user, adapter])
        await db.flush()
        agent = AgentORM(
            tenant_id=tenant_id,
            name=f"mem-it-agent-{uuid.uuid4().hex[:8]}",
            agent_tool=adapter.agent_tool,
            adapter_id=adapter.id,
        )
        db.add(agent)
        await db.flush()
        db.add(
            SessionORM(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent.id,
                user_id=user_id,
                channel="web",
                status="active",
            )
        )
    principal = Principal(
        {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "roles": ["member"],
            "scopes": ["memory:read", "memory:write"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )
    seed = MemorySeed(
        settings=settings,
        factory=factory,
        redis=fake_redis(),
        principal=principal,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent.id,
        session_id=session_id,
    )
    yield seed
    async with factory() as db, db.begin():
        from services.memory.data.orm import MemoryL2Fact as MemoryL2FactORM

        await db.execute(
            text("DELETE FROM audit_logs WHERE tenant_id = CAST(:tid AS uuid)"), {"tid": str(tenant_id)}
        )  # ★ 端点副作用（promotions 登记行）先于租户清理（FK 逆序，standards/01 §2.9）
        await db.execute(delete(MemoryL2FactORM).where(MemoryL2FactORM.tenant_id == tenant_id))
        await db.execute(delete(SessionORM).where(SessionORM.tenant_id == tenant_id))
        await db.execute(delete(AgentORM).where(AgentORM.id == agent.id))
        await db.execute(delete(AgentAdapterORM).where(AgentAdapterORM.id == adapter.id))
        await db.execute(delete(UserORM).where(UserORM.id == user_id))
        await db.execute(delete(TenantORM).where(TenantORM.id == tenant_id))
    await engine.dispose()
    await seed.redis.aclose()

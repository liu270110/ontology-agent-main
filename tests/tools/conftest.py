"""tools 集市测试夹具：PG 集成用例租户种子（不可达/未迁移自动跳过）。

Windows psycopg 需 Selector 事件循环；每用例自建租户+用户，结束按 FK 逆序清理
（standards/01 §2.9；共享 PG 口径=tests/plugin/conftest.py 同款）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.iam.data.orm import User as UserORM
from services.platform.config import Settings

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@dataclass
class ToolsSeed:
    """PG 集成用例装配（每用例独立租户 + 会话工厂）。"""

    settings: Settings
    factory: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    user_id: uuid.UUID


@pytest.fixture
async def tools_seed() -> AsyncIterator[ToolsSeed]:
    """PG 全装配；不可达或 tools_registry 未迁移即跳过（本批迁移归属验证锚点）。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect() as conn:
            await conn.execute(text("SELECT 1 FROM tools_registry LIMIT 1"))
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达或 tools_registry 未迁移（e5c7d9f1a3b5），跳过 tools 集成用例")
    await probe.dispose()

    engine = create_async_engine(settings.pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as db, db.begin():
        db.add(TenantORM(id=tenant_id, name="tools-it-租户", slug=f"tools-it-{uuid.uuid4().hex[:12]}", settings={}))
        db.add(
            UserORM(
                id=user_id,
                tenant_id=tenant_id,
                email=f"{uuid.uuid4().hex[:10]}@tools-it.local",
                password_hash="it",
            )
        )
    yield ToolsSeed(settings=settings, factory=factory, tenant_id=tenant_id, user_id=user_id)
    async with factory() as db, db.begin():
        await db.execute(delete(UserORM).where(UserORM.tenant_id == tenant_id))
        await db.execute(text("DELETE FROM audit_logs WHERE tenant_id = :tid"), {"tid": str(tenant_id)})
        await db.execute(text("DELETE FROM tools_registry WHERE tenant_id = :tid"), {"tid": str(tenant_id)})
        await db.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": str(tenant_id)})
    await engine.dispose()

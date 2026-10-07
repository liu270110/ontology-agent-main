"""plugin 测试夹具：PG 集成用例租户种子（不可达/未迁移自动跳过）；共享 Fake 见 helpers.py。

Windows psycopg 需 Selector 事件循环；每用例自建租户（settings.governance_tier=solo）+
发布者，结束按 FK 逆序清理（standards/01 §2.9）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.iam.data.orm import User as UserORM
from services.platform.config import Settings
from services.plugin.runtime.registry import PluginRuntime

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 平台签名 dev key（两级签名 fail-closed 的开发期供给口径：OA_PLATFORM_PLUGIN_SIGNING_KEY；
# 生成方式=services.platform.security.generate_signing_key，生产经部署密钥管理下发，禁入库）
os.environ.setdefault("OA_PLATFORM_PLUGIN_SIGNING_KEY", "3f" * 32)


@dataclass
class PluginSeed:
    """PG 集成用例装配（租户 settings.governance_tier=solo + 会话工厂 + 发布者）。"""

    settings: Settings
    factory: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    publisher_id: uuid.UUID
    runtime: PluginRuntime


@pytest.fixture
async def plugin_seed() -> AsyncIterator[PluginSeed]:
    """PG 全装配；不可达或 plugin 四表未迁移即跳过（本批迁移归属验证锚点）。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect() as conn:
            await conn.execute(text("SELECT 1 FROM plugins LIMIT 1"))
            await conn.execute(text("SELECT 1 FROM tools LIMIT 1"))
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达或 plugin 四表未迁移（a1b2c3d4e5f6），跳过 plugin 集成用例")
    await probe.dispose()

    engine = create_async_engine(settings.pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, publisher = uuid.uuid4(), uuid.uuid4()
    async with factory() as db, db.begin():
        db.add(
            TenantORM(
                id=tenant_id,
                name="plugin-it-租户",
                slug=f"plugin-it-{uuid.uuid4().hex[:12]}",
                settings={"governance_tier": "solo"},
            )
        )
        db.add(
            UserORM(
                id=publisher, tenant_id=tenant_id, email=f"{uuid.uuid4().hex[:10]}@plugin-it.local", password_hash="it"
            )
        )
    yield PluginSeed(
        settings=settings, factory=factory, tenant_id=tenant_id, publisher_id=publisher, runtime=PluginRuntime()
    )
    async with factory() as db, db.begin():
        from services.plugin.data.orm import PluginORM, PluginVersionORM, ToolInvocationORM, ToolORM
        from services.review.data.orm import ReviewTicket as ReviewTicketORM

        plugin_ids = (await db.execute(select(PluginORM.id).where(PluginORM.slug.like("it-%")))).scalars().all()
        if plugin_ids:
            await db.execute(
                delete(ReviewTicketORM).where(
                    ReviewTicketORM.tenant_id == tenant_id,
                    ReviewTicketORM.target_type == "plugin_listing",
                    ReviewTicketORM.target_id.in_(
                        select(PluginVersionORM.id).where(PluginVersionORM.plugin_id.in_(plugin_ids))
                    ),
                )
            )
            await db.execute(delete(PluginVersionORM).where(PluginVersionORM.plugin_id.in_(plugin_ids)))
        await db.execute(delete(PluginORM).where(PluginORM.slug.like("it-%")))
        await db.execute(delete(ToolInvocationORM).where(ToolInvocationORM.tenant_id == tenant_id))
        await db.execute(delete(ToolORM).where(ToolORM.tenant_id == tenant_id))
        await db.execute(delete(ReviewTicketORM).where(ReviewTicketORM.tenant_id == tenant_id))
        await db.execute(delete(UserORM).where(UserORM.tenant_id == tenant_id))
        await db.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": str(tenant_id)})
    await engine.dispose()

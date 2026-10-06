"""gateway 集成测试夹具：本地 PG（deploy compose）直连；不可达即跳过（CI lite 段执行）。

fixture 分层（standards/01 §2.9）：每用例自建租户/用户/适配器/agent，结束自清理（FK 逆序）。
psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.agent.data.orm import Agent as AgentORM
from services.agent.data.orm import AgentAdapter as AgentAdapterORM
from services.agent.data.orm import Message as MessageORM
from services.agent.data.orm import Run as RunORM
from services.agent.data.orm import Session as SessionORM
from services.agent.data.orm import Task as TaskORM
from services.agent.data.orm import TaskEvent as TaskEventORM
from services.iam.data.orm import DeviceSession as DeviceSessionORM
from services.iam.data.orm import Tenant as TenantORM
from services.iam.data.orm import TotpBackupCode as TotpBackupCodeORM
from services.iam.data.orm import TotpCredential as TotpCredentialORM
from services.iam.data.orm import User as UserORM
from services.iam.data.orm import UserPreferences as UserPreferenceORM
from services.platform.config import Settings
from services.platform.db.uow import AsyncUnitOfWork
from services.platform.deps import Principal
from services.writeback.data.orm import OutboxEventORM

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def make_principal(tenant_id: uuid.UUID, user_id: uuid.UUID) -> Principal:
    """测试主体：持有 session 域三 scope（api/01 §5.2），绕过 JWT 中间件（端点直调）。"""
    return Principal(
        {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "roles": ["member"],
            "scopes": ["session:read", "session:write", "session:chat"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


@pytest.fixture
async def gateway_uow() -> AsyncIterator[AsyncUnitOfWork]:
    """AsyncUnitOfWork 直连本地 PG；不可达则跳过整用例。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过 gateway 集成用例")
    await probe.dispose()
    uow = AsyncUnitOfWork.from_dsn(settings.pg_dsn)
    yield uow
    await uow.aclose()


@pytest.fixture
async def seed(gateway_uow: AsyncUnitOfWork) -> AsyncIterator[tuple[Principal, uuid.UUID]]:
    """每用例独立租户/用户/适配器/agent；返回 (主体, agent_id)，结束按 FK 逆序清理。"""
    settings = Settings()
    engine = create_async_engine(settings.pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        tenant = TenantORM(name="it-租户", slug=f"it-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()  # uuid7 PK 在 flush 时分配，依赖行需引用真实 id
        user = UserORM(tenant_id=tenant.id, email=f"{uuid.uuid4().hex[:10]}@it.local", password_hash="it-only")
        adapter = AgentAdapterORM(agent_tool="nanobot", version=f"it-{uuid.uuid4().hex[:8]}")
        db.add_all([user, adapter])
        await db.flush()
        agent = AgentORM(
            tenant_id=tenant.id,
            name=f"it-agent-{uuid.uuid4().hex[:8]}",
            agent_tool=adapter.agent_tool,
            adapter_id=adapter.id,
        )
        db.add(agent)
        principal = make_principal(tenant.id, user.id)
    yield principal, agent.id
    async with factory() as db, db.begin():
        for stmt in (
            delete(MessageORM).where(MessageORM.tenant_id == tenant.id),
            delete(TaskEventORM).where(TaskEventORM.tenant_id == tenant.id),
            # 计划 4.2：enqueue_projection 已真实写 outbox_events（platform/db/uow.py）——
            # 租户清理须先摘除其 outbox 行，否则 tenants 删除触发 fk_outbox_events 违例
            delete(OutboxEventORM).where(OutboxEventORM.tenant_id == tenant.id),
            delete(RunORM).where(RunORM.tenant_id == tenant.id),
            delete(TaskORM).where(TaskORM.tenant_id == tenant.id),
            delete(SessionORM).where(SessionORM.tenant_id == tenant.id),
            delete(AgentORM).where(AgentORM.id == agent.id),
            # C1 me 域表先于 users 清（device_sessions/totp* 以 user_id 引用 users；
            # user_preferences 以租户+用户为键——漏列曾致 24 例 teardown FK 违例）
            delete(DeviceSessionORM).where(DeviceSessionORM.user_id == user.id),
            delete(TotpCredentialORM).where(TotpCredentialORM.user_id == user.id),
            delete(TotpBackupCodeORM).where(TotpBackupCodeORM.user_id == user.id),
            delete(UserPreferenceORM).where(UserPreferenceORM.user_id == user.id),
            delete(UserORM).where(UserORM.id == user.id),
            delete(AgentAdapterORM).where(AgentAdapterORM.id == adapter.id),
            delete(TenantORM).where(TenantORM.id == tenant.id),
        ):
            await db.execute(stmt)
    await engine.dispose()

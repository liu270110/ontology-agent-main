# tests/agent/test_session_created_at_b3.py
"""会话读面 created_at 透出测试（P1-B3 缺陷修复 2026-10-07：SessionOut.created_at 恒 null）。

缺陷：GET /sessions 行 created_at=null 而 ORM 有值——盘点结论根因在 domain 层：Session
聚合无 created_at 字段，仓储映射（_session_to_domain）与 API 映射（from_domain）链路两处
皆无从透出。修复：domain Session 增 created_at（可选，未落库聚合为 None）+ 两处映射随行。

用一次性 PG 测试库承载（tests/agent/pg_testdb.py 同款）；本地 PG 不可达即跳过。
端点直调（Depends 显式传参等价，test_session_user_face.py 同口径）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.agent.api.schemas.session import SessionCreateIn
from services.agent.api.sessions import create_session, get_session, list_sessions
from services.agent.data.orm import Agent as AgentORM
from services.agent.data.orm import AgentAdapter as AgentAdapterORM
from services.agent.data.orm import Session as SessionORM
from services.iam.data.orm import Tenant as TenantORM
from services.iam.data.orm import User as UserORM
from services.platform.db import registry as _orm_registry  # noqa: F401  全表聚合注册（create_all 需跨模块 FK 解析）
from services.platform.db.base import Base
from services.platform.db.uow import AsyncUnitOfWork
from services.platform.deps import Principal
from tests.agent.pg_testdb import create_test_database, drop_test_database, probe_pg

if sys.platform == "win32":  # psycopg 异步要求 Selector 循环（导入期固定策略）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pytestmark = pytest.mark.integration


@pytest.fixture
async def pg_env() -> AsyncIterator[tuple[AsyncUnitOfWork, async_sessionmaker[AsyncSession]]]:
    """一次性测试库（每用例独立，create_all 建全量表）→ (UoW, 裸 session 工厂)。"""
    from services.platform.config import Settings

    settings = Settings()
    if not await probe_pg(settings.pg_dsn):
        pytest.skip("本地 PG 不可达，跳过会话 created_at 读面用例")
    test_dsn = await create_test_database(settings.pg_dsn)
    engine = create_async_engine(test_dsn)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield AsyncUnitOfWork.from_dsn(test_dsn), async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        await drop_test_database(test_dsn)


@pytest.fixture
async def face(
    pg_env: tuple[AsyncUnitOfWork, async_sessionmaker[AsyncSession]],
) -> AsyncIterator[tuple[AsyncUnitOfWork, async_sessionmaker[AsyncSession], Principal, uuid.UUID]]:
    """每用例独立租户/用户/适配器/agent → (uow, 裸 session 工厂, 主体, agent_id)。"""
    uow, factory = pg_env
    async with factory() as db, db.begin():
        tenant = TenantORM(
            name=f"b3-租户-{uuid.uuid4().hex[:8]}", slug=f"b3-{uuid.uuid4().hex[:12]}", plan="free", status="active"
        )
        db.add(tenant)
        await db.flush()  # uuid7 PK 在 flush 时分配，依赖行需引用真实 id
        user = UserORM(tenant_id=tenant.id, email=f"{uuid.uuid4().hex[:10]}@it.local", password_hash="it-only")
        adapter = AgentAdapterORM(agent_tool="nanobot", version=f"b3-{uuid.uuid4().hex[:8]}")
        db.add_all([user, adapter])
        await db.flush()
        agent = AgentORM(
            tenant_id=tenant.id,
            name=f"b3-agent-{uuid.uuid4().hex[:8]}",
            agent_tool=adapter.agent_tool,
            adapter_id=adapter.id,
        )
        db.add(agent)
        await db.flush()  # uuid7 PK 在 flush 时分配，依赖行需引用真实 id
        principal = Principal(
            {
                "sub": str(user.id),
                "tenant_id": str(tenant.id),
                "roles": ["member"],
                "scopes": ["session:read", "session:write", "session:chat"],
                "typ": "access",
                "jti": uuid.uuid4().hex,
            }
        )
        agent_id = agent.id
    yield uow, factory, principal, agent_id


async def test_B3_会话读面created_at透出_列表与详情与ORM行一致(face) -> None:
    """B3 验收面：GET /sessions 行 created_at 非 null，且与 ORM 行创建时刻一致（列表+详情+创建回执）。"""
    # Arrange：经标准创建端点建会话（ORM 行落库即带 created_at）
    uow, factory, principal, agent_id = face
    created = await create_session(
        body=SessionCreateIn(agent_id=agent_id, title="B3 靶会话"), principal=principal, uow=uow
    )
    sid = created.id
    async with factory() as db:
        row = await db.get(SessionORM, sid)
    assert row is not None and row.created_at is not None  # 前置：ORM 行确有值
    # Act①：GET /sessions 列表
    listed = await list_sessions(principal=principal, uow=uow)
    out = next(s for s in listed.data if s.id == sid)
    # Assert①：列表行 created_at 非 null 且等于 ORM 行值（修复前恒 null）
    assert out.created_at == row.created_at
    # Act②：GET /sessions/{id} 详情
    detail = await get_session(sid, principal=principal, uow=uow)
    # Assert②：详情同口径
    assert detail.created_at == row.created_at
    # Assert③：创建回执（聚合未回读路径）不强求值——可选字段缺省 None 不炸
    assert hasattr(created, "created_at")

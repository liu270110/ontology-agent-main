# tests/agent/test_bridge_session_map.py
"""adapter_sessions 桥会话映射集成测试（docs/Agent/05 §4.4 + 20 篇 §2.1/§3.1）。

PgAdapterSessionMapper 贯通：put 登记→get 取回、同 (session_id, adapter) 幂等覆盖、
跨 adapter 隔离（同会话可绑 http-generic 与 cli-generic 各一行）、ORM/migration DDL parity
（create_all 建表即含唯一约束与索引——迁移 SQL 镜像同构）。
一次性测试库（pg_testdb 同款机制）；本地 PG 不可达即跳过。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.agent.business.adapters.session_map import PgAdapterSessionMapper
from services.platform.config import Settings
from services.platform.db import registry as _orm_registry  # noqa: F401  全表聚合注册（create_all 需跨模块 FK 解析）
from services.platform.db.base import Base
from tests.agent.pg_testdb import create_test_database, drop_test_database, probe_pg

if sys.platform == "win32":  # psycopg 异步要求 Selector 循环（导入期固定策略，仓库 PG 用例同款）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pytestmark = pytest.mark.integration


@pytest.fixture
async def pg_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """一次性测试库（每用例独立，create_all 建全量表）→ 裸 session 工厂。"""
    settings = Settings()
    if not await probe_pg(settings.pg_dsn):
        pytest.skip("本地 PG 不可达，跳过 adapter_sessions 集成用例")
    test_dsn = await create_test_database(settings.pg_dsn)
    engine = create_async_engine(test_dsn)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        await drop_test_database(test_dsn)


async def test_会话映射_put_get_贯通_同adapter幂等覆盖(pg_session_factory: async_sessionmaker[AsyncSession]) -> None:
    """登记→取回贯通；同 (tenant, session_id, adapter) 再 put 为覆盖（uk 约束下的幂等语义）。"""
    mapper = PgAdapterSessionMapper(pg_session_factory, adapter="http-generic")
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()

    assert await mapper.get(tenant_id=tenant_id, session_id=session_id) is None  # 未登记：None
    await mapper.put(
        tenant_id=tenant_id, session_id=session_id, foreign_id="svc-abc", metadata={"protocol": "custom-rest"}
    )
    assert await mapper.get(tenant_id=tenant_id, session_id=session_id) == "svc-abc"
    await mapper.put(tenant_id=tenant_id, session_id=session_id, foreign_id="svc-xyz")  # 幂等覆盖（非第二行）
    assert await mapper.get(tenant_id=tenant_id, session_id=session_id) == "svc-xyz"


async def test_会话映射_跨adapter隔离_同会话双绑定并存(pg_session_factory: async_sessionmaker[AsyncSession]) -> None:
    """同平台会话可同时绑 http-generic 与 cli-generic（uk 含 adapter 维度；F3/F2 并存语义）。"""
    http_mapper = PgAdapterSessionMapper(pg_session_factory, adapter="http-generic")
    cli_mapper = PgAdapterSessionMapper(pg_session_factory, adapter="cli-generic")
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()

    await http_mapper.put(tenant_id=tenant_id, session_id=session_id, foreign_id="svc-1")
    await cli_mapper.put(tenant_id=tenant_id, session_id=session_id, foreign_id="cli-thread-9")
    assert await http_mapper.get(tenant_id=tenant_id, session_id=session_id) == "svc-1"
    assert await cli_mapper.get(tenant_id=tenant_id, session_id=session_id) == "cli-thread-9"

# tests/mcp/test_a2a_pg_roundtrip.py
"""A2A 任务受理 PG 集成用例（真实 TaskRepository 承载受理路径；本地 PG 不可达即跳过）。

覆盖（api/04 §4 委托数据流的持久化面）：
- message/send → tasks 表真实落行（type=a2a、活跃 Run queued、task.created 事件）；
- tasks/get → 真实读回（working）；tasks/cancel → 聚合方法取消落终态（canceled）；
- 全程审计行（内存汇）留痕。

环境纪律：integration 标记；psycopg 异步要求 Selector 事件循环（Windows）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.agent.data.orm import Run as RunORM
from services.agent.data.orm import Task as TaskORM
from services.agent.data.orm import TaskEvent as TaskEventORM
from services.iam.data.orm import Tenant as TenantORM
from services.mcp.a2a.service import A2aService
from services.mcp.audit import InMemoryAuditSink
from services.platform.config import Settings
from services.platform.db import registry as orm_registry  # noqa: F401  全模块 ORM 入 metadata（FK 解析）
from services.platform.db.uow import AsyncUnitOfWork

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pytestmark = [pytest.mark.integration]

FULL_SCOPES = ("session:read", "session:write", "session:chat")


@pytest.fixture
async def pg_uow() -> AsyncIterator[tuple[AsyncUnitOfWork, async_sessionmaker[AsyncSession], uuid.UUID]]:
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过 A2A 受理集成用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        tenant = TenantORM(name="a2a-it-租户", slug=f"a2a-it-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
    tenant_id = tenant.id
    yield AsyncUnitOfWork(factory), factory, tenant_id
    async with factory() as db, db.begin():  # FK 逆序清理
        for stmt in (
            delete(TaskEventORM).where(TaskEventORM.tenant_id == tenant_id),
            delete(RunORM).where(RunORM.tenant_id == tenant_id),
            delete(TaskORM).where(TaskORM.tenant_id == tenant_id),
            delete(TenantORM).where(TenantORM.id == tenant_id),
        ):
            await db.execute(stmt)
    await engine.dispose()


async def test_a2a_委托受理_真实PG往返(
    pg_uow: tuple[AsyncUnitOfWork, async_sessionmaker[AsyncSession], uuid.UUID],
) -> None:
    uow, _factory, tenant_id = pg_uow
    sink = InMemoryAuditSink()
    service = A2aService(uow=uow, tenant_id=tenant_id, audit_sink=sink)

    # 受理：真实 tasks/runs/task_events 三表落行（聚合方法 start_run 级联）
    accepted = await service.message_send(
        {"message": {"role": "user", "parts": [{"kind": "text", "text": "PG 往返委托"}]}, "skillId": "task-delegate"},
        scopes=FULL_SCOPES,
    )
    task_id = uuid.UUID(accepted["task"]["id"])
    assert accepted["task"]["status"]["state"] == "submitted"

    # 状态回查询（真实读回）+ 取消（聚合方法落终态）
    polled = await service.tasks_get({"taskId": str(task_id)}, scopes=FULL_SCOPES)
    assert polled["task"]["status"]["state"] == "working"
    cancelled = await service.tasks_cancel({"taskId": str(task_id)}, scopes=FULL_SCOPES)
    assert cancelled["task"]["status"]["state"] == "canceled"

    # 审计全留痕（api/04 §5：委托/查询/取消三面）
    tools = {rec.tool for rec in sink.entries}
    assert {"a2a.message/send", "a2a.tasks/get", "a2a.tasks/cancel"} <= tools
    assert all(rec.trace_id for rec in sink.entries)

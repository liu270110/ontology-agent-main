# tests/agent/test_subrun_persistence.py
"""子 Run 持久化集成用例（40 篇 §3.1/§8 R1；2026-10-04）。

断言目标（R1 验收口径）：
- 并行 4 子 Run 落库不违反 uk_runs_one_active（收窄为仅根 Run）且互不丢更新；
- 聚合 save 全量覆写后子 Run 行不丢不覆写（聚合加载 WHERE parent_run_id IS NULL 隔离）；
- 子 Run 独立写入口（create_subrun / update_subrun_status）roundtrip；
- 活跃 Run 预检（find_active_run）与 start_run 断言语义只约束根 Run。

用一次性 PG 测试库承载（禁对共享库 alembic upgrade——新列 schema 由
Base.metadata.create_all 建，tests/agent/pg_testdb.py 说明）；本地 PG 不可达即跳过。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.agent.data.orm import Run as RunORM
from services.agent.domain.model.task import Run, RunStatus, Task, TaskError, TaskEvent
from services.iam.data.orm import Tenant as TenantORM
from services.platform.config import Settings
from services.platform.db import registry as _orm_registry  # noqa: F401  全表聚合注册（create_all 需跨模块 FK 解析）
from services.platform.db.base import Base
from services.platform.db.uow import AsyncUnitOfWork
from tests.agent.pg_testdb import create_test_database, drop_test_database, probe_pg

if sys.platform == "win32":  # psycopg 异步要求 Selector 循环（导入期固定策略）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pytestmark = pytest.mark.integration


@pytest.fixture
async def pg_env() -> AsyncIterator[tuple[AsyncUnitOfWork, async_sessionmaker[AsyncSession]]]:
    """一次性测试库（每用例独立，create_all 建全量表）→ (UoW, 裸 session 工厂)。"""
    settings = Settings()
    if not await probe_pg(settings.pg_dsn):
        pytest.skip("本地 PG 不可达，跳过子 Run 持久化集成用例")
    test_dsn = await create_test_database(settings.pg_dsn)
    engine = create_async_engine(test_dsn)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield AsyncUnitOfWork.from_dsn(test_dsn), async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        await drop_test_database(test_dsn)


async def _seed_tenant(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with factory() as db, db.begin():
        db.add(
            TenantORM(
                id=tenant_id,
                name=f"r1-it-租户-{uuid.uuid4().hex[:8]}",
                slug=f"r1-it-{uuid.uuid4().hex[:12]}",
                plan="free",
                status="active",
            )
        )
    return tenant_id


def _make_subrun(tenant_id: uuid.UUID, task_id: uuid.UUID, parent_id: uuid.UUID, index: int) -> Run:
    return Run(
        tenant_id=tenant_id,
        task_id=task_id,
        parent_run_id=parent_id,
        label=f"sub-{index}",
        goal=f"子任务目标 {index}",
        depth=1,
    )


async def test_并行四子run落库_不违反唯一约束且聚合save不丢(pg_env) -> None:
    """R1 验收：并行 4 子 Run 不撞 uk_runs_one_active；聚合 save（根 Run 收口）后子 Run 行原样。"""
    uow, factory = pg_env
    tenant_id = await _seed_tenant(factory)
    task = Task(tenant_id=tenant_id, type="chat")
    root = task.start_run()
    root.start()  # 受理即认领（queued→running，send_message SSE 路径同口径）：后续 complete 合法
    async with uow.for_tenant(tenant_id) as tx:
        await tx.tasks.save(task)
        await tx.tasks.append_event(task.id, TaskEvent(task_id=task.id, event_type="task.created", data={}))

    subs = [_make_subrun(tenant_id, task.id, root.id, i) for i in range(4)]

    async def _spawn(sub: Run) -> None:
        async with uow.for_tenant(tenant_id) as tx:  # 独立事务=真并发写路径（并行子 Run 各走各的）
            await tx.tasks.create_subrun(sub)

    await asyncio.gather(*(_spawn(s) for s in subs))  # 旧索引（未收窄）下此处必撞 uk_runs_one_active

    async with uow.for_tenant(tenant_id) as tx:
        loaded = await tx.tasks.get(task.id)
        assert loaded is not None
        # 聚合加载隔离：聚合只见根 Run（子 Run 行不进 runs，save 不会全量覆写它们）
        assert [r.id for r in loaded.runs] == [root.id]
        loaded.runs[0].complete(usage={"total_tokens": 7})
        await tx.tasks.save(loaded)  # 全量覆写路径（40 篇 R1：子 Run 不得走此路径——已隔离）

    async with factory() as db:
        rows = (await db.execute(select(RunORM).where(RunORM.task_id == task.id))).scalars().all()
    assert len(rows) == 5  # 1 根 + 4 子：save 未丢未覆写子 Run 行
    sub_rows = [r for r in rows if r.parent_run_id == root.id]
    assert {(r.label, r.goal) for r in sub_rows} == {(f"sub-{i}", f"子任务目标 {i}") for i in range(4)}
    assert all(r.depth == 1 and r.status == "queued" for r in sub_rows)
    root_row = next(r for r in rows if r.parent_run_id is None)
    assert root_row.status == "completed" and root_row.usage == {"total_tokens": 7}


async def test_子run独立写入口_roundtrip(pg_env) -> None:
    """R1 独立写入口：create_subrun 落行 → update_subrun_status 状态推进（终态回填 ended_at）。"""
    uow, factory = pg_env
    tenant_id = await _seed_tenant(factory)
    task = Task(tenant_id=tenant_id, type="chat")
    root = task.start_run()
    async with uow.for_tenant(tenant_id) as tx:
        await tx.tasks.save(task)

    sub = Run(
        tenant_id=tenant_id,
        task_id=task.id,
        parent_run_id=root.id,
        label="检索子代理",
        goal="检索停电相关证据并摘要",
        depth=1,
    )
    async with uow.for_tenant(tenant_id) as tx:
        await tx.tasks.create_subrun(sub)
        assert await tx.tasks.update_subrun_status(sub.id, RunStatus.RUNNING) is True
        assert await tx.tasks.update_subrun_status(sub.id, RunStatus.COMPLETED, usage={"total_tokens": 42}) is True

    async with factory() as db:
        row = await db.get(RunORM, sub.id)
    assert row is not None
    assert row.parent_run_id == root.id and row.tenant_id == tenant_id
    assert row.status == "completed" and row.usage == {"total_tokens": 42}
    assert row.label == "检索子代理" and row.goal == "检索停电相关证据并摘要" and row.depth == 1
    assert row.ended_at is not None  # 终态时间仓储兜底回填（_save_run 同口径）

    # 防御口径：根 Run 误入子 Run 写入口、跨租户写入、未知 id、跨租户更新——结构化拒绝
    with pytest.raises(ValueError):
        async with uow.for_tenant(tenant_id) as tx:
            await tx.tasks.create_subrun(Run(tenant_id=tenant_id, task_id=task.id))  # parent_run_id=None
    with pytest.raises(ValueError):
        async with uow.for_tenant(uuid.uuid4()) as tx:  # 跨租户（save 同口径防御）
            await tx.tasks.create_subrun(sub)
    async with uow.for_tenant(tenant_id) as tx:
        assert await tx.tasks.update_subrun_status(uuid.uuid4(), RunStatus.COMPLETED) is False
    async with uow.for_tenant(uuid.uuid4()) as tx:
        assert await tx.tasks.update_subrun_status(sub.id, RunStatus.COMPLETED) is False


async def test_活跃run预检只认根run(pg_env) -> None:
    """R1：find_active_run 预检与 uk_runs_one_active 同口径（parent_run_id IS NULL）——

    根 Run 终态后，仍在运行的子 Run 不得触发 4102 预检（否则并行子 Run 拒绝新受理）。
    """
    uow, factory = pg_env
    tenant_id = await _seed_tenant(factory)
    task = Task(tenant_id=tenant_id, type="chat")
    root = task.start_run()
    root.start()  # running：后续 complete 合法（04 §3 状态机 queued 不可直达 completed）
    async with uow.for_tenant(tenant_id) as tx:
        await tx.tasks.save(task)
        sub = _make_subrun(tenant_id, task.id, root.id, 0)
        await tx.tasks.create_subrun(sub)
        assert await tx.tasks.update_subrun_status(sub.id, RunStatus.RUNNING) is True
        active = await tx.tasks.find_active_run(task.id)
        assert active is not None and active.id == root.id  # 根活跃：返回根（非子）

    async with uow.for_tenant(tenant_id) as tx:
        loaded = await tx.tasks.get(task.id)
        assert loaded is not None
        loaded.runs[0].complete()
        await tx.tasks.save(loaded)
        assert await tx.tasks.find_active_run(task.id) is None  # 根已终态：子 Run 活跃不触发预检


def test_start_run断言只约束根run() -> None:
    """R1（纯领域）：活跃互斥断言只看根 Run——手工注入的子 Run 不触发 4102。"""
    task = Task(tenant_id=uuid.uuid4(), type="chat")
    sub = Run(
        tenant_id=task.tenant_id,
        task_id=task.id,
        parent_run_id=uuid.uuid4(),
        label="已活跃的子 Run",
        depth=1,
        status=RunStatus.RUNNING,
    )
    task.runs.append(sub)  # 防御性注入场景（正规路径子 Run 不入聚合）
    root = task.start_run()  # 不因「存在活跃子 Run」而拒——PENDING 前置与活跃断言均针对根 Run
    assert task.active_run_id == root.id and root.parent_run_id is None
    with pytest.raises(TaskError):
        task.start_run()  # 根 Run 活跃互斥（既有断言语义保持）


async def test_唯一索引已收窄为根run(pg_env) -> None:
    """索引形状断言：uk_runs_one_active WHERE 含 parent_run_id IS NULL（与 ORM/迁移同文）。"""
    uow, factory = pg_env
    tenant_id = await _seed_tenant(factory)
    task = Task(tenant_id=tenant_id, type="chat")
    task.start_run()
    async with uow.for_tenant(tenant_id) as tx:
        await tx.tasks.save(task)
    async with factory() as db:
        indexdef = (
            await db.execute(sa.text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uk_runs_one_active'"))
        ).scalar_one()
    assert "parent_run_id IS NULL" in indexdef
    # 收窄后仍保留活跃态过滤（旧口径不回退）
    assert all(f"'{s}'" in indexdef for s in ("queued", "running", "waiting_tool"))

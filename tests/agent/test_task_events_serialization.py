# tests/agent/test_task_events_serialization.py
"""task_events 写序串行化集成用例（40 篇 §8 R11；2026-10-04）。

断言目标（R11 验收口径）：
- asyncio 并发 50 追加同一任务零丢失且 seq 连续（per-task 任务行 FOR UPDATE 串行化）；
- replay_root=True（执行结构事件，回放根）追加失败经 SAVEPOINT 重试成功且不吞；
- 默认路径（replay_root=False）保持既有调用方行为：一次尝试、失败随事务上抛。

并发窗口的旧 max(seq) 读数用受控注入模拟（首个 _insert_event 调用按过期 seq 真实
INSERT，真实撞 uk_task_events_task_id_seq），不模拟时间竞态、结果确定。用一次性 PG
测试库承载（tests/agent/pg_testdb.py）；本地 PG 不可达即跳过。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.agent.data.orm import TaskEvent as TaskEventORM
from services.agent.data.repo_impl.session_repo import _APPEND_RETRIES, PgTaskRepository
from services.agent.domain.model.task import Task, TaskEvent
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
        pytest.skip("本地 PG 不可达，跳过 task_events 串行化集成用例")
    test_dsn = await create_test_database(settings.pg_dsn)
    engine = create_async_engine(test_dsn)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield AsyncUnitOfWork.from_dsn(test_dsn), async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        await drop_test_database(test_dsn)


async def _seed_task(
    env: tuple[AsyncUnitOfWork, async_sessionmaker[AsyncSession]],
) -> tuple[uuid.UUID, uuid.UUID]:
    """种子租户 + 任务行（task_events FK 依赖），返回 (tenant_id, task_id)。"""
    uow, factory = env
    tenant_id = uuid.uuid4()
    async with factory() as db, db.begin():
        db.add(
            TenantORM(
                id=tenant_id,
                name=f"r11-it-租户-{uuid.uuid4().hex[:8]}",
                slug=f"r11-it-{uuid.uuid4().hex[:12]}",
                plan="free",
                status="active",
            )
        )
    task = Task(tenant_id=tenant_id, type="chat")
    async with uow.for_tenant(tenant_id) as tx:
        await tx.tasks.save(task)
    return tenant_id, task.id


async def test_并发五十追加_零丢失且seq连续(pg_env) -> None:
    """R11 验收：50 并发追加同一任务，uk 零冲突丢失、seq 0..49 连续无空洞无重复。"""
    uow, _ = pg_env
    tenant_id, task_id = await _seed_task(pg_env)

    async def _append(i: int) -> int:
        async with uow.for_tenant(tenant_id) as tx:  # 每协程独立事务=真并发写（旧无锁实现必撞 uk）
            return await tx.tasks.append_event(
                task_id, TaskEvent(task_id=task_id, event_type="subrun.started", data={"i": i})
            )

    seqs = await asyncio.gather(*(_append(i) for i in range(50)))
    assert sorted(seqs) == list(range(50))  # 分配即唯一且从 0 连续

    async with uow.for_tenant(tenant_id) as tx:
        rows = await tx.tasks.list_events(task_id, limit=100)
    assert [r.seq for r in rows] == list(range(50))  # 落库核验：零丢失、连续、严格递增


async def test_replay_root追加_冲突重试成功不吞(pg_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """replay_root=True（回放根）：首撞 uk → SAVEPOINT 回滚重试 → 重读 max 成功落库。"""
    uow, _ = pg_env
    tenant_id, task_id = await _seed_task(pg_env)
    async with uow.for_tenant(tenant_id) as tx:  # 已提交 seq=0（冲突源）
        await tx.tasks.append_event(task_id, TaskEvent(task_id=task_id, event_type="run.started", data={}))

    real_insert = PgTaskRepository._insert_event
    calls = {"n": 0}

    async def poisoned_insert(self: PgTaskRepository, tid: uuid.UUID, event: TaskEvent) -> tuple[int, datetime]:
        calls["n"] += 1
        if calls["n"] == 1:
            # 模拟并发窗口旧读数：按已占用 seq=0 真实 INSERT，真实撞 uk_task_events_task_id_seq
            self._db.add(
                TaskEventORM(
                    tenant_id=self._tenant_id,
                    task_id=tid,
                    seq=0,
                    event_type=event.event_type,
                    data=event.data,
                    created_at=datetime.now(UTC),
                )
            )
            await self._db.flush()  # → IntegrityError（seq=0 已提交）
        return await real_insert(self, tid, event)

    monkeypatch.setattr(PgTaskRepository, "_insert_event", poisoned_insert)

    async with uow.for_tenant(tenant_id) as tx:
        seq = await tx.tasks.append_event(
            task_id, TaskEvent(task_id=task_id, event_type="subrun.started", data={}), replay_root=True
        )
    assert seq == 1  # 重试后按重读 max+1 落库（不可吞：吞了返回值无从谈起）

    async with uow.for_tenant(tenant_id) as tx:
        rows = await tx.tasks.list_events(task_id, limit=10)
    assert [r.seq for r in rows] == [0, 1]  # 冲突行随 SAVEPOINT 回滚，重试行落位，无空洞


async def test_replay_root追加_重试耗尽仍失败则上抛(pg_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """replay_root=True 持续冲突：_APPEND_RETRIES 次后上抛 IntegrityError（不静默吞）。"""
    uow, _ = pg_env
    tenant_id, task_id = await _seed_task(pg_env)
    async with uow.for_tenant(tenant_id) as tx:
        await tx.tasks.append_event(task_id, TaskEvent(task_id=task_id, event_type="run.started", data={}))

    attempts = {"n": 0}

    async def always_poison(self: PgTaskRepository, tid: uuid.UUID, event: TaskEvent) -> tuple[int, datetime]:
        attempts["n"] += 1
        self._db.add(
            TaskEventORM(
                tenant_id=self._tenant_id,
                task_id=tid,
                seq=0,  # 恒撞已提交 seq=0
                event_type=event.event_type,
                data=event.data,
                created_at=datetime.now(UTC),
            )
        )
        await self._db.flush()
        raise AssertionError("不可达：flush 应先因 uk 冲突失败")

    monkeypatch.setattr(PgTaskRepository, "_insert_event", always_poison)

    with pytest.raises(IntegrityError):
        async with uow.for_tenant(tenant_id) as tx:
            await tx.tasks.append_event(
                task_id, TaskEvent(task_id=task_id, event_type="subrun.started", data={}), replay_root=True
            )
    assert attempts["n"] == _APPEND_RETRIES  # 耗尽即上抛，不无限重试

    async with uow.for_tenant(tenant_id) as tx:
        rows = await tx.tasks.list_events(task_id, limit=10)
    assert [r.seq for r in rows] == [0]  # 失败追加不残留半行（事务回滚，账本不受污染）


async def test_默认路径_失败即上抛_既有行为不变(pg_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """replay_root=False（默认）：一次尝试、失败随事务上抛——既有调用方行为保持不变。"""
    uow, _ = pg_env
    tenant_id, task_id = await _seed_task(pg_env)
    async with uow.for_tenant(tenant_id) as tx:
        await tx.tasks.append_event(task_id, TaskEvent(task_id=task_id, event_type="run.started", data={}))

    attempts = {"n": 0}

    async def poisoned_insert(self: PgTaskRepository, tid: uuid.UUID, event: TaskEvent) -> tuple[int, datetime]:
        attempts["n"] += 1
        self._db.add(
            TaskEventORM(
                tenant_id=self._tenant_id,
                task_id=tid,
                seq=0,
                event_type=event.event_type,
                data=event.data,
                created_at=datetime.now(UTC),
            )
        )
        await self._db.flush()
        raise AssertionError("不可达：flush 应先因 uk 冲突失败")

    monkeypatch.setattr(PgTaskRepository, "_insert_event", poisoned_insert)

    with pytest.raises(IntegrityError):
        async with uow.for_tenant(tenant_id) as tx:
            await tx.tasks.append_event(task_id, TaskEvent(task_id=task_id, event_type="task.cancelled", data={}))
    assert attempts["n"] == 1  # 无重试（默认路径不走 SAVEPOINT 循环）

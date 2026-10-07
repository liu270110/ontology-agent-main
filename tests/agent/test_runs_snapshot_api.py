# tests/agent/test_runs_snapshot_api.py
"""GET /runs/{run_id}/subruns 子 Run 快照端点用例（40 篇 §4.4/§8 R3；2026-10-04）。

断言目标（R3 验收口径）：
- 快照与落库一致：端点行逐字段对齐 runs 表行（id/parent_run_id/label/goal/depth/status/
  started_at/ended_at/duration_ms/usage），artifact 摘要 v1 恒 None（runs 表无产物列）；
- 扁平后代列表（含嵌套孙 Run），按 depth、started_at 排序（树由前端派生，40 篇 §2.4）；
- 404 语义：run 不存在/跨租户一律 404（TenantMixin 口径）；无子 Run → 空 items；
- 权限路径：scope 门禁 session:read（deny-by-default → 403+2001，08 §2.5），经最小
  FastAPI app + GlobalExceptionMiddleware 走真实依赖链（stub UoW，零存储触达）。

PG 集成用一次性测试库（禁对共享库 alembic upgrade——schema 由 Base.metadata.create_all 建，
tests/agent/pg_testdb.py 说明）；本地 PG 不可达即跳过。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.agent.api.runs import list_subruns
from services.agent.api.runs import router as runs_router
from services.agent.data.orm import Run as RunORM
from services.agent.domain.model.task import Run, RunStatus, Task
from services.gateway.middlewares import GlobalExceptionMiddleware
from services.iam.data.orm import Tenant as TenantORM
from services.platform.config import Settings
from services.platform.db import registry as _orm_registry  # noqa: F401  全表聚合注册（create_all 需跨模块 FK 解析）
from services.platform.db.base import Base
from services.platform.db.uow import AsyncUnitOfWork
from services.platform.deps import Principal, get_current_principal, get_uow
from services.platform.errors import GatewayError
from tests.agent.pg_testdb import create_test_database, drop_test_database, probe_pg

if sys.platform == "win32":  # psycopg 异步要求 Selector 循环（导入期固定策略）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 10, 4, 10, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 10, 4, 10, 1, 0, tzinfo=UTC)


@pytest.fixture
async def pg_env() -> AsyncIterator[tuple[AsyncUnitOfWork, async_sessionmaker[AsyncSession]]]:
    """一次性测试库（每用例独立，create_all 建全量表）→ (UoW, 裸 session 工厂)。"""
    settings = Settings()
    if not await probe_pg(settings.pg_dsn):
        pytest.skip("本地 PG 不可达，跳过子 Run 快照端点用例")
    test_dsn = await create_test_database(settings.pg_dsn)
    engine = create_async_engine(test_dsn)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield AsyncUnitOfWork.from_dsn(test_dsn), async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        await drop_test_database(test_dsn)


def _principal(tenant_id: uuid.UUID, *, scopes: list[str] | None = None) -> Principal:
    return Principal(
        {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(tenant_id),
            "roles": ["member"],
            "scopes": scopes if scopes is not None else ["session:read", "session:write", "session:chat"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


async def _seed_tenant(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with factory() as db, db.begin():
        db.add(
            TenantORM(
                id=tenant_id,
                name=f"r3-it-租户-{uuid.uuid4().hex[:8]}",
                slug=f"r3-it-{uuid.uuid4().hex[:12]}",
                plan="free",
                status="active",
            )
        )
    return tenant_id


async def _seed_task_with_subruns(uow: AsyncUnitOfWork, tenant_id: uuid.UUID) -> tuple[Task, Run, list[Run]]:
    """建任务 + 根 Run + 三个子 Run，返回 (task, root, [sub_a, sub_b, sub_c])。

    - sub_a：depth 1，10:01 起，终态 completed（usage/ended_at 由 update_subrun_status 落列）；
    - sub_b：depth 1，10:00 起，running——排序应在 sub_a 之前（started_at 升序）；
    - sub_c：depth 2（parent=sub_a，嵌套派发），10:00 起，completed。
    """
    async with uow.for_tenant(tenant_id) as tx:
        task = Task(tenant_id=tenant_id, type="chat")
        root = task.start_run()
        root.start()
        await tx.tasks.save(task)
        sub_a = Run(
            tenant_id=tenant_id,
            task_id=task.id,
            parent_run_id=root.id,
            label="数据抽取员",
            goal="从工单正文抽取停电时间与范围",
            depth=1,
            status=RunStatus.RUNNING,
            started_at=_T1,
        )
        await tx.tasks.create_subrun(sub_a)
        sub_b = Run(
            tenant_id=tenant_id,
            task_id=task.id,
            parent_run_id=root.id,
            label="证据检索员",
            goal="检索停电相关证据",
            depth=1,
            status=RunStatus.RUNNING,
            started_at=_T0,
        )
        await tx.tasks.create_subrun(sub_b)
        sub_c = Run(
            tenant_id=tenant_id,
            task_id=task.id,
            parent_run_id=sub_a.id,  # 嵌套派发（40 篇 §3.1 树深 ≤ max_spawn_depth）
            label="规则核对员",
            goal="核对抽取结果与规则一致性",
            depth=2,
            status=RunStatus.RUNNING,
            started_at=_T0,
        )
        await tx.tasks.create_subrun(sub_c)
        assert await tx.tasks.update_subrun_status(sub_a.id, RunStatus.COMPLETED, usage={"total_tokens": 966})
        assert await tx.tasks.update_subrun_status(sub_c.id, RunStatus.COMPLETED, usage={"total_tokens": 42})
    return task, root, [sub_a, sub_b, sub_c]


# ── 快照与落库一致 ───────────────────────────────────────────────────────


async def test_快照与落库一致_逐字段对齐(pg_env) -> None:
    uow, factory = pg_env
    tenant_id = await _seed_tenant(factory)
    task, root, subs = await _seed_task_with_subruns(uow, tenant_id)

    out = await list_subruns(root.id, principal=_principal(tenant_id), uow=uow)
    _, sub_b, sub_c = subs
    assert [i.id for i in out.items] == [sub_b.id, subs[0].id, sub_c.id]  # 保序：depth、started_at
    async with factory() as db:  # 与 runs 表行逐字段对账（R3 验收：快照与落库一致）
        rows = {r.id: r for r in (await db.execute(select(RunORM).where(RunORM.task_id == task.id))).scalars().all()}
    by_id = {i.id: i for i in out.items}
    for sub in subs:
        row = rows[sub.id]
        item = by_id[sub.id]
        assert item.parent_run_id == row.parent_run_id
        assert (item.label, item.goal, item.depth) == (row.label, row.goal, row.depth)
        assert item.status == row.status
        assert item.started_at == row.started_at and item.ended_at == row.ended_at
        assert item.usage == (row.usage or {})
        assert item.artifact is None  # v1：runs 表无产物列（协议形状先登记）
    a = by_id[subs[0].id]
    assert a.duration_ms == int((a.ended_at - a.started_at).total_seconds() * 1000)  # completed：耗时毫秒
    assert by_id[subs[1].id].duration_ms is None  # running：ended_at 缺 → None
    assert root.id not in by_id  # 快照只含后代（根自己经 GET /tasks/{id} 展开）


async def test_排序_depth优先_started_at次之(pg_env) -> None:
    uow, _factory = pg_env
    tenant_id = await _seed_tenant(_factory)
    _, root, subs = await _seed_task_with_subruns(uow, tenant_id)
    sub_a, sub_b, sub_c = subs
    out = await list_subruns(root.id, principal=_principal(tenant_id), uow=uow)
    # depth 1 两行按 started_at 升序（sub_b 10:00 → sub_a 10:01），depth 2 殿后
    assert [i.id for i in out.items] == [sub_b.id, sub_a.id, sub_c.id]


async def test_嵌套孙Run入快照_祖先链可达即后代(pg_env) -> None:
    uow, _factory = pg_env
    tenant_id = await _seed_tenant(_factory)
    _, root, subs = await _seed_task_with_subruns(uow, tenant_id)
    sub_a, _, sub_c = subs
    # 以中间子 Run 为锚：只返回其自己的后代（孙 Run），不含兄弟分支
    out = await list_subruns(sub_a.id, principal=_principal(tenant_id), uow=uow)
    assert [i.id for i in out.items] == [sub_c.id]


async def test_无子run_空items(pg_env) -> None:
    uow, _factory = pg_env
    tenant_id = await _seed_tenant(_factory)
    async with uow.for_tenant(tenant_id) as tx:
        task = Task(tenant_id=tenant_id, type="chat")
        root = task.start_run()
        await tx.tasks.save(task)
    out = await list_subruns(root.id, principal=_principal(tenant_id), uow=uow)
    assert out.items == []


# ── 404 语义（不存在/跨租户一律 404）────────────────────────────────────


async def test_404_run不存在(pg_env) -> None:
    uow, _factory = pg_env
    tenant_id = await _seed_tenant(_factory)
    with pytest.raises(GatewayError) as ei:
        await list_subruns(uuid.uuid4(), principal=_principal(tenant_id), uow=uow)
    assert ei.value.status_code == 404


async def test_404_跨租户run不泄露存在性(pg_env) -> None:
    uow, _factory = pg_env
    tenant_a = await _seed_tenant(_factory)
    tenant_b = await _seed_tenant(_factory)
    _, other_root, _ = await _seed_task_with_subruns(uow, tenant_b)  # 受害者行挂租户 B
    with pytest.raises(GatewayError) as ei:  # 租户 A 查租户 B 的 run：与不存在同形 404
        await list_subruns(other_root.id, principal=_principal(tenant_a), uow=uow)
    assert ei.value.status_code == 404


# ── 权限路径（scope 门禁 session:read；真实依赖链 + stub UoW）────────────


class _StubTaskRepo:
    async def list_subruns(self, run_id: UUID) -> list[Run]:
        return []


class _StubTx:
    tasks = _StubTaskRepo()

    async def __aenter__(self) -> _StubTx:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _StubUow:
    def for_tenant(self, tenant_id: UUID) -> _StubTx:
        return _StubTx()


def _client_with_principal(principal: Principal, *, uow: object | None = None) -> TestClient:
    """最小 app 走真实依赖链：require_scope("session:read") + GlobalExceptionMiddleware。

    get_uow 一并覆盖（403 路径零存储触达；依赖求解序不依赖参数声明序）。
    """
    app = FastAPI()
    app.include_router(runs_router)
    app.add_middleware(GlobalExceptionMiddleware)
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_uow] = lambda: uow if uow is not None else _StubUow()
    return TestClient(app)


def test_权限_无session_read_403_2001() -> None:
    principal = _principal(uuid.uuid4(), scopes=["session:write"])  # 缺 session:read
    resp = _client_with_principal(principal).get(f"/runs/{uuid.uuid4()}/subruns")
    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert body["code"] == 2001 and body["detail"]["required"] == "session:read"  # deny-by-default（08 §2.5）


def test_权限_持有session_read_走通端点_200空快照() -> None:
    """正对照：session:read 通过门禁进端点体 → stub UoW 空列表 → 200 {items: []}（信封形状）。"""
    principal = _principal(uuid.uuid4(), scopes=["session:read"])
    resp = _client_with_principal(principal).get(f"/runs/{uuid.uuid4()}/subruns")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": []}


def test_权限_匿名主体_401() -> None:
    """JWT 中间件未注入 claims → 401+1001（门禁依赖链第一环，403 之前）。"""
    app = FastAPI()
    app.include_router(runs_router)
    app.add_middleware(GlobalExceptionMiddleware)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(f"/runs/{uuid.uuid4()}/subruns")
    assert resp.status_code == 401, resp.text
    assert resp.json()["code"] == 1001

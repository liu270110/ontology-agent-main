# tests/agent/test_task_sessionless_read_b1.py
"""session-less 任务读面归属测试（P1-B1 缺陷修复 2026-10-07：工作流任务读面 404）。

缺陷：deps.get_task_owned 对 ``session_id IS NULL`` 一律 404，GET /tasks 列表的 user
过滤经会话 EXISTS 同样漏 session-less 任务——工作流任务恒 session_id=None（X16
executor 契约），任务中心读面（列表/详情/事件）全 404。

修法（盘点结论：tasks ORM 无 user/created_by 列，最小改动=复用既有触发者留痕
``payload.triggered_by``——workflow 受理端点 submit 落行，零迁移）：归属双路判定
（会话锚 + 触发者锚），deps.get_task_owned 与 repo list/count 同口径。

用一次性 PG 测试库承载（禁对共享库 alembic upgrade——schema 由 Base.metadata.create_all
建，tests/agent/pg_testdb.py 同款）；本地 PG 不可达即跳过。端点直调（Depends 显式传参
等价，tests/gateway/test_session_ownership_a2.py 同口径）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.agent.api.tasks import get_task, list_task_events, list_tasks
from services.agent.data.orm import Agent as AgentORM
from services.agent.data.orm import AgentAdapter as AgentAdapterORM
from services.agent.domain.model.task import Task, TaskEvent
from services.gateway.middlewares import GatewayError
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


# ---------------------------------------------------------------- 夹具与种子


@pytest.fixture
async def pg_env() -> AsyncIterator[tuple[AsyncUnitOfWork, async_sessionmaker[AsyncSession]]]:
    """一次性测试库（每用例独立，create_all 建全量表）→ (UoW, 裸 session 工厂)。"""
    from services.platform.config import Settings

    settings = Settings()
    if not await probe_pg(settings.pg_dsn):
        pytest.skip("本地 PG 不可达，跳过 session-less 任务读面用例")
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
async def seeded(
    pg_env: tuple[AsyncUnitOfWork, async_sessionmaker[AsyncSession]],
) -> AsyncIterator[tuple[AsyncUnitOfWork, Principal, Principal, Task, uuid.UUID]]:
    """租户 + 双主体（owner/同租户攻击者）+ agent + session-less 工作流任务（含事件）。

    任务种子=WorkflowRunControl.submit 落行形态（X16）：session_id=None +
    payload.triggered_by=owner（触发者留痕）。yield (uow, owner, attacker, task, agent_id)。
    """
    uow, factory = pg_env
    async with factory() as db, db.begin():
        tenant = TenantORM(
            name=f"b1-租户-{uuid.uuid4().hex[:8]}", slug=f"b1-{uuid.uuid4().hex[:12]}", plan="free", status="active"
        )
        db.add(tenant)
        await db.flush()  # uuid7 PK 在 flush 时分配，依赖行需引用真实 id
        owner_row = UserORM(tenant_id=tenant.id, email=f"{uuid.uuid4().hex[:10]}@it.local", password_hash="it-only")
        attacker_row = UserORM(tenant_id=tenant.id, email=f"{uuid.uuid4().hex[:10]}@it.local", password_hash="it-only")
        adapter = AgentAdapterORM(agent_tool="nanobot", version=f"b1-{uuid.uuid4().hex[:8]}")
        db.add_all([owner_row, attacker_row, adapter])
        await db.flush()
        agent = AgentORM(
            tenant_id=tenant.id,
            name=f"b1-agent-{uuid.uuid4().hex[:8]}",
            agent_tool=adapter.agent_tool,
            adapter_id=adapter.id,
        )
        db.add(agent)
        await db.flush()

        def make_principal(user_id: uuid.UUID) -> Principal:
            return Principal(
                {
                    "sub": str(user_id),
                    "tenant_id": str(tenant.id),
                    "roles": ["member"],
                    "scopes": ["session:read", "session:write", "session:chat"],
                    "typ": "access",
                    "jti": uuid.uuid4().hex,
                }
            )

        owner, attacker = make_principal(owner_row.id), make_principal(attacker_row.id)
        tenant_id, workflow_id = tenant.id, uuid.uuid4()
        owner_id = owner_row.id

    task = Task(
        tenant_id=tenant_id,
        type="workflow_run",
        session_id=None,  # 工作流任务无会话语义（X16 executor 契约——B1 缺陷触发面）
        payload={
            "workflow_id": str(workflow_id),
            "kind": "workflow_run",
            "version": 1,
            "triggered_by": str(owner_id),  # 触发者留痕（submit 落行口径，归属锚）
            "origin_trace_id": "b1-trace",
        },
    )
    task.start_run()
    async with uow.for_tenant(tenant_id) as tx:
        await tx.tasks.save(task)
        await tx.tasks.append_event(
            task.id,
            TaskEvent(
                task_id=task.id,
                event_type="task.created",
                data={"workflow_id": str(workflow_id), "kind": "workflow_run", "version": 1},
            ),
        )
    yield uow, owner, attacker, task, agent.id


# ---------------------------------------------------------------- 三端点归属读面


async def test_B1_工作流任务三端点_owner可读_详情事件列表(seeded) -> None:
    """B1 验收面：session-less 工作流任务 owner 三端点全通（修复前详情/事件恒 404、列表漏行）。"""
    # Arrange：种子工作流任务（session_id=None + triggered_by=owner）
    uow, owner, _attacker, task, _agent_id = seeded
    # Act①：GET /tasks/{id} 详情
    detail = await get_task(task.id, principal=owner, uow=uow)
    # Assert①：详情可读，payload/触发者留痕随载
    assert detail.id == task.id
    assert detail.session_id is None
    assert detail.type == "workflow_run"
    assert detail.payload["triggered_by"] == str(owner.user_id)
    assert any(r.id == task.active_run_id for r in detail.runs)
    # Act②：GET /tasks/{id}/events（request=None → JSON 形态）
    events = await list_task_events(task.id, principal=owner, uow=uow)
    # Assert②：事件帧可读（task.created 落库回放，seq/created_at 齐）
    assert [e.event_type for e in events.items] == ["task.created"]
    assert events.items[0].seq == 0 and events.next_after_seq == 0
    assert events.items[0].created_at is not None
    # Act③：GET /tasks 列表
    page = await list_tasks(principal=owner, uow=uow)
    # Assert③：列表含工作流任务（修复前经会话 EXISTS 漏行）
    assert task.id in {t.id for t in page.data}
    assert page.meta.total >= 1


async def test_B1_同租户攻击者_三端点同形404_列表不可见(seeded) -> None:
    """B1 修复不回退 A2 反探测面：非触发者同租户用户详情/事件 404（与不存在同形）、列表不可见。"""
    # Arrange：同租户攻击者（非 triggered_by）
    uow, _owner, attacker, task, _agent_id = seeded
    # Act + Assert：详情/事件 404（防存在性探测，与「任务不存在」同形）
    with pytest.raises(GatewayError) as ei_detail:
        await get_task(task.id, principal=attacker, uow=uow)
    assert (ei_detail.value.code, ei_detail.value.status_code) == (404, 404)
    with pytest.raises(GatewayError) as ei_events:
        await list_task_events(task.id, principal=attacker, uow=uow)
    assert (ei_events.value.code, ei_events.value.status_code) == (404, 404)
    # 列表面：攻击者任务列表不见该任务（user_id 归属过滤双路口径）
    attacker_page = await list_tasks(principal=attacker, uow=uow)
    assert all(t.id != task.id for t in attacker_page.data)
    assert attacker_page.meta.total == 0


async def test_B1_触发者锚不误扩_无留痕sessionless任务不可见_会话锚路径不回退(seeded) -> None:
    """归属谓词负向对照：无 triggered_by 的 session-less 任务（如 a2a 形态）两路皆不中 →
    owner 亦不可见（不因放开展开误扩）；会话锚任务（chat）owner 照常可读（原路径零回退）。"""
    # Arrange①：无留痕 session-less 任务（payload 无 triggered_by）
    uow, owner, _attacker, _task, _agent_id = seeded
    bare = Task(tenant_id=owner.tenant_id, type="a2a", session_id=None, payload={"delegate": "x"})
    async with uow.for_tenant(owner.tenant_id) as tx:
        await tx.tasks.save(bare)
    # Assert①：owner 亦不可见（列表不漏进 + 详情 404 同形）
    page = await list_tasks(principal=owner, uow=uow)
    assert all(t.id != bare.id for t in page.data)
    with pytest.raises(GatewayError) as ei_bare:
        await get_task(bare.id, principal=owner, uow=uow)
    assert ei_bare.value.status_code == 404
    # Arrange②：会话锚任务（chat，session 归属 owner）
    from services.agent.domain.model.session import Session as DomainSession

    session = DomainSession(id=uuid.uuid4(), tenant_id=owner.tenant_id, agent_id=_agent_id, user_id=owner.user_id)
    chat_task = Task(tenant_id=owner.tenant_id, type="chat", session_id=session.id)
    chat_task.start_run()
    async with uow.for_tenant(owner.tenant_id) as tx:
        await tx.sessions.add(session)
        await tx.tasks.save(chat_task)
    # Assert②：会话锚路径不回退——列表含 chat 任务、详情可读
    page2 = await list_tasks(principal=owner, uow=uow)
    assert chat_task.id in {t.id for t in page2.data}
    detail2 = await get_task(chat_task.id, principal=owner, uow=uow)
    assert detail2.id == chat_task.id and detail2.session_id == session.id

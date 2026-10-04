# tests/agent/test_session_user_face.py
"""会话用户面集成用例（M4.6-D2 G-07：FTS 检索 + 标题定题 + rewind；docs/Agent/13 §2）。

断言目标（详设 §2.5 全列，中文 AAA）：
- 检索：英文 query 走 simple tsvector 命中 / 中文 query 经 pg_trgm 相似兜底命中 /
  无命中空列表 / 空 query 行为不变；page/page_size 语义不变、count 同过滤口径；
- 定题：首条用户消息落库触发确定性定题（strip 后前 32 字符，空则「新会话」）+
  用户显式 PATCH 的 title 不被自动定题覆盖（title_generated 单向闸）；
- rewind：软删后 GET messages 不复活 + last_message_at 回退到边界前 + 检索面退出
  被删正文 + L1 三键清空（fakeredis）+ 重复同锚幂等（零新增软删）+ closed 4101 +
  非法锚 4106 + 审计行 session.rewound 落 task_events。

用一次性 PG 测试库承载（禁对共享库 alembic upgrade——schema 由 Base.metadata.create_all
建，tests/agent/pg_testdb.py 建库即预装 pg_trgm 供两检索索引创建）；本地 PG 不可达即跳过。
端点均为直调（Depends 参数显式传参等价，tests/gateway/test_sessions.py 同口径）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator

import fakeredis.aioredis
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.agent.api.schemas.session import SessionPatchIn, SessionRewindIn
from services.agent.api.sessions import (
    close_session,
    list_messages,
    list_sessions,
    patch_session,
    rewind_session,
)
from services.agent.data.orm import Agent as AgentORM
from services.agent.data.orm import AgentAdapter as AgentAdapterORM
from services.agent.data.orm import Message as MessageORM
from services.agent.data.orm import Session as SessionORM
from services.agent.data.orm import TaskEvent as TaskEventORM
from services.agent.domain.model.session import Message, Session
from services.agent.domain.model.task import Task
from services.gateway.middlewares import GatewayError
from services.iam.data.orm import Tenant as TenantORM
from services.iam.data.orm import User as UserORM
from services.memory.business.runtime import build_l1_store
from services.memory.data.l1 import RedisL1Store
from services.memory.domain.model.l1 import MemoryBlock, WindowMessage
from services.platform.config import Settings
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
    settings = Settings()
    if not await probe_pg(settings.pg_dsn):
        pytest.skip("本地 PG 不可达，跳过会话用户面集成用例")
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
) -> AsyncIterator[tuple[AsyncUnitOfWork, async_sessionmaker[AsyncSession], Principal, uuid.UUID, uuid.UUID]]:
    """每用例独立租户/用户/适配器/agent → (uow, factory, 读写主体, agent_id, tenant_id)。"""
    uow, factory = pg_env
    async with factory() as db, db.begin():
        tenant = TenantORM(
            name=f"uf-租户-{uuid.uuid4().hex[:8]}", slug=f"uf-{uuid.uuid4().hex[:12]}", plan="free", status="active"
        )
        db.add(tenant)
        await db.flush()  # uuid7 PK 在 flush 时分配，依赖行需引用真实 id
        user = UserORM(tenant_id=tenant.id, email=f"{uuid.uuid4().hex[:10]}@it.local", password_hash="it-only")
        adapter = AgentAdapterORM(agent_tool="nanobot", version=f"uf-{uuid.uuid4().hex[:8]}")
        db.add_all([user, adapter])
        await db.flush()
        agent = AgentORM(
            tenant_id=tenant.id,
            name=f"uf-agent-{uuid.uuid4().hex[:8]}",
            agent_tool=adapter.agent_tool,
            adapter_id=adapter.id,
        )
        db.add(agent)
        await db.flush()
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
        tenant_id, agent_id = tenant.id, agent.id
    yield uow, factory, principal, agent_id, tenant_id


async def _new_session(
    uow: AsyncUnitOfWork, tenant_id: uuid.UUID, user_id: uuid.UUID, agent_id: uuid.UUID
) -> uuid.UUID:
    """建会话（title 留空=定题前置条件；channel/type/routing 全默认）。"""
    session = Session(id=uuid.uuid4(), tenant_id=tenant_id, agent_id=agent_id, user_id=user_id, title=None)
    async with uow.for_tenant(tenant_id) as tx:
        await tx.sessions.add(session)
    return session.id


async def _append(uow: AsyncUnitOfWork, tenant_id: uuid.UUID, session_id: uuid.UUID, role: str, content: str) -> int:
    """仓储直追一条消息（绕编排器）：seq 经聚合分配、状态迁移经 save_meta 持久化——

    与 send_message 同序（先 save_meta 后 append，防定题被陈旧域对象覆写），单写者测试
    环境下与端点路径等价。返回分配的 seq。"""
    async with uow.for_tenant(tenant_id) as tx:
        session = await tx.sessions.get(session_id)
        seq = session.append_message(role, content)  # 4101 断言照走；user 首条 created→active
        await tx.sessions.save_meta(session)
        await tx.sessions.append_message(
            session_id, Message(session_id=session_id, seq=seq, role=role, content=content)
        )
    return seq


async def _seed_history(
    uow: AsyncUnitOfWork, tenant_id: uuid.UUID, session_id: uuid.UUID, *, with_task: bool
) -> Task | None:
    """四轮历史（seq 0/1/2/3 = user/assistant/user/assistant）；with_task 再挂一个任务作审计载体。"""
    await _append(uow, tenant_id, session_id, "user", "分析昨夜城东线路的停电原因")
    await _append(uow, tenant_id, session_id, "assistant", "结论：雷击导致 110kV 城东线跳闸")
    await _append(uow, tenant_id, session_id, "user", "那抢修建议呢")
    await _append(uow, tenant_id, session_id, "assistant", "建议先巡 110kV 城东线再试送电")
    if not with_task:
        return None
    task = Task(tenant_id=tenant_id, type="chat", session_id=session_id)
    async with uow.for_tenant(tenant_id) as tx:
        await tx.tasks.save(task)
    return task


# ---------------------------------------------------------------- 检索（GET /sessions?query=）


async def test_检索_英文query命中_user与assistant正文均入检索面(face) -> None:
    """详设 §2.5：英文 query 命中（simple tsvector）；user/assistant 正文均入。"""
    # Arrange：A 会话英文双轮（user 问 transformer、assistant 答 harmonic）；B 会话中文对照
    uow, _factory, principal, agent_id, tenant_id = face
    sid_a = await _new_session(uow, tenant_id, principal.user_id, agent_id)
    await _append(uow, tenant_id, sid_a, "user", "please analyze the transformer overload incident")
    await _append(uow, tenant_id, sid_a, "assistant", "the overload was caused by harmonic distortion")
    sid_b = await _new_session(uow, tenant_id, principal.user_id, agent_id)
    await _append(uow, tenant_id, sid_b, "user", "停电分析")
    # Act：query 命中 assistant 独有词（证明 assistant 正文入检索面）
    result = await list_sessions(principal=principal, uow=uow, query="harmonic")
    # Assert：仅 A 命中；分页 meta 同过滤口径
    assert [s.id for s in result.data] == [sid_a]
    assert (result.meta.page, result.meta.page_size, result.meta.total) == (1, 20, 1)


async def test_检索_中文query经trgm兜底命中(face) -> None:
    """详设 §2.5：中文 query 不走 simple 分词（整串单 token），经 pg_trgm 相似度兜底命中。"""
    # Arrange：B 会话仅一条中文短消息（检索面=原文「停电分析」，与 query「停电」三元组
    # 相似度 2/6≈0.333 ≥ 默认阈值 0.3；A 会话英文不受中文 query 干扰）
    uow, _factory, principal, agent_id, tenant_id = face
    sid_a = await _new_session(uow, tenant_id, principal.user_id, agent_id)
    await _append(uow, tenant_id, sid_a, "user", "please analyze the transformer overload incident")
    sid_b = await _new_session(uow, tenant_id, principal.user_id, agent_id)
    await _append(uow, tenant_id, sid_b, "user", "停电分析")
    # Act
    result = await list_sessions(principal=principal, uow=uow, query="停电")
    # Assert：仅 B 命中（英文会话相似度≈0 不混入）
    assert [s.id for s in result.data] == [sid_b]
    assert result.meta.total == 1


async def test_检索_无命中空列表_空query行为不变(face) -> None:
    """详设 §2.5：乱码 query 空命中；None/空白 query 行为与无参完全一致。"""
    # Arrange：三个会话（中英混合）
    uow, _factory, principal, agent_id, tenant_id = face
    ids: set[uuid.UUID] = set()
    for content in ("transformer overload", "停电分析", "会议纪要整理"):
        sid = await _new_session(uow, tenant_id, principal.user_id, agent_id)
        await _append(uow, tenant_id, sid, "user", content)
        ids.add(sid)
    # Act / Assert：乱码 query 空命中（total 同口径=0）
    miss = await list_sessions(principal=principal, uow=uow, query="zzzq不存在")
    assert miss.data == [] and miss.meta.total == 0
    # 空 query（None / 空白串）→ 不过滤，全集照常返回且与无参调用一致
    base = await list_sessions(principal=principal, uow=uow)
    empty = await list_sessions(principal=principal, uow=uow, query=None)
    blank = await list_sessions(principal=principal, uow=uow, query="   ")
    assert {s.id for s in base.data} == ids == {s.id for s in empty.data} == {s.id for s in blank.data}
    assert base.meta.total == empty.meta.total == blank.meta.total == 3


# ---------------------------------------------------------------- 定题


async def test_定题_首条用户消息触发_strip后截32字符_空白兜底新会话(face) -> None:
    """详设 §2.4：title 为空且未生成过 → 首条用户消息落库即定题；strip 后前 32 字符；空白兜「新会话」。"""
    # Arrange：两个无标题会话
    uow, factory, principal, agent_id, tenant_id = face
    sid_long = await _new_session(uow, tenant_id, principal.user_id, agent_id)
    sid_blank = await _new_session(uow, tenant_id, principal.user_id, agent_id)
    full = "分析昨夜城东线路停电的根本原因，并给出抢修优先级排序建议，覆盖 A/B/C 三条馈线"
    assert len(full) > 32  # 守卫：素材确超 32 字符，截断路径生效
    # Act
    await _append(uow, tenant_id, sid_long, "user", f"  {full}  ")  # 前后空白须 strip 后再截
    await _append(uow, tenant_id, sid_blank, "user", "   ")  # strip 后为空 → 兜底「新会话」
    # Assert
    async with factory() as db:
        row_long = await db.get(SessionORM, sid_long)
        row_blank = await db.get(SessionORM, sid_blank)
    assert row_long.title == full[:32] and row_long.title_generated is True
    assert row_blank.title == "新会话" and row_blank.title_generated is True
    # 第二条用户消息不再重定题（title 非空 + 单向闸）
    await _append(uow, tenant_id, sid_long, "user", "换一个话题聊天气")
    async with factory() as db:
        assert (await db.get(SessionORM, sid_long)).title == full[:32]


async def test_定题_PATCH显式标题后自动定题不触发(face) -> None:
    """详设 §2.4：用户显式 PATCH title 非空 → 定题闸不触发（title_generated 保持 false）。"""
    # Arrange：无标题会话，先 PATCH 显式标题
    uow, factory, principal, agent_id, tenant_id = face
    sid = await _new_session(uow, tenant_id, principal.user_id, agent_id)
    patched = await patch_session(sid, SessionPatchIn(title="自定义标题"), principal=principal, uow=uow)
    assert patched.title == "自定义标题"
    # Act：首条用户消息落库（自动定题唯一触发点）
    await _append(uow, tenant_id, sid, "user", "分析昨夜城东线路的停电原因")
    # Assert：显式标题不被覆盖，title_generated 保持 false（从未自动生成）
    async with factory() as db:
        row = await db.get(SessionORM, sid)
    assert row.title == "自定义标题" and row.title_generated is False


# ---------------------------------------------------------------- rewind


async def test_rewind_软删后历史不复活_last_message_at回退_检索面退出被删正文(face) -> None:
    """详设 §2.3 主链路：seq>=before_seq 软删（物理保留）+ 历史不可见 + last_message_at
    回退边界前 + 检索面同步退出被删正文 + 重复同锚幂等零新增软删。"""
    # Arrange：四轮历史（seq 0~3）
    uow, factory, principal, agent_id, tenant_id = face
    sid = await _new_session(uow, tenant_id, principal.user_id, agent_id)
    await _seed_history(uow, tenant_id, sid, with_task=False)
    async with factory() as db:
        boundary_created_at = (
            await db.execute(select(MessageORM.created_at).where(MessageORM.session_id == sid, MessageORM.seq == 1))
        ).scalar_one()
        before_search = (await db.get(SessionORM, sid)).search_text
    assert "110kV 城东线再试送电" in before_search  # 前置：被删正文原在检索面
    # Act：以 seq=2（第二条用户消息）为锚回退
    resp = await rewind_session(sid, SessionRewindIn(before_seq=2), principal=principal, uow=uow, request=None)
    # Assert：202 {data:{before_seq,deleted_count}}；seq 2/3 软删
    assert resp == {"data": {"before_seq": 2, "deleted_count": 2}, "meta": {}}
    page = await list_messages(sid, principal=principal, uow=uow)
    assert [m.seq for m in page.items] == [1, 0]  # 软删行不复活
    async with factory() as db:
        rows = (
            (await db.execute(select(MessageORM).where(MessageORM.session_id == sid).order_by(MessageORM.seq)))
            .scalars()
            .all()
        )
        row = await db.get(SessionORM, sid)
    assert len(rows) == 4  # 物理保留（软删非硬删）
    assert [r.seq for r in rows if r.deleted_at is None] == [0, 1]
    assert all(r.deleted_at is not None for r in rows if r.seq >= 2)
    # last_message_at 回退到边界前最后一条未删消息（seq=1 的 created_at），非回退时刻
    assert row.last_message_at == boundary_created_at
    # 检索面同步退出被删正文
    assert "110kV 城东线再试送电" not in row.search_text
    assert "雷击" in row.search_text  # 未删正文保留
    # Act②：重复同锚（幂等）——零新增软删，历史不再变化
    resp2 = await rewind_session(sid, SessionRewindIn(before_seq=2), principal=principal, uow=uow, request=None)
    # Assert②
    assert resp2["data"]["deleted_count"] == 0
    page2 = await list_messages(sid, principal=principal, uow=uow)
    assert [m.seq for m in page2.items] == [1, 0]


async def test_rewind_L1三键清空_fakeredis_无实例跳过(face) -> None:
    """详设 §2.3：rewind 后 L1 blocks/window/state 三键清空；无 L1 实例跳过并日志不阻断。"""
    # Arrange：历史 + fakeredis 承载的 L1 三键
    uow, _factory, principal, agent_id, tenant_id = face
    sid = await _new_session(uow, tenant_id, principal.user_id, agent_id)
    await _seed_history(uow, tenant_id, sid, with_task=False)
    fake = fakeredis.aioredis.FakeRedis()
    store: RedisL1Store = build_l1_store(fake, ttl_seconds=3600)
    await store.write_blocks(tenant_id, sid, [MemoryBlock(key="persona", content="电力分析助手")])
    await store.append_window(tenant_id, sid, [WindowMessage(role="user", content="分析昨夜城东线路的停电原因")])
    await store.write_state(tenant_id, sid, {"stage": "active"})
    sanity = await store.read(tenant_id, sid)
    assert sanity.blocks and sanity.window and sanity.state  # 前置：三键确有数据
    # Act：携 L1 实例回退
    await rewind_session(sid, SessionRewindIn(before_seq=2), principal=principal, uow=uow, request=None, l1_store=store)
    # Assert：三键全清（delete_all 口径），实例未误降级
    snapshot = await store.read(tenant_id, sid)
    assert snapshot.blocks == {} and snapshot.window == [] and snapshot.state is None
    assert snapshot.degraded is False
    # Act②：无 L1 实例（request=None 且未注入）→ 跳过失效并日志，回退主流程不受影响
    sid2 = await _new_session(uow, tenant_id, principal.user_id, agent_id)
    await _seed_history(uow, tenant_id, sid2, with_task=False)
    resp = await rewind_session(sid2, SessionRewindIn(before_seq=2), principal=principal, uow=uow, request=None)
    assert resp["data"]["deleted_count"] == 2
    await fake.aclose()


async def test_rewind_closed会话4101拒(face) -> None:
    """详设 §2.3：closed 会话回退 4101 拒且零副作用（消息不软删）。"""
    # Arrange：历史会话 → close（状态迁移经聚合方法）
    uow, factory, principal, agent_id, tenant_id = face
    sid = await _new_session(uow, tenant_id, principal.user_id, agent_id)
    await _seed_history(uow, tenant_id, sid, with_task=False)
    closed = await close_session(sid, principal=principal, uow=uow)
    assert closed.status.value == "closed"
    # Act / Assert：closed 回退 4101（HTTP 409，02 §7 4xxx 业务规则口径）
    with pytest.raises(GatewayError) as ei:
        await rewind_session(sid, SessionRewindIn(before_seq=2), principal=principal, uow=uow, request=None)
    assert (ei.value.code, ei.value.status_code) == (4101, 409)
    assert "SESSION_CLOSED" in ei.value.message
    async with factory() as db:
        deleted_flags = (
            (await db.execute(select(MessageORM.deleted_at).where(MessageORM.session_id == sid))).scalars().all()
        )
    assert all(d is None for d in deleted_flags)  # 拒绝路径零副作用（UoW 回滚）


async def test_rewind_无任务载体_审计跳过不阻断回退(face) -> None:
    """详设 §2.3：会话无任务载体（直追消息未建任务）→ 审计告警跳过，回退照常 202。"""
    # Arrange：历史会话，不建任务（with_task=False）
    uow, _factory, principal, agent_id, tenant_id = face
    sid = await _new_session(uow, tenant_id, principal.user_id, agent_id)
    await _seed_history(uow, tenant_id, sid, with_task=False)
    # Act / Assert：不抛即通过（审计 warning 留痕，主流程完成）
    resp = await rewind_session(sid, SessionRewindIn(before_seq=2), principal=principal, uow=uow, request=None)
    assert resp["data"]["deleted_count"] == 2


async def test_rewind_非法锚4106_不存在seq与非用户轮(face) -> None:
    """详设 §2.3：仅用户消息 seq 可作锚——不存在 / assistant 轮均 4106（HTTP 409），零软删。"""
    # Arrange：四轮历史
    uow, factory, principal, agent_id, tenant_id = face
    sid = await _new_session(uow, tenant_id, principal.user_id, agent_id)
    await _seed_history(uow, tenant_id, sid, with_task=False)
    # Act / Assert：seq 越界不存在 → 4106
    with pytest.raises(GatewayError) as ei_missing:
        await rewind_session(sid, SessionRewindIn(before_seq=99), principal=principal, uow=uow, request=None)
    assert (ei_missing.value.code, ei_missing.value.status_code) == (4106, 409)
    assert "SESSION_REWIND_INVALID" in ei_missing.value.message
    # Act / Assert：锚落在 assistant 轮（seq=1/3）→ 4106
    for anchor in (1, 3):
        with pytest.raises(GatewayError) as ei_role:
            await rewind_session(sid, SessionRewindIn(before_seq=anchor), principal=principal, uow=uow, request=None)
        assert ei_role.value.code == 4106
    async with factory() as db:
        deleted_flags = (
            (await db.execute(select(MessageORM.deleted_at).where(MessageORM.session_id == sid))).scalars().all()
        )
    assert all(d is None for d in deleted_flags)  # 非法锚零副作用


async def test_rewind_审计行session_rewound落task_events(face) -> None:
    """详设 §2.3：审计事件 session.rewound 复用 task_events 追加路径，payload 含
    before_seq/deleted_count/session_id；幂等重放同锚亦留审计痕（如实记录 deleted_count=0）。"""
    # Arrange：历史 + 任务载体
    uow, factory, principal, agent_id, tenant_id = face
    sid = await _new_session(uow, tenant_id, principal.user_id, agent_id)
    task = await _seed_history(uow, tenant_id, sid, with_task=True)
    # Act
    await rewind_session(sid, SessionRewindIn(before_seq=2), principal=principal, uow=uow, request=None)
    # Assert：事件行落库且 payload 齐全
    async with factory() as db:
        events = (
            (
                await db.execute(
                    select(TaskEventORM)
                    .where(TaskEventORM.task_id == task.id, TaskEventORM.event_type == "session.rewound")
                    .order_by(TaskEventORM.seq)
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].data == {"session_id": str(sid), "before_seq": 2, "deleted_count": 2}
    # Act②：幂等重放
    await rewind_session(sid, SessionRewindIn(before_seq=2), principal=principal, uow=uow, request=None)
    # Assert②：第二次审计行如实记录零新增软删
    async with factory() as db:
        events2 = (
            (
                await db.execute(
                    select(TaskEventORM)
                    .where(TaskEventORM.task_id == task.id, TaskEventORM.event_type == "session.rewound")
                    .order_by(TaskEventORM.seq)
                )
            )
            .scalars()
            .all()
        )
    assert [e.data["deleted_count"] for e in events2] == [2, 0]

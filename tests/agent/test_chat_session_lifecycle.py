# tests/agent/test_chat_session_lifecycle.py
"""chat 会话生命周期全链集成用例（docs/Agent/18 §3 用例①：建会话→3 轮消息→rewind→close）。

断言目标（真 PG 一次性测试库；本地 PG 不可达自动跳过）：
- 事件序：每轮 RUN_STARTED 领头、RETRIEVAL_EVIDENCE 在列、RUN_FINISHED 收尾；
- L1 窗状态：3 轮后窗内 user/assistant 交替共 6 条、无重复入窗（修复 1 幂等守卫在
  真实生命周期下的组合观察）、最新 user 消息在窗顶（LPUSH 新→旧）；
- messages 表 seq 单调：受理面 user + 结果汇 assistant 共 6 行，seq 0..5 严格递增、
  role 交替；
- rewind：软删边界后仅边界前消息可见（幂等重放语义按端点契约）；
- close：关闭后新消息 4101 拒绝（状态机口径）。

编排器链路与生产同构：send_message 202 受理 →（模拟 worker 认领）编排器 stream_chat
→ build_chat_result_sink 落库（FakeModel/Fake 检索/Fake L1，零 LLM/Redis 依赖）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.agent.api.schemas.session import SendMessageIn, SessionCreateIn, SessionRewindIn
from services.agent.api.sessions import (
    build_chat_result_sink,
    close_session,
    create_session,
    list_messages,
    rewind_session,
    send_message,
)
from services.agent.business.adapters.builtin import BuiltinAdapter
from services.agent.business.chat_context import ChatContextAssembler
from services.agent.business.chat_events import ChatCommand, ChatEventName
from services.agent.business.chat_orchestrator import ChatOrchestrator
from services.kb.business.search_service import KnowledgeSearchResult
from services.memory.domain.model.l1 import L1Snapshot, MemoryBlock, WindowMessage
from services.platform.config import Settings
from services.platform.db import registry as _orm_registry  # noqa: F401  全表聚合注册（create_all 需跨模块 FK 解析）
from services.platform.db.base import Base
from services.platform.db.uow import AsyncUnitOfWork
from services.platform.deps import Principal
from services.platform.errors import GatewayError
from tests.agent.pg_testdb import create_test_database, drop_test_database, probe_pg

if sys.platform == "win32":  # psycopg 异步要求 Selector 循环（导入期固定策略，test_session_user_face 同款）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pytestmark = pytest.mark.integration

ROUNDS = ("第一轮：城东线停电原因？", "第二轮：影响哪些馈线？", "第三轮：抢修建议？")


# ── Fakes（编排器侧桩；PG 面全真）─────────────────────────────────────────────


class FakeL1Store:
    """L1 存储桩：窗口追加可观测（生命周期 L1 断言口），三键语义最小实现。"""

    def __init__(self) -> None:
        self.window: list[WindowMessage] = []
        self.cleared = 0

    async def read(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> L1Snapshot:
        return L1Snapshot(tenant_id=tenant_id, session_id=session_id, window=list(self.window))

    async def write_blocks(self, tenant_id: uuid.UUID, session_id: uuid.UUID, blocks: list[MemoryBlock]) -> int:
        return 0

    async def append_window(self, tenant_id: uuid.UUID, session_id: uuid.UUID, messages: list[WindowMessage]) -> int:
        for message in messages:
            self.window.insert(0, message)
        return len(self.window)

    async def write_state(self, tenant_id: uuid.UUID, session_id: uuid.UUID, state: dict) -> None:
        pass

    async def delete_all(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
        self.cleared += 1
        self.window.clear()


class FakeKnowledge:
    """检索服务桩：恒空引用（组装面走通，非本用例被测对象）。"""

    async def search(self, **kwargs: Any) -> KnowledgeSearchResult:
        return KnowledgeSearchResult(query=str(kwargs.get("query", "")), degraded=False, citations=[])


class FakeL2Repo:
    """L2 仓储桩：双通道恒空（生命周期用例不测记忆融合面）。"""

    async def search_candidates(self, user_id: uuid.UUID, query: str, *, limit: int) -> list[Any]:
        return []

    async def recent_candidates(self, user_id: uuid.UUID, *, limit: int) -> list[Any]:
        return []


class FakeChatModel:
    """ModelPort 桩：调用计数产逐轮唯一答案（history 含同文，禁按 prompt 内容反推轮次）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {"answer": f"答复（第 {self.calls} 轮）：依据证据给出确定性结论。"}


# ── 夹具与种子（test_session_user_face 同款一次性测试库形态）──────────────────


@pytest.fixture
async def pg_env() -> AsyncIterator[tuple[AsyncUnitOfWork, async_sessionmaker[AsyncSession]]]:
    settings = Settings()
    if not await probe_pg(settings.pg_dsn):
        pytest.skip("本地 PG 不可达，跳过 chat 生命周期集成用例")
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
    uow, factory = pg_env
    from services.agent.data.orm import Agent as AgentORM
    from services.agent.data.orm import AgentAdapter as AgentAdapterORM
    from services.iam.data.orm import Tenant as TenantORM
    from services.iam.data.orm import User as UserORM

    async with factory() as db, db.begin():
        tenant = TenantORM(
            name=f"lc-租户-{uuid.uuid4().hex[:8]}", slug=f"lc-{uuid.uuid4().hex[:12]}", plan="free", status="active"
        )
        db.add(tenant)
        await db.flush()
        user = UserORM(tenant_id=tenant.id, email=f"{uuid.uuid4().hex[:10]}@it.local", password_hash="it-only")
        adapter = AgentAdapterORM(agent_tool="nanobot", version=f"lc-{uuid.uuid4().hex[:8]}")
        db.add_all([user, adapter])
        await db.flush()
        agent = AgentORM(
            tenant_id=tenant.id,
            name=f"lc-agent-{uuid.uuid4().hex[:8]}",
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


def _orchestrator(uow: AsyncUnitOfWork, l1: FakeL1Store) -> ChatOrchestrator:
    """生产同构编排器：真结果汇（PG 落库）+ Fake 生成/检索/L1。"""

    @asynccontextmanager
    async def fake_session_factory():
        yield None

    assembler = ChatContextAssembler(
        l1_store=l1,  # type: ignore[arg-type]
        session_factory=fake_session_factory,  # type: ignore[arg-type]
        knowledge=FakeKnowledge(),  # type: ignore[arg-type]
        repo_factory=lambda db, tenant: FakeL2Repo(),  # type: ignore[arg-type,return-value]
    )
    return ChatOrchestrator(
        adapters={"builtin": BuiltinAdapter(FakeChatModel())},
        assembler=assembler,
        result_sink=build_chat_result_sink(uow),
    )


# ── 用例①：生命周期全链 ───────────────────────────────────────────────────────


async def test_建会话到三轮消息到rewind到close_全链状态与事件序(face) -> None:
    # Arrange：建会话 + 生产同构编排器 + Fake L1
    uow, factory, principal, agent_id, tenant_id = face
    l1 = FakeL1Store()
    orchestrator = _orchestrator(uow, l1)
    created = await create_session(SessionCreateIn(agent_id=agent_id, title="生命周期用例"), principal, uow)
    session_id = created.id

    # Act：3 轮消息——202 受理（user 落库+任务受理）→ 模拟 worker 认领跑编排器（结果汇落 assistant）
    for round_no, content in enumerate(ROUNDS, start=1):
        accepted = await send_message(session_id, SendMessageIn(content=content), principal, uow, request=None)
        payload = accepted["data"]
        command = ChatCommand(
            tenant_id=tenant_id,
            user_id=principal.user_id,
            session_id=session_id,
            task_id=uuid.UUID(payload["task_id"]),
            run_id=uuid.UUID(payload["run_id"]),
            agent_id=agent_id,
            message=content,
            trace_id=f"trace-lifecycle-{round_no}",
            adapter="builtin",
        )
        events = [event async for event in orchestrator.stream_chat(command)]

        # Assert 事件序：RUN_STARTED 领头、RETRIEVAL_EVIDENCE 在列、RUN_FINISHED 收尾
        assert events[0].name is ChatEventName.RUN_STARTED
        assert any(e.name is ChatEventName.RETRIEVAL_EVIDENCE for e in events)
        assert events[-1].name is ChatEventName.RUN_FINISHED

    # Assert L1 窗状态：6 条（3 user + 3 assistant）、新→旧（LPUSH：每轮 user 先入、
    # assistant 后入 → 窗序 assistant/user 交替且窗顶=末轮 assistant）、无重复入窗
    assert [m.role for m in l1.window] == ["assistant", "user"] * 3
    assert l1.window[1].content == ROUNDS[2]  # 最新 user 消息紧随窗顶
    assert len({m.content for m in l1.window}) == 6  # 无重复入窗（幂等守卫组合观察）
    assert len({m.metadata.get("seed_task_id") for m in l1.window if m.role == "user"}) == 3

    # Assert messages 表 seq 单调：6 行 0..5 严格递增、role 交替（list_messages 契约=seq 降序，
    # 前端 seed F1 同口径——断言前升序化）
    async with uow.for_tenant(tenant_id) as tx:
        page = await list_messages(session_id, principal, uow, limit=100)
        rows = sorted(page.items, key=lambda m: m.seq)
        seqs = [m.seq for m in rows]
        assert seqs == list(range(6))
        assert [m.role for m in rows] == ["user", "assistant"] * 3
        # 事件留痕：每任务有 task.created（受理）与 run.finished（结果汇）
        carrier = await tx.tasks.list(session_id=session_id, limit=10)
        assert len(carrier) == 3
        for task in carrier:
            types = {e.event_type for e in await tx.tasks.list_events(task.id)}
            assert "task.created" in types and "run.finished" in types

    # Act+Assert rewind：锚 seq=4（第三轮 user）→ 软删 2 行，边界前 4 行可见
    rewound = await rewind_session(session_id, SessionRewindIn(before_seq=4), principal, uow, l1_store=l1)
    assert rewound["data"] == {"before_seq": 4, "deleted_count": 2}
    after = await list_messages(session_id, principal, uow, limit=100)
    assert sorted(m.seq for m in after.items) == [0, 1, 2, 3]
    assert l1.cleared == 1  # rewind L1 失效已触发（Fake delete_all 计数）

    # Act+Assert close：关闭后新消息 4101 拒（状态机唯一入口语义）
    await close_session(session_id, principal, uow)
    with pytest.raises(GatewayError) as err:
        await send_message(session_id, SendMessageIn(content="关闭后再发"), principal, uow, request=None)
    assert "4101" in str(err.value)

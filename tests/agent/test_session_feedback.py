# tests/agent/test_session_feedback.py
"""会话反馈端点集成用例（docs/Agent/19 §5 数据飞轮·采集环，W9+B5 批）。

断言目标（19 §5 采集环验收口径，中文 AAA）：
- POST 幂等：同 (session, run, user) 重复反馈=更新（outcome/tags/correction_text 覆盖），
  行数恒 1 不重复（uk_session_feedback_session_run_user），created_at 保持首次时刻；
- 归属校验 404 矩阵：他人会话（A2 收口）/会话不存在，同形 404「会话不存在」；
- run 归属断言：run 属于其他会话 → 404「run 不属于该会话」（cancel 端点同款口径）；
- 枚举校验 422：outcome 非法值构造请求体即被 Pydantic 拒（fail-closed 契约常量）；
- 审计行：session.feedback 落 task_events（就近任务载体；纠错原文不进审计 data）；
- GET：本人反馈历史 {data,meta}；他人会话 GET 同形 404。

用一次性 PG 测试库承载（禁对共享库 alembic upgrade——schema 由 Base.metadata.create_all
建，tests/agent/pg_testdb.py 说明）；本地 PG 不可达即跳过。
端点均为直调（Depends 参数显式传参等价，tests/gateway/test_sessions.py 同口径）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.agent.api.schemas.session import SessionFeedbackIn
from services.agent.api.sessions import list_session_feedback, submit_session_feedback
from services.agent.data.orm import Agent as AgentORM
from services.agent.data.orm import AgentAdapter as AgentAdapterORM
from services.agent.data.orm import SessionFeedback as SessionFeedbackORM
from services.agent.data.orm import TaskEvent as TaskEventORM
from services.agent.domain.model.session import FeedbackOutcome, Session
from services.agent.domain.model.task import Task
from services.gateway.middlewares import GatewayError
from services.iam.data.orm import Tenant as TenantORM
from services.iam.data.orm import User as UserORM
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
        pytest.skip("本地 PG 不可达，跳过会话反馈集成用例")
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
async def fb(
    pg_env: tuple[AsyncUnitOfWork, async_sessionmaker[AsyncSession]],
) -> AsyncIterator[tuple[AsyncUnitOfWork, async_sessionmaker[AsyncSession], Principal, Principal, uuid.UUID]]:
    """租户 + 甲乙两用户 + 适配器/agent → (uow, factory, 主体甲, 主体乙, agent_id)。

    乙主体用于归属 404 矩阵（同租户他用户访问甲会话——A2 口径防存在性探测）。"""
    uow, factory = pg_env
    async with factory() as db, db.begin():
        tenant = TenantORM(
            name=f"fb-租户-{uuid.uuid4().hex[:8]}", slug=f"fb-{uuid.uuid4().hex[:12]}", plan="free", status="active"
        )
        db.add(tenant)
        await db.flush()  # uuid7 PK 在 flush 时分配，依赖行需引用真实 id
        user_a = UserORM(tenant_id=tenant.id, email=f"{uuid.uuid4().hex[:10]}@it.local", password_hash="it-only")
        user_b = UserORM(tenant_id=tenant.id, email=f"{uuid.uuid4().hex[:10]}@it.local", password_hash="it-only")
        adapter = AgentAdapterORM(agent_tool="nanobot", version=f"fb-{uuid.uuid4().hex[:8]}")
        db.add_all([user_a, user_b, adapter])
        await db.flush()
        agent = AgentORM(
            tenant_id=tenant.id,
            name=f"fb-agent-{uuid.uuid4().hex[:8]}",
            agent_tool=adapter.agent_tool,
            adapter_id=adapter.id,
        )
        db.add(agent)
        await db.flush()

        def _principal(user: UserORM) -> Principal:
            return Principal(
                {
                    "sub": str(user.id),
                    "tenant_id": str(tenant.id),
                    "roles": ["member"],
                    "scopes": ["session:read", "session:write", "session:chat"],
                    "typ": "access",
                    "jti": uuid.uuid4().hex,
                }
            )

        p_a, p_b, agent_id = _principal(user_a), _principal(user_b), agent.id
    yield uow, factory, p_a, p_b, agent_id


async def _new_session(
    uow: AsyncUnitOfWork, tenant_id: uuid.UUID, user_id: uuid.UUID, agent_id: uuid.UUID
) -> uuid.UUID:
    """建会话（归属=传入 user）。"""
    session = Session(id=uuid.uuid4(), tenant_id=tenant_id, agent_id=agent_id, user_id=user_id, title=None)
    async with uow.for_tenant(tenant_id) as tx:
        await tx.sessions.add(session)
    return session.id


async def _seed_task_with_run(
    uow: AsyncUnitOfWork, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """种子任务 + 根 Run（chat 受理形态：task.start_run 聚合方法 + 聚合 save 落行）。"""
    task = Task(tenant_id=tenant_id, type="chat", session_id=session_id)
    run = task.start_run()
    async with uow.for_tenant(tenant_id) as tx:
        await tx.tasks.save(task)
    return task.id, run.id


# ---------------------------------------------------------------- POST 幂等


async def test_POST幂等_重复反馈更新不重复行(fb) -> None:
    """同 (session, run, user) 两次不同 outcome → 202、行数恒 1、outcome/纠错文本被覆盖。"""
    # Arrange：会话 + 任务/run 载体
    uow, factory, principal, _p_b, agent_id = fb
    sid = await _new_session(uow, principal.tenant_id, principal.user_id, agent_id)
    _task_id, run_id = await _seed_task_with_run(uow, principal.tenant_id, sid)
    # Act：第一次反馈（completed + 纠错文本）
    first = await submit_session_feedback(
        sid,
        SessionFeedbackIn(run_id=run_id, outcome="completed", correction_text="基本可用"),
        principal=principal,
        uow=uow,
    )
    # 第二次反馈（改 partial + 换纠错文本，同 run 同用户）
    second = await submit_session_feedback(
        sid,
        SessionFeedbackIn(run_id=run_id, outcome="partial", tags=["答案不完整"], correction_text="缺了检修建议"),
        principal=principal,
        uow=uow,
    )
    # Assert：两次均 202 语义（响应 data 回显最新值），表内仅 1 行且字段为最新
    assert first["data"]["outcome"] == "completed"
    assert second["data"]["outcome"] == "partial"
    assert second["data"]["correction_text"] == "缺了检修建议"
    assert second["data"]["tags"] == ["答案不完整"]
    async with factory() as db:
        rows = (await db.execute(select(SessionFeedbackORM))).scalars().all()
    assert len(rows) == 1  # 幂等：更新非新增（uk_session_feedback_session_run_user）
    assert rows[0].outcome == "partial"
    # created_at 保持首次反馈时刻（幂等更新不改动；响应侧为 ISO 字符串，解析后比对）
    first_at = datetime.fromisoformat(str(first["data"]["created_at"]).replace("Z", "+00:00"))
    second_at = datetime.fromisoformat(str(second["data"]["created_at"]).replace("Z", "+00:00"))
    assert rows[0].created_at == first_at == second_at


# ---------------------------------------------------------------- 归属校验 404 矩阵


async def test_归属校验404矩阵_他人会话与会话不存在同形(fb) -> None:
    """同租户他用户访问甲会话 → 404「会话不存在」；不存在会话 → 同形 404（防存在性探测）。"""
    # Arrange：甲会话 + run 载体；乙主体（同租户非归属）
    uow, _factory, principal, other, agent_id = fb
    sid = await _new_session(uow, principal.tenant_id, principal.user_id, agent_id)
    _task_id, run_id = await _seed_task_with_run(uow, principal.tenant_id, sid)
    body = SessionFeedbackIn(run_id=run_id, outcome="completed")
    # Act / Assert：他人会话 → 404 同形「会话不存在」
    with pytest.raises(GatewayError) as ei:
        await submit_session_feedback(sid, body, principal=other, uow=uow)
    assert (ei.value.code, ei.value.status_code) == (404, 404)
    assert "会话不存在" in ei.value.message
    # 不存在会话 → 同形 404
    with pytest.raises(GatewayError) as ei2:
        await submit_session_feedback(uuid.uuid4(), body, principal=principal, uow=uow)
    assert (ei2.value.code, ei2.value.status_code) == (404, 404)
    # GET 同矩阵：他人会话/不存在会话同形 404
    with pytest.raises(GatewayError) as ei3:
        await list_session_feedback(sid, principal=other, uow=uow)
    assert (ei3.value.code, ei3.value.status_code) == (404, 404)
    with pytest.raises(GatewayError) as ei4:
        await list_session_feedback(uuid.uuid4(), principal=principal, uow=uow)
    assert (ei4.value.code, ei4.value.status_code) == (404, 404)


async def test_run归属断言_run属于其他会话_404(fb) -> None:
    """run 属于会话 B，反馈打到会话 A → 404「run 不属于该会话」，且零落行。"""
    # Arrange：甲的两个会话；run 挂在会话 B
    uow, factory, principal, _p_b, agent_id = fb
    sid_a = await _new_session(uow, principal.tenant_id, principal.user_id, agent_id)
    sid_b = await _new_session(uow, principal.tenant_id, principal.user_id, agent_id)
    _task_b, run_b = await _seed_task_with_run(uow, principal.tenant_id, sid_b)
    # Act / Assert：对会话 A 反馈会话 B 的 run → 404
    with pytest.raises(GatewayError) as ei:
        await submit_session_feedback(
            sid_a, SessionFeedbackIn(run_id=run_b, outcome="completed"), principal=principal, uow=uow
        )
    assert (ei.value.code, ei.value.status_code) == (404, 404)
    assert "run 不属于该会话" in ei.value.message
    # 不存在的 run 同口径 404
    with pytest.raises(GatewayError) as ei2:
        await submit_session_feedback(
            sid_a, SessionFeedbackIn(run_id=uuid.uuid4(), outcome="completed"), principal=principal, uow=uow
        )
    assert (ei2.value.code, ei2.value.status_code) == (404, 404)
    async with factory() as db:
        assert (await db.execute(select(SessionFeedbackORM))).scalars().all() == []  # 断言拒绝零副作用


# ---------------------------------------------------------------- 枚举校验 422


async def test_枚举校验_非法outcome构造即422(fb) -> None:
    """outcome 非三元值 → 请求体构造即被 Pydantic 拒（fail-closed；直调形态=ValidationError）。"""
    # Arrange / Act / Assert：非法枚举在 schema 校验层拦截（HTTP 形态即 422 3001）
    with pytest.raises(ValidationError):
        SessionFeedbackIn(run_id=uuid.uuid4(), outcome="good")
    with pytest.raises(ValidationError):
        SessionFeedbackIn(run_id=uuid.uuid4(), outcome="completed", correction_text="x" * 121)  # >120 字拒


# ---------------------------------------------------------------- 审计行 + GET


async def test_审计行落库_且纠错原文不进审计data(fb) -> None:
    """POST 后 task_events 出现 session.feedback 行（就近任务载体）；data 只留 outcome/has_correction。"""
    # Arrange：会话 + run 载体
    uow, factory, principal, _p_b, agent_id = fb
    sid = await _new_session(uow, principal.tenant_id, principal.user_id, agent_id)
    task_id, run_id = await _seed_task_with_run(uow, principal.tenant_id, sid)
    # Act：带纠错文本反馈
    await submit_session_feedback(
        sid,
        SessionFeedbackIn(run_id=run_id, outcome="failed", correction_text="结论方向错了"),
        principal=principal,
        uow=uow,
    )
    # Assert：审计行落库且 data 最小面（原文不进审计）
    async with factory() as db:
        events = (
            (
                await db.execute(
                    select(TaskEventORM).where(
                        TaskEventORM.task_id == task_id, TaskEventORM.event_type == "session.feedback"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    data = events[0].data
    assert data["outcome"] == "failed"
    assert data["has_correction"] is True
    assert data["run_id"] == str(run_id)
    assert "结论方向错了" not in str(data)  # 纠错原文不进审计行（用户内容最小留痕）


async def test_GET本人反馈历史_信封与他用户行隔离(fb) -> None:
    """GET 只回本人在本会话的反馈（{data,meta}）；乙用户在群场景写行互不可见。"""
    # Arrange：甲会话 + run；甲反馈 run1；乙（临时借用同会话写权限面）反馈同 run——
    # 同 run 不同 user 各一行（uk 三键含 user_id），GET 甲只见甲行
    uow, factory, principal, other, agent_id = fb
    sid = await _new_session(uow, principal.tenant_id, principal.user_id, agent_id)
    _task_id, run_id = await _seed_task_with_run(uow, principal.tenant_id, sid)
    await submit_session_feedback(
        sid, SessionFeedbackIn(run_id=run_id, outcome="completed"), principal=principal, uow=uow
    )
    # 直插乙行（乙无甲会话归属，端点面不可达——此处验证仓储行级 user 隔离口径）
    async with uow.for_tenant(principal.tenant_id) as tx:
        await tx.sessions.record_feedback(
            sid, run_id, other.user_id, outcome=FeedbackOutcome.PARTIAL, tags=[], correction_text=None
        )
    # Act / Assert：甲 GET → 仅甲行；meta 空信封
    result = await list_session_feedback(sid, principal=principal, uow=uow)
    assert [f.outcome for f in result.data] == ["completed"]
    assert result.data[0].run_id == run_id
    assert result.data[0].user_id == principal.user_id
    assert result.meta == {}

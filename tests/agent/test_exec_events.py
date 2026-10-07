# tests/agent/test_exec_events.py
"""执行结构事件管道用例（40 篇 §4「执行结构波」/§8 R2 管道步；2026-10-04）。

断言目标（R2 管道验收口径）：
- 枚举登记：六事件入 ChatEventName、wire 名 ≤32 字符（task_events.event_type 约束）；
- 转译映射：内核锚点 → ChatEvent 载荷逐字段对照 40 篇 §4.2 schema（含 session_id/trace_id 注入）；
- observer 接线：经 dispatcher.register_hook(ON_KERNEL_EVENT) + broadcast_kernel_event 真实广播路径；
- 落库纪律：SUBRUN_UPDATED 纯实时不落库（40 篇 §4.1），双写钩子对回放根事件落 task_events
  （event_type=事件名、trace_id 携带、R11 replay_root 串行化路径）。

集成用例用一次性 PG 测试库承载（禁对共享库 alembic upgrade——schema 由
Base.metadata.create_all 建，tests/agent/pg_testdb.py 说明）；本地 PG 不可达即跳过。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.agent.api.sessions import build_exec_event_dual_write
from services.agent.business.chat_events import ChatCommand, ChatEvent, ChatEventName, wire_data
from services.agent.business.exec_events import (
    EXEC_PERSISTED_EVENTS,
    EXEC_REALTIME_ONLY_EVENTS,
    EXEC_STRUCTURE_EVENTS,
    KERNEL_APPROVAL_PENDING,
    KERNEL_PLAN_UPDATED,
    KERNEL_SUBRUN_FINISHED,
    KERNEL_SUBRUN_STARTED,
    KERNEL_SUBRUN_UPDATED,
    ApprovalRequiredPayload,
    ExecEventTranslator,
    SubRunFinishedPayload,
    SubRunStartedPayload,
    SubRunUpdatedPayload,
    WorkflowNodeFinishedPayload,
    WorkflowNodeStartedPayload,
)
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.hooks import HookName
from services.agent.data.orm import TaskEvent as TaskEventORM
from services.agent.domain.model.kernel_context import KernelEvent
from services.agent.domain.model.task import Task, TaskEvent
from services.iam.data.orm import Tenant as TenantORM
from services.platform.config import Settings
from services.platform.db import registry as _orm_registry  # noqa: F401  全表聚合注册（create_all 需跨模块 FK 解析）
from services.platform.db.base import Base
from services.platform.db.uow import AsyncUnitOfWork
from tests.agent.pg_testdb import create_test_database, drop_test_database, probe_pg

if sys.platform == "win32":  # psycopg 异步要求 Selector 循环（导入期固定策略）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

TENANT = uuid.uuid4()
TASK_ID, SESSION_ID, RUN_ID, SUB_RUN_ID, PARENT_RUN_ID = (uuid.uuid4() for _ in range(5))
TRACE = "trace-exec-events"
_APPROVAL_HASH = "c" * 64  # 审批锚点 param_hash（B5 参数哈希绑定）


# ── 枚举登记（chat_events.py 单一事实源）──────────────────────────────────


def test_执行结构波六事件已登记且wire名合法() -> None:
    """40 篇 §4.1 六事件登记为 ChatEventName；wire 名 ≤32 字符（task_events.event_type 约束已核）。"""
    expected = {
        "PLAN_UPDATED",
        "SUBRUN_STARTED",
        "SUBRUN_UPDATED",
        "SUBRUN_FINISHED",
        "WORKFLOW_NODE_STARTED",
        "WORKFLOW_NODE_FINISHED",
    }
    assert {n.value for n in ChatEventName} >= expected
    for name in EXEC_STRUCTURE_EVENTS:
        assert len(name.value) <= 32, name.value


def test_落库集合_SUBRUN_UPDATED为纯实时不落库() -> None:
    """40 篇 §4.1：执行结构事件中仅 SUBRUN_UPDATED 不落 task_events（纯实时心跳）。"""
    assert EXEC_REALTIME_ONLY_EVENTS == frozenset({ChatEventName.SUBRUN_UPDATED})
    assert ChatEventName.SUBRUN_UPDATED not in EXEC_PERSISTED_EVENTS
    assert EXEC_PERSISTED_EVENTS | EXEC_REALTIME_ONLY_EVENTS == EXEC_STRUCTURE_EVENTS


# ── 转译映射（逐字段对照 40 篇 §4.2）──────────────────────────────────────


def _translator(on_event=None) -> ExecEventTranslator:
    return ExecEventTranslator(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        trace_id=TRACE,
        on_event=on_event or (lambda _e: None),
    )


def _kernel_event(event_type: str, data: dict) -> KernelEvent:
    return KernelEvent(
        event_type=event_type,
        tenant_id=TENANT,
        run_id=RUN_ID,
        trace_id=TRACE,
        data=data,
    )


def test_转译_SUBRUN_STARTED_载荷逐字段对照40篇_4_2() -> None:
    """kernel.subrun_started → SUBRUN_STARTED：§4.2 全字段映射（session_id/task_id 由上下文注入）。"""
    event = _kernel_event(
        KERNEL_SUBRUN_STARTED,
        {
            "sub_run_id": SUB_RUN_ID,
            "parent_run_id": PARENT_RUN_ID,
            "label": "数据抽取员",
            "goal": "从工单正文抽取停电时间与范围",
            "depth": 1,
            "index": 2,
            "total": 5,
            "context_budget": 32000,
            "started_at": "2026-10-04T10:00:00+00:00",
        },
    )
    chat = _translator().translate(event)
    assert chat is not None and chat.name is ChatEventName.SUBRUN_STARTED
    assert chat.run_id == RUN_ID and chat.trace_id == TRACE
    # §4.2 SUBRUN_STARTED schema 逐字段（含注入的 session_id/task_id/trace_id 公共字段）
    assert chat.data == {
        "sub_run_id": str(SUB_RUN_ID),
        "parent_run_id": str(PARENT_RUN_ID),
        "task_id": str(TASK_ID),
        "session_id": str(SESSION_ID),
        "label": "数据抽取员",
        "goal": "从工单正文抽取停电时间与范围",
        "depth": 1,
        "index": 2,
        "total": 5,
        "context_budget": 32000,
        "started_at": "2026-10-04T10:00:00+00:00",
        "trace_id": TRACE,
    }
    assert SubRunStartedPayload.model_validate(chat.data)  # 载荷过模型校验（形状冻结）


def test_转译_SUBRUN_UPDATED_载荷对照40篇_4_2() -> None:
    """kernel.subrun_updated → SUBRUN_UPDATED：心跳字段（该事件设计为不落库，转译仅上 wire）。"""
    chat = _translator().translate(
        _kernel_event(
            KERNEL_SUBRUN_UPDATED,
            {
                "sub_run_id": SUB_RUN_ID,
                "phase": "tool",
                "tool_name": "kb.search",
                "tool_count": 7,
                "preview": "检索到 3 条证据",
                "tokens": {"input": 120, "output": 40},
            },
        )
    )
    assert chat is not None and chat.name is ChatEventName.SUBRUN_UPDATED
    assert chat.data == {
        "sub_run_id": str(SUB_RUN_ID),
        "phase": "tool",
        "tool_name": "kb.search",
        "tool_count": 7,
        "preview": "检索到 3 条证据",
        "tokens": {"input": 120, "output": 40},
        "trace_id": TRACE,
    }
    assert SubRunUpdatedPayload.model_validate(chat.data)


def test_转译_SUBRUN_FINISHED_终态含rejected_artifact对照40篇_4_2() -> None:
    """kernel.subrun_finished → SUBRUN_FINISHED：§4.2 终态字段（status 五值枚举，宪法 2）。"""
    chat = _translator().translate(
        _kernel_event(
            KERNEL_SUBRUN_FINISHED,
            {
                "sub_run_id": SUB_RUN_ID,
                "status": "rejected_artifact",
                "duration_ms": 18230,
                "summary": "产物未过回传契约校验",
                "artifact": {"name": "停电范围", "schema_id": "art.outage", "digest": "sha256:abc"},
                "usage": {"input_tokens": 900, "output_tokens": 60},
                "error": {"code": "artifact_rejected", "message": "缺必填字段 range"},
            },
        )
    )
    assert chat is not None and chat.name is ChatEventName.SUBRUN_FINISHED
    assert chat.data["status"] == "rejected_artifact"  # 不得误报 completed（40 篇 §3.2）
    assert chat.data["duration_ms"] == 18230
    assert chat.data["artifact"]["digest"] == "sha256:abc"
    assert chat.data["usage"] == {"input_tokens": 900, "output_tokens": 60}
    assert chat.data["error"] == {"code": "artifact_rejected", "message": "缺必填字段 range"}
    assert chat.data["trace_id"] == TRACE and chat.data["sub_run_id"] == str(SUB_RUN_ID)
    assert SubRunFinishedPayload.model_validate(chat.data)


def test_转译_PLAN_UPDATED_整表快照对照40篇_4_2() -> None:
    """kernel.plan_updated → PLAN_UPDATED：revision 单调 + items 整表；plan_id 缺省=run_id。"""
    chat = _translator().translate(
        _kernel_event(
            KERNEL_PLAN_UPDATED,
            {
                "revision": 3,
                "items": [
                    {"id": "p1", "content": "检索停电工单", "status": "completed"},
                    {"id": "p2", "content": "抽取停电时间与范围", "status": "in_progress"},
                ],
            },
        )
    )
    assert chat is not None and chat.name is ChatEventName.PLAN_UPDATED
    assert chat.data == {
        "plan_id": str(RUN_ID),  # §4.2：plan_id=本次 run 内计划标识（=run_id）
        "revision": 3,
        "items": [
            {"id": "p1", "content": "检索停电工单", "status": "completed"},
            {"id": "p2", "content": "抽取停电时间与范围", "status": "in_progress"},
        ],
        "trace_id": TRACE,
    }


def test_转译_APPROVAL_REQUIRED_锚点载荷对照02协议行67() -> None:
    """kernel.approval_pending → APPROVAL_REQUIRED：02 协议审批波行 67 载荷逐字段。

    锚点载荷（execution.py 发射面）无 task_id/时间/描述类字段：task_id 由转译器上下文
    补齐；waiting_since=锚点受理时刻（occurred_at 内核未赋值→转译受理当下）；summary
    无描述类字段→省略（exclude_none，02 协议「无则省略」）。
    """
    before = datetime.now(tz=UTC)
    chat = _translator().translate(
        _kernel_event(
            KERNEL_APPROVAL_PENDING,
            {
                "step_seq": 3,
                "param_hash": _APPROVAL_HASH,
                "action_iri": "http://ontology.example/action/external_write",
                "execution_mode": "external_write",
            },
        )
    )
    after = datetime.now(tz=UTC)
    assert chat is not None and chat.name is ChatEventName.APPROVAL_REQUIRED
    assert chat.run_id == RUN_ID and chat.trace_id == TRACE
    assert chat.data == {
        "run_id": str(RUN_ID),
        "task_id": str(TASK_ID),
        "step_seq": 3,
        "action_iri": "http://ontology.example/action/external_write",
        "param_hash": _APPROVAL_HASH,
        "execution_mode": "external_write",
        "waiting_since": chat.data["waiting_since"],  # 动态时刻：形状断言+下方时刻窗断言
        "trace_id": TRACE,
    }
    assert "summary" not in chat.data  # 锚点无描述类字段→省略（02 协议「无则省略」）
    waiting_since = datetime.fromisoformat(chat.data["waiting_since"])
    assert before <= waiting_since <= after  # 锚点受理时刻（ISO8601，落在转译窗口内）
    ApprovalRequiredPayload.model_validate(chat.data)  # 载荷过模型校验（形状冻结）
    assert len(ChatEventName.APPROVAL_REQUIRED.value) <= 32  # task_events.event_type 约束


def test_转译_APPROVAL_REQUIRED_步描述截80字且occurred_at透出() -> None:
    """锚点带描述类字段→summary 截 80 字；occurred_at 已赋值→waiting_since=锚点时刻非受理时刻。"""
    occurred = datetime(2026, 10, 7, 8, 0, 0, tzinfo=UTC)
    event = KernelEvent(
        event_type=KERNEL_APPROVAL_PENDING,
        tenant_id=TENANT,
        run_id=RUN_ID,
        trace_id=TRACE,
        step_seq=1,
        occurred_at=occurred,
        data={
            "step_seq": 1,
            "param_hash": _APPROVAL_HASH,
            "action_iri": "http://ontology.example/action/external_write",
            "execution_mode": "code",
            "description": "停电" * 100,  # 200 字 → 截 80
        },
    )
    chat = _translator().translate(event)
    assert chat is not None
    assert chat.data["summary"] == "停电" * 40  # 80 字（200→80 截断）
    assert chat.data["waiting_since"] == "2026-10-07T08:00:00+00:00"  # 锚点时刻非受理时刻


def test_转译_非执行结构锚点与非法载荷一律丢弃不外泄() -> None:
    """observer 纪律：未知锚点 fast-path None；越界 status/缺必填键转译失败丢弃（事件已落账不受影响）。"""
    translator = _translator()
    assert translator.translate(_kernel_event("kernel.gated", {"step_seq": 1})) is None
    assert translator.translate(_kernel_event("kernel.settled", {})) is None
    # status 越界（非 §3.2 五值）
    bad_status = translator.translate(_kernel_event(KERNEL_SUBRUN_FINISHED, {"sub_run_id": SUB_RUN_ID, "status": "ok"}))
    assert bad_status is None
    # 缺必填键（无 sub_run_id）
    assert translator.translate(_kernel_event(KERNEL_SUBRUN_STARTED, {"parent_run_id": PARENT_RUN_ID})) is None
    # items 畸形（非对象元素）
    assert translator.translate(_kernel_event(KERNEL_PLAN_UPDATED, {"revision": 1, "items": ["p1"]})) is None


def test_转译_内核data优先_上下文只补缺() -> None:
    """发射方已知 task_id/session_id 时以其为准（只补缺不覆盖，ledger sink 同款纪律）。"""
    other_task, other_session = uuid.uuid4(), uuid.uuid4()
    chat = _translator().translate(
        _kernel_event(
            KERNEL_SUBRUN_STARTED,
            {
                "sub_run_id": SUB_RUN_ID,
                "parent_run_id": PARENT_RUN_ID,
                "context_budget": 8000,
                "task_id": other_task,
                "session_id": other_session,
            },
        )
    )
    assert chat is not None
    assert chat.data["task_id"] == str(other_task) and chat.data["session_id"] == str(other_session)
    assert chat.data["context_budget"] == 8000


def test_observer经dispatcher广播真实接线_收到转译事件() -> None:
    """H-0a 接线：register_hook(ON_KERNEL_EVENT) → broadcast_kernel_event → on_event 收到 ChatEvent。"""
    received: list[ChatEvent] = []
    dispatcher = ExtensionDispatcher()
    dispatcher.register_hook(HookName.ON_KERNEL_EVENT, _translator(on_event=received.append))
    dispatcher.hooks.broadcast_kernel_event(
        _kernel_event(
            KERNEL_SUBRUN_STARTED,
            {"sub_run_id": SUB_RUN_ID, "parent_run_id": PARENT_RUN_ID, "context_budget": 32000},
        )
    )
    assert len(received) == 1
    assert received[0].name is ChatEventName.SUBRUN_STARTED
    assert received[0].data["trace_id"] == TRACE
    # 非转译型锚点不产出（无事件入队）
    dispatcher.hooks.broadcast_kernel_event(_kernel_event("kernel.gated", {"step_seq": 1}))
    assert len(received) == 1


def test_工作流节点payload模型已登记_X16前不实现转译() -> None:
    """40 篇 §4.2 WORKFLOW_NODE_*：本步仅登记 payload 模型（枚举+形状），无发射点无转译。"""
    started = WorkflowNodeStartedPayload(
        workflow_run_id=str(RUN_ID),
        node_id="n_3",
        node_type="agent",
        title="证据检索",
        attempt=1,
        parallel_id="p1",
        parent_parallel_id=None,
        started_at="2026-10-04T10:00:00+00:00",
        trace_id=TRACE,
    )
    finished = WorkflowNodeFinishedPayload(
        workflow_run_id=str(RUN_ID),
        node_id="n_3",
        attempt=1,
        status="succeeded",
        duration_ms=4210,
        error=None,
        usage={"input_tokens": 10, "output_tokens": 5},
        trace_id=TRACE,
    )
    assert started.node_type.value == "agent" and finished.status.value == "succeeded"
    # 无节点执行器：无对应内核锚点转译（X16 落地时随发射点补）
    assert _translator().translate(_kernel_event("kernel.workflow_node_started", {})) is None


# ── ChatEvent.trace_id / wire_data（向后兼容）────────────────────────────


def test_chat_event_trace_id可选_向后兼容且wire_data只补缺() -> None:
    """R2 增补：主干事件不设 trace_id 行为不变；设了经 wire_data 进 payload（不覆盖已有键）。"""
    legacy = ChatEvent(name=ChatEventName.RUN_STARTED, data={"run_id": "r1"})
    assert legacy.trace_id is None and wire_data(legacy) == {"run_id": "r1"}
    carried = ChatEvent(name=ChatEventName.SUBRUN_FINISHED, data={"run_id": "r1"}, trace_id=TRACE)
    assert wire_data(carried) == {"run_id": "r1", "trace_id": TRACE}
    already = ChatEvent(name=ChatEventName.SUBRUN_FINISHED, data={"trace_id": "k"}, trace_id=TRACE)
    assert wire_data(already)["trace_id"] == "k"  # 只补缺不覆盖


# ── 双写钩子集成（PG；R11 replay_root 路径）──────────────────────────────


@pytest.fixture
async def pg_env() -> AsyncIterator[tuple[AsyncUnitOfWork, async_sessionmaker[AsyncSession]]]:
    """一次性测试库（每用例独立，create_all 建全量表）→ (UoW, 裸 session 工厂)。"""
    settings = Settings()
    if not await probe_pg(settings.pg_dsn):
        pytest.skip("本地 PG 不可达，跳过执行结构双写集成用例")
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
    """建租户 + chat 任务（受理即 queued Run），返回 (tenant_id, task_id)。"""
    uow, factory = env
    tenant_id = uuid.uuid4()
    async with factory() as db, db.begin():
        db.add(
            TenantORM(
                id=tenant_id,
                name=f"r2-it-租户-{uuid.uuid4().hex[:8]}",
                slug=f"r2-it-{uuid.uuid4().hex[:12]}",
                plan="free",
                status="active",
            )
        )
    task = Task(tenant_id=tenant_id, type="chat")
    task.start_run()
    async with uow.for_tenant(tenant_id) as tx:
        await tx.tasks.save(task)
        await tx.tasks.append_event(task.id, TaskEvent(task_id=task.id, event_type="task.created", data={}))
    return tenant_id, task.id


async def test_双写钩子落task_events_行名与trace_id正确(pg_env) -> None:
    """R2 验收：回放根事件经双写钩子落 task_events（event_type=事件名；trace_id 随 data 落行）。"""
    uow, factory = pg_env
    tenant_id, task_id = await _seed_task(pg_env)
    dual_write = build_exec_event_dual_write(uow, tenant_id)
    started = _translator().translate(
        KernelEvent(
            event_type=KERNEL_SUBRUN_STARTED,
            tenant_id=tenant_id,
            run_id=RUN_ID,
            trace_id=TRACE,
            data={"sub_run_id": SUB_RUN_ID, "parent_run_id": PARENT_RUN_ID, "context_budget": 32000, "depth": 1},
        )
    )
    assert started is not None
    await dual_write(task_id, started)
    async with factory() as db:
        row = (
            await db.execute(
                select(TaskEventORM).where(TaskEventORM.task_id == task_id, TaskEventORM.event_type == "SUBRUN_STARTED")
            )
        ).scalar_one()
    assert row.data["trace_id"] == TRACE  # 回放根行携带 trace（40 篇 §4.2 公共字段）
    assert row.data["sub_run_id"] == str(SUB_RUN_ID) and row.data["context_budget"] == 32000
    assert row.data["session_id"] == str(SESSION_ID)  # 转译器注入字段已随载荷落行


async def test_双写钩子_SUBRUN_UPDATED不落库_主干事件同样跳过(pg_env) -> None:
    """40 篇 §4.1：SUBRUN_UPDATED 纯实时不落库；非执行结构事件（RUN_STARTED）亦不归钩子管。"""
    uow, factory = pg_env
    tenant_id, task_id = await _seed_task(pg_env)
    dual_write = build_exec_event_dual_write(uow, tenant_id)
    async with factory() as db:
        before = len((await db.execute(select(TaskEventORM).where(TaskEventORM.task_id == task_id))).scalars().all())
    await dual_write(
        task_id,
        ChatEvent(name=ChatEventName.SUBRUN_UPDATED, data={"sub_run_id": str(SUB_RUN_ID)}, trace_id=TRACE),
    )
    await dual_write(task_id, ChatEvent(name=ChatEventName.RUN_STARTED, data={"run_id": str(RUN_ID)}))
    async with factory() as db:
        rows = (await db.execute(select(TaskEventORM).where(TaskEventORM.task_id == task_id))).scalars().all()
    assert len(rows) == before  # 零新增：心跳与主干事件均不落库
    assert all(r.event_type != "SUBRUN_UPDATED" for r in rows)


async def test_双写钩子_串行追加seq连续_R11重试路径可用(pg_env) -> None:
    """同任务串行双写两条执行结构事件：seq 连续递增（per-task 锁 + SAVEPOINT 重试路径在用）。"""
    uow, factory = pg_env
    tenant_id, task_id = await _seed_task(pg_env)
    dual_write = build_exec_event_dual_write(uow, tenant_id)
    await dual_write(
        task_id,
        ChatEvent(name=ChatEventName.SUBRUN_STARTED, data={"sub_run_id": str(SUB_RUN_ID)}, trace_id=TRACE),
    )
    await dual_write(
        task_id,
        ChatEvent(
            name=ChatEventName.SUBRUN_FINISHED,
            data={"sub_run_id": str(SUB_RUN_ID), "status": "completed"},
            trace_id=TRACE,
        ),
    )
    async with factory() as db:
        rows = (
            (
                await db.execute(
                    select(TaskEventORM)
                    .where(
                        TaskEventORM.task_id == task_id,
                        TaskEventORM.event_type.in_(["SUBRUN_STARTED", "SUBRUN_FINISHED"]),
                    )
                    .order_by(TaskEventORM.seq)
                )
            )
            .scalars()
            .all()
        )
    # seq 0 基（仓储 max+1 分配）：task.created=0 → 双写两条续 1/2，零冲突零丢失
    assert [(r.event_type, r.seq) for r in rows] == [("SUBRUN_STARTED", 1), ("SUBRUN_FINISHED", 2)]


async def test_双写钩子_APPROVAL_REQUIRED落库_回放通道自动带出(pg_env) -> None:
    """W2 复核残留③：APPROVAL_REQUIRED（W2-2b run 挂起事实）经双写钩子落 task_events。

    此前 SSE 钩子守卫直接 return 不落库（worker 路径才落）——两路径口径分裂；收编入
    EXEC_EVENT_PERSIST_RULES 后钩子落库且为回放根。回放完整性：GET /tasks/{id}/events
    取数口=tx.tasks.list_events（无事件类型过滤），落库行自动随回放带出——断线重连
    可还原挂起审批卡（waiting_since 供 30min SLA 倒计时续算，02 协议行 67）。
    """
    uow, factory = pg_env
    tenant_id, task_id = await _seed_task(pg_env)
    dual_write = build_exec_event_dual_write(uow, tenant_id)
    required = _translator().translate(
        _kernel_event(
            KERNEL_APPROVAL_PENDING,
            {
                "step_seq": 1,
                "param_hash": _APPROVAL_HASH,
                "action_iri": "http://ontology.example/action/external_write",
                "execution_mode": "external_write",
            },
        )
    )
    assert required is not None and required.name is ChatEventName.APPROVAL_REQUIRED
    await dual_write(task_id, required)

    async with uow.for_tenant(tenant_id) as tx:
        rows = await tx.tasks.list_events(task_id)  # 回放通道取数口（tasks.py list_task_events 同源）
    replay = [r for r in rows if r.event_type == "APPROVAL_REQUIRED"]
    assert len(replay) == 1  # 落库即自动带出（回放侧零过滤、零补丁）
    row = replay[0]
    assert row.data["param_hash"] == _APPROVAL_HASH and row.data["step_seq"] == 1
    assert row.data["task_id"] == str(TASK_ID) and row.data["run_id"] == str(RUN_ID)
    assert row.data["waiting_since"]  # SLA 倒计时权威起点随行（02 协议行 67）
    ApprovalRequiredPayload.model_validate(row.data)  # 回放行载荷过模型校验（形状冻结）


def test_chat_command_task_type缺省chat() -> None:
    """40 篇 §4.2：ChatCommand.task_type 透传通道存在，缺省 chat（向后兼容）。"""
    command = ChatCommand(
        tenant_id=TENANT,
        user_id=uuid.uuid4(),
        session_id=SESSION_ID,
        task_id=TASK_ID,
        run_id=RUN_ID,
        message="hi",
        trace_id=TRACE,
    )
    assert command.task_type == "chat"

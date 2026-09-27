# tests/gateway/test_task_events.py
"""任务事件时间线端点 + worker 事件落库 测试（api/01 §5.2 ★ GET /tasks/{id}/events）。

覆盖：JSON 游标分页（after_seq/limit/404）、SSE 回放帧（02 §5 帧格式）+ 终态收敛、
worker 路径非终态事件逐条落 task_events（RUN_FINISHED/RUN_ERROR 归结果汇防双写）。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from services.agent.api.tasks import list_task_events
from services.agent.business.chat_events import ChatCommand, ChatEvent, ChatEventName, ChatOutcome
from services.agent.business.task_worker import TaskRunWorker, finalize_outcome_on_task
from services.agent.data.repo_impl.task_poller import RunQueuePoller
from services.agent.domain.model.session import Message, Session
from services.agent.domain.model.task import RunRetryPolicy, Task, TaskEvent
from services.gateway.middlewares import GatewayError

pytestmark = pytest.mark.integration


async def _seed_task_with_events(gateway_uow, principal, agent_id: uuid.UUID, *, n_events: int = 5):
    """造任务 + n 条事件（seq 由仓储分配），返回 (task, run)。"""
    async with gateway_uow.for_tenant(principal.tenant_id) as tx:
        session = Session(id=uuid.uuid4(), tenant_id=principal.tenant_id, agent_id=agent_id, user_id=principal.user_id)
        await tx.sessions.add(session)
        seq = session.append_message("user", "分析停电原因")
        user_msg = Message(session_id=session.id, seq=seq, role="user", content="分析停电原因")
        await tx.sessions.append_message(session.id, user_msg)
        await tx.sessions.save_meta(session)
        task = Task(
            tenant_id=principal.tenant_id,
            type="chat",
            session_id=session.id,
            agent_id=agent_id,
            payload={"message_seq": seq},
        )
        run = task.start_run()
        await tx.tasks.save(task)
        for i in range(n_events):
            await tx.tasks.append_event(
                task.id,
                TaskEvent(task_id=task.id, event_type=f"RUN_STEP_{i}", data={"i": i}),
            )
    return task, run


async def test_事件时间线_JSON_游标分页(gateway_uow, seed):  # noqa: ANN001
    principal, agent_id = seed
    task, _ = await _seed_task_with_events(gateway_uow, principal, agent_id)
    page1 = await list_task_events(task.id, principal=principal, uow=gateway_uow, after_seq=None, limit=3)
    assert [e.seq for e in page1.items] == [0, 1, 2]
    assert page1.next_after_seq == 2 and page1.limit == 3
    page2 = await list_task_events(
        task.id, principal=principal, uow=gateway_uow, after_seq=page1.next_after_seq, limit=3
    )
    assert [e.seq for e in page2.items] == [3, 4]
    assert page2.items[0].event_type == "RUN_STEP_3"
    with pytest.raises(GatewayError) as ei:  # 越权/不存在 → 404
        await list_task_events(uuid.uuid4(), principal=principal, uow=gateway_uow, after_seq=None, limit=10)
    assert ei.value.status_code == 404


async def test_事件时间线_SSE_回放帧与终态收敛(gateway_uow, seed):  # noqa: ANN001
    principal, agent_id = seed
    task, run = await _seed_task_with_events(gateway_uow, principal, agent_id)
    # 任务置终态：SSE 尾随在回放完后即收敛（不悬挂）
    async with gateway_uow.for_tenant(principal.tenant_id) as tx:
        stored = await tx.tasks.get(task.id)
        assert stored is not None
        stored.runs[0].start()
        stored.runs[0].complete({"total_tokens": 1})
        stored.succeed()
        await tx.tasks.save(stored)

    from services.agent.api.tasks import _task_event_stream

    frames: list[bytes] = []
    async for frame in _task_event_stream(gateway_uow, principal.tenant_id, task.id, None, poll_interval=0.01):
        frames.append(frame)
    assert len(frames) == 5
    assert frames[0].startswith(b"id: 0\nevent: RUN_STEP_0\ndata: ")
    assert b'"i":0' in frames[0] or b'"i": 0' in frames[0]
    # 断点续传：after_seq=2 只回放剩余
    frames2: list[bytes] = []
    async for frame in _task_event_stream(gateway_uow, principal.tenant_id, task.id, 2, poll_interval=0.01):
        frames2.append(frame)
    assert [f.split(b"\n")[0] for f in frames2] == [b"id: 3", b"id: 4"]


class EventfulFakeOrchestrator:
    """成功路径编排器桩：多产主干波事件 + 履行结果汇契约（自包含，防跨文件耦合）。"""

    def __init__(self, uow: Any) -> None:
        self._uow = uow
        self.commands: list[ChatCommand] = []

    def stream_chat(self, command: ChatCommand) -> Any:
        return self._stream(command)

    async def _stream(self, command: ChatCommand) -> Any:
        self.commands.append(command)
        outcome = ChatOutcome(
            tenant_id=command.tenant_id,
            session_id=command.session_id,
            task_id=command.task_id,
            run_id=command.run_id,
            status="completed",
            answer="结论：线路过载。",
            usage={"total_tokens": 42},
        )
        async with self._uow.for_tenant(command.tenant_id) as tx:
            task = await tx.tasks.get(command.task_id)
            assert task is not None
            finalize_outcome_on_task(task, outcome)
            await tx.tasks.save(task)
        yield ChatEvent(
            name=ChatEventName.RUN_STARTED,
            data={"run_id": str(command.run_id)},
            run_id=command.run_id,
        )
        yield ChatEvent(
            name=ChatEventName.TEXT_MESSAGE_CONTENT,
            data={"delta": "结论："},
            run_id=command.run_id,
        )
        yield ChatEvent(
            name=ChatEventName.RUN_FINISHED,
            data={"run_id": str(command.run_id), "usage": {"total_tokens": 42}},
            run_id=command.run_id,
        )


async def test_worker_非终态事件落task_events_终态事件豁免(gateway_uow, seed):  # noqa: ANN001
    principal, agent_id = seed
    task, _run = await _seed_task_with_events(gateway_uow, principal, agent_id, n_events=0)
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from services.platform.config import Settings

    engine = create_async_engine(Settings().pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    inner = RunQueuePoller(factory)

    class TenantScoped:
        async def next_work(self) -> Any:
            return await inner.next_work(tenant_id=principal.tenant_id)

    worker = TaskRunWorker(
        uow=gateway_uow,
        poller=TenantScoped(),
        orchestrator_provider=lambda: EventfulFakeOrchestrator(gateway_uow),
        policy=RunRetryPolicy(base_seconds=0.001, cap_seconds=0.002, jitter_ratio=0.0),
        poll_interval_s=0.01,
    )
    assert await worker.poll_once() is True
    await engine.dispose()
    async with gateway_uow.for_tenant(principal.tenant_id) as tx:
        events = await tx.tasks.list_events(task.id)
    types = [e.event_type for e in events]
    assert "RUN_STARTED" in types and "TEXT_MESSAGE_CONTENT" in types  # 非终态事件逐条落库
    assert "RUN_FINISHED" not in types  # 终态事件归结果汇（防双写）


async def test_组合根账本sink_锚点事件落task_events并注run_id(gateway_uow, seed):  # noqa: ANN001
    """C1 投影工厂：kernel.* 事件经 sink 落 PG task_events，data 注 run_id（api/01 时间线取数口）。"""
    from services.agent.api.sessions import build_kernel_ledger_sink_factory
    from services.agent.domain.model.kernel_context import KernelEvent

    principal, agent_id = seed
    task, run = await _seed_task_with_events(gateway_uow, principal, agent_id, n_events=0)
    sink = build_kernel_ledger_sink_factory(gateway_uow)(task.id, run.id)
    await sink(
        KernelEvent(
            event_type="kernel.planned",
            tenant_id=principal.tenant_id,
            run_id=run.id,
            trace_id="trace-ledger-sink",
            data={"strategy": "chat.template_planner"},
        )
    )
    async with gateway_uow.for_tenant(principal.tenant_id) as tx:
        events = await tx.tasks.list_events(task.id)
    planned = [e for e in events if e.event_type == "kernel.planned"]
    assert len(planned) == 1
    assert planned[0].data["run_id"] == str(run.id)  # 组合根注入 run_id（对账四元组可追溯）
    assert planned[0].data["strategy"] == "chat.template_planner"

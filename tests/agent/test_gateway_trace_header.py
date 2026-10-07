# tests/agent/test_gateway_trace_header.py
"""C4 trace 贯通测试（红队审查 docs/评审/红队攻击性审查-2026-10-06 §5，2026-10-07 修复批）。

覆盖（红队 C4：builtin→httpx/provider 跳 trace 彻底丢失 + worker 202 重合成断链）：
- 网关非流式/流式两条出站路径注入 X-Trace-ID 请求头（伪 httpx MockTransport 断言）；
- 无 trace_id 调用零行为变化（不注入头）；
- worker 202 认领/审批 resume 重放：task.payload.origin_trace_id 回溯为命令 trace
  （original_trace_id 同步注记），无法回溯才用 worker 合成兜底；
- worker 事件 payload 注 original_trace_id（落 task_events 行可见）。
桩：httpx.MockTransport + 内存 FakeUow + 捕获型编排器，零真网零真库。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from services.agent.business.chat_events import ChatCommand, ChatEvent, ChatEventName
from services.agent.business.task_worker import TaskRunWorker
from services.agent.data.repo_impl.task_poller import WorkerClaim
from services.agent.domain.model.session import Message, Session
from services.agent.domain.model.task import RunRetryPolicy, Task, TaskEvent
from services.platform.llm.gateway import OpenAICompatibleModelPort

_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
_TENANT = uuid.uuid4()


# ── 网关出站面（伪 transport 断言请求头）─────────────────────────────────────────


def _llm_response() -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})


def _sse_response() -> httpx.Response:
    """流式响应：一帧增量 + usage 末块 + [DONE]（OpenAI 兼容 SSE 形态）。"""
    payload = json.dumps({"choices": [{"delta": {"content": "你好"}}]})
    usage = json.dumps({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
    body = (f"data: {payload}\n\n" f"data: {usage}\n\n" "data: [DONE]\n\n").encode()
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


def _capture_transport(captured: list[httpx.Request], *, response: httpx.Response) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_非流式出站_注入X_Trace_ID头():
    # Arrange：伪 transport 捕获出站请求
    captured: list[httpx.Request] = []
    port = OpenAICompatibleModelPort(
        base_url="http://llm", api_key="k", model="m", client=_capture_transport(captured, response=_llm_response())
    )
    # Act
    data = await port.complete_structured(system="s", user="u", json_schema=_SCHEMA, trace_id="req_abc123")
    # Assert：结构化输出照常返回 + 请求头带原值 X-Trace-ID（provider 侧可归因）
    assert data == {"ok": True}
    assert len(captured) == 1
    assert captured[0].headers["x-trace-id"] == "req_abc123"
    await port.aclose()


async def test_流式出站_注入X_Trace_ID头():
    # Arrange
    captured: list[httpx.Request] = []
    port = OpenAICompatibleModelPort(
        base_url="http://llm", api_key="k", model="m", client=_capture_transport(captured, response=_sse_response())
    )
    # Act：消费整流
    pieces = [
        piece async for piece in port.stream_complete([{"role": "user", "content": "hi"}], trace_id="req_stream9")
    ]
    # Assert：增量照常产出 + 请求头带原值 X-Trace-ID
    assert "".join(pieces) == "你好"
    assert len(captured) == 1
    assert captured[0].headers["x-trace-id"] == "req_stream9"
    await port.aclose()


async def test_无trace调用_不注入头_零行为变化():
    # Arrange
    captured: list[httpx.Request] = []
    port = OpenAICompatibleModelPort(
        base_url="http://llm", api_key="k", model="m", client=_capture_transport(captured, response=_llm_response())
    )
    # Act
    data = await port.complete_structured(system="s", user="u", json_schema=_SCHEMA)
    # Assert：无 trace_id → 无 X-Trace-ID 头（既有调用面零行为变化）
    assert data == {"ok": True}
    assert "x-trace-id" not in captured[0].headers
    await port.aclose()


async def test_凭证池形态_trace头与Authorization并存():
    # Arrange：有池形态——trace 头不得被轮换 Authorization 覆盖
    captured: list[httpx.Request] = []
    from services.platform.llm.cred_pool import CredentialPool

    pool = CredentialPool(provider="openai_compatible", keys=["k1"], cooldown_s=60, max_cooldown_s=600)
    port = OpenAICompatibleModelPort(
        base_url="http://llm",
        api_key="k1",
        model="m",
        client=_capture_transport(captured, response=_llm_response()),
        credential_pool=pool,
    )
    # Act
    await port.complete_structured(system="s", user="u", json_schema=_SCHEMA, trace_id="req_pool1")
    # Assert：双头并存（trace 贯通不因凭证轮换面丢失）
    assert captured[0].headers["x-trace-id"] == "req_pool1"
    assert captured[0].headers["authorization"] == "Bearer k1"
    await port.aclose()


# ── worker 202 面（origin_trace_id 回溯 + 事件注记）─────────────────────────────


class FakeTaskRepo:
    def __init__(self) -> None:
        self.tasks: dict[uuid.UUID, Task] = {}
        self.events: dict[uuid.UUID, list[TaskEvent]] = {}

    async def get(self, task_id: uuid.UUID) -> Task | None:
        return self.tasks.get(task_id)

    async def save(self, task: Task) -> None:
        self.tasks[task.id] = task

    async def append_event(self, task_id: uuid.UUID, event: TaskEvent, **_: Any) -> int:
        self.events.setdefault(task_id, []).append(event)
        return len(self.events[task_id])

    async def list_events(self, task_id: uuid.UUID, **_: Any) -> list[TaskEvent]:
        return list(self.events.get(task_id, []))


class FakeSessionRepo:
    def __init__(self, session: Session) -> None:
        self.sessions: dict[uuid.UUID, Session] = {session.id: session}
        self.messages: dict[tuple[uuid.UUID, int], Message] = {}

    async def get(self, session_id: uuid.UUID, *, user_id: uuid.UUID | None = None) -> Session | None:
        return self.sessions.get(session_id)

    async def get_message_by_seq(self, session_id: uuid.UUID, seq: int) -> Message | None:
        return self.messages.get((session_id, seq))


class FakeTx:
    def __init__(self, uow: FakeUow) -> None:
        self.tasks = uow.task_repo
        self.sessions = uow.session_repo

    async def __aenter__(self) -> FakeTx:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeUow:
    def __init__(self, session: Session) -> None:
        self.task_repo = FakeTaskRepo()
        self.session_repo = FakeSessionRepo(session)

    def for_tenant(self, tenant_id: uuid.UUID) -> FakeTx:
        return FakeTx(self)


class YieldingOrchestrator:
    """产出 RUN_STARTED+RUN_FINISHED 两事件的编排器桩（事件 payload 断言采集点）。"""

    def __init__(self) -> None:
        self.commands: list[ChatCommand] = []

    def stream_chat(self, command: ChatCommand) -> AsyncIterator[ChatEvent]:
        return self._stream(command)

    async def _stream(self, command: ChatCommand) -> AsyncIterator[ChatEvent]:
        self.commands.append(command)
        yield ChatEvent(name=ChatEventName.RUN_STARTED, data={"run_id": str(command.run_id)}, run_id=command.run_id)
        yield ChatEvent(
            name=ChatEventName.RUN_FINISHED, data={"run_id": str(command.run_id)}, run_id=command.run_id
        )


class QueuedPoller:
    def __init__(self, claim: WorkerClaim) -> None:
        self.claim = claim

    async def next_work(self) -> WorkerClaim:
        claim, self.claim = self.claim, None
        return claim


def _make_worker(uow: FakeUow, task: Task, run_id: uuid.UUID, orchestrator: YieldingOrchestrator) -> TaskRunWorker:
    return TaskRunWorker(
        uow=uow,
        poller=QueuedPoller(WorkerClaim(kind="queued", tenant_id=_TENANT, task_id=task.id, run_id=run_id)),
        orchestrator_provider=lambda: orchestrator,
        policy=RunRetryPolicy(base_seconds=0.001, cap_seconds=0.002, jitter_ratio=0.0),
        orphan_sweep_interval_s=30.0,
        orphan_running_timeout_s=300.0,
    )


def _seed_task(uow: FakeUow, session: Session, *, origin: str | None) -> tuple[Task, uuid.UUID]:
    task = Task(
        tenant_id=_TENANT,
        type="chat",
        session_id=session.id,
        payload={"message_seq": 1, **({"origin_trace_id": origin} if origin else {})},
    )
    run = task.start_run()
    uow.task_repo.tasks[task.id] = task
    uow.session_repo.messages[(session.id, 1)] = Message(session_id=session.id, seq=1, role="user", content="触发")
    return task, run.id


async def test_worker认领_回溯网关原trace_命令与事件注记贯通():
    """红队 C4 主断言：202 受理任务的 worker 重放不再断链——命令 trace=受理网关 trace，
    事件 payload 显式注 original_trace_id（task_events 行可见）。"""
    # Arrange：受理面已在 task.payload 落 origin_trace_id（send_message C4 修复形态）
    session = Session(id=uuid.uuid4(), tenant_id=_TENANT, agent_id=uuid.uuid4(), user_id=uuid.uuid4())
    uow = FakeUow(session)
    task, run_id = _seed_task(uow, session, origin="req_gateway42")
    orchestrator = YieldingOrchestrator()
    worker = _make_worker(uow, task, run_id, orchestrator)
    # Act：认领执行
    assert await worker.poll_once() is True
    # Assert ①：命令 trace 复用网关原 trace（受理链不断）；original_trace_id 同步注记
    command = orchestrator.commands[0]
    assert command.trace_id == "req_gateway42"
    assert command.original_trace_id == "req_gateway42"
    # Assert ②：事件 payload 注 original_trace_id（RUN_STARTED 落 task_events 行）
    started = next(e for e in uow.task_repo.events[task.id] if e.event_type == "RUN_STARTED")
    assert started.data["original_trace_id"] == "req_gateway42"


async def test_worker认领_无origin回溯_worker合成trace兜底_零注记():
    # Arrange：存量任务形态（payload 无 origin_trace_id）
    session = Session(id=uuid.uuid4(), tenant_id=_TENANT, agent_id=uuid.uuid4(), user_id=uuid.uuid4())
    uow = FakeUow(session)
    task, run_id = _seed_task(uow, session, origin=None)
    orchestrator = YieldingOrchestrator()
    worker = _make_worker(uow, task, run_id, orchestrator)
    # Act
    assert await worker.poll_once() is True
    # Assert：兜底现状——worker 合成 trace，original_trace_id 为 None，事件零注记
    command = orchestrator.commands[0]
    assert command.trace_id == f"worker-{run_id}"
    assert command.original_trace_id is None
    started = next(e for e in uow.task_repo.events[task.id] if e.event_type == "RUN_STARTED")
    assert "original_trace_id" not in started.data

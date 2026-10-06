# tests/agent/test_worker_resume_with_tickets.py
"""运行中审批携票重放接线测试（H-0b 接线 B，2026-09-29；审批断链销项）。

worker resume 通道（poller kind=resume → _execute_resume）：
- 有效票：票仓移除（防双消费）→ ChatCommand.approvals 携票（param_hash/approver/时效还原）
  → drain 重放；at-least-once 语义=移除后 drain 前崩溃由孤儿回收兜底；
- 过期票：视同无回执 → run.fail(2001) + task.fail + run.approval_expired 审计行，不重放；
- 护栏：活跃指针不符 / 票属他 run → 幂等跳过；触发消息缺失 → 3001 失败闭合。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from services.agent.business.chat_events import ChatCommand, ChatEvent
from services.agent.business.task_worker import TaskRunWorker
from services.agent.data.repo_impl.task_poller import WorkerClaim
from services.agent.domain.model.session import Message, Session
from services.agent.domain.model.task import RunRetryPolicy, RunStatus, Task, TaskEvent, TaskStatus

_TENANT = uuid.uuid4()
_MSG = "确认高危操作"
# 票时效相对真实时钟构造（worker 判定用 _utcnow()，固定字面量会随时钟漂移翻转）
_FRESH = datetime.now(UTC) + timedelta(hours=1)
_STALE = datetime.now(UTC) - timedelta(minutes=1)


def _ticket_row(run_id: uuid.UUID, *, expires: datetime | None = _FRESH) -> dict[str, Any]:
    return {
        "ticket_id": str(uuid.uuid4()),
        "run_id": str(run_id),
        "action_iri": "http://o/DispatchRepair",
        "param_hash": "hash-123",
        "approved_by": str(uuid.uuid4()),
        "decided_at": datetime.now(UTC).isoformat(),
        "expires_at": expires.isoformat() if expires else None,
    }


class FakeTaskRepo:
    def __init__(self) -> None:
        self.tasks: dict[uuid.UUID, Task] = {}
        self.events: dict[uuid.UUID, list[TaskEvent]] = {}

    async def get(self, task_id: uuid.UUID) -> Task | None:
        return self.tasks.get(task_id)

    async def save(self, task: Task) -> None:
        self.tasks[task.id] = task

    async def append_event(self, task_id: uuid.UUID, event: TaskEvent) -> int:
        self.events.setdefault(task_id, []).append(event)
        return len(self.events[task_id])

    async def list_events(self, task_id: uuid.UUID, **_: Any) -> list[TaskEvent]:
        return list(self.events.get(task_id, []))


class FakeSessionRepo:
    def __init__(self) -> None:
        self.sessions: dict[uuid.UUID, Session] = {}
        self.messages: dict[tuple[uuid.UUID, int], Message] = {}

    async def get(self, session_id: uuid.UUID) -> Session | None:
        return self.sessions.get(session_id)

    async def get_message_by_seq(self, session_id: uuid.UUID, seq: int) -> Message | None:
        return self.messages.get((session_id, seq))


class FakeTx:
    def __init__(self, tasks: FakeTaskRepo, sessions: FakeSessionRepo) -> None:
        self.tasks = tasks
        self.sessions = sessions

    async def __aenter__(self) -> FakeTx:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeUow:
    def __init__(self) -> None:
        self.task_repo = FakeTaskRepo()
        self.session_repo = FakeSessionRepo()

    def for_tenant(self, tenant_id: uuid.UUID) -> FakeTx:
        return FakeTx(self.task_repo, self.session_repo)


class CaptureOrchestrator:
    def __init__(self) -> None:
        self.commands: list[ChatCommand] = []

    def stream_chat(self, command: ChatCommand) -> AsyncIterator[ChatEvent]:
        return self._empty(command)

    async def _empty(self, command: ChatCommand) -> AsyncIterator[ChatEvent]:
        self.commands.append(command)
        return
        yield  # pragma: no cover


class ResumePoller:
    def __init__(self) -> None:
        self.queue: list[WorkerClaim] = []

    async def next_work(self) -> WorkerClaim | None:
        return self.queue.pop(0) if self.queue else None


def make_resume_env(
    *, tickets: list[dict[str, Any]] | None = None, with_message: bool = True
) -> tuple[TaskRunWorker, FakeUow, CaptureOrchestrator, Task, Any]:
    uow = FakeUow()
    session = Session(id=uuid.uuid4(), tenant_id=_TENANT, agent_id=uuid.uuid4(), user_id=uuid.uuid4())
    uow.session_repo.sessions[session.id] = session
    if with_message:
        uow.session_repo.messages[(session.id, 1)] = Message(session_id=session.id, seq=1, role="user", content=_MSG)

    payload: dict[str, Any] = {"message_seq": 1}
    task = Task(tenant_id=_TENANT, type="chat", session_id=session.id, payload=payload)
    run = task.start_run()
    run._transition(RunStatus.RUNNING)
    task.payload = {**(task.payload or {}), "approvals": tickets if tickets is not None else [_ticket_row(run.id)]}
    uow.task_repo.tasks[task.id] = task

    orch = CaptureOrchestrator()
    worker = TaskRunWorker(
        uow=uow,
        poller=ResumePoller(),
        orchestrator_provider=lambda: orch,
        policy=RunRetryPolicy(base_seconds=0.001, cap_seconds=0.002, jitter_ratio=0.0),
        rng=lambda: 0.5,
    )
    worker._poller.queue.append(WorkerClaim(kind="resume", tenant_id=_TENANT, task_id=task.id, run_id=run.id))
    return worker, uow, orch, task, run


async def test_有效票_携票重放且票仓移除():
    worker, uow, orch, task, run = make_resume_env()
    expected_approver = task.payload["approvals"][0]["approved_by"]
    assert await worker.poll_once() is True
    assert len(orch.commands) == 1
    command = orch.commands[0]
    assert command.trace_id.startswith("worker-resume-")
    assert len(command.approvals) == 1
    ticket = command.approvals[0]
    assert ticket.param_hash == "hash-123" and str(ticket.approved_by) == expected_approver
    # 票仓已消费（防双消费）
    assert not (task.payload or {}).get("approvals")
    # 状态仍 running（重放驱动后续由编排器终态回写）
    assert run.status is RunStatus.RUNNING and task.status is TaskStatus.RUNNING


async def test_过期票_视同无回执_失败闭合不重放():
    worker, uow, orch, task, run = make_resume_env(tickets=None)
    expired = _ticket_row(run.id, expires=_STALE)
    task.payload = {**(task.payload or {}), "approvals": [expired]}
    assert await worker.poll_once() is True
    assert orch.commands == []
    assert run.status is RunStatus.FAILED and run.error["code"] == 2001
    assert task.status is TaskStatus.FAILED
    events = uow.task_repo.events.get(task.id, [])
    assert any(e.event_type == "run.approval_expired" for e in events)


async def test_护栏_活跃指针不符_幂等跳过():
    worker, uow, orch, task, run = make_resume_env()
    other = uuid.uuid4()
    worker._poller.queue[0] = WorkerClaim(kind="resume", tenant_id=_TENANT, task_id=task.id, run_id=other)
    assert await worker.poll_once() is False
    assert orch.commands == []
    assert task.payload.get("approvals")  # 票未动


async def test_护栏_票属他run_不动票不重放():
    worker, uow, orch, task, run = make_resume_env(tickets=[_ticket_row(uuid.uuid4())])
    assert await worker.poll_once() is False
    assert orch.commands == []
    assert task.payload.get("approvals")  # 他 run 票保持原样


async def test_触发消息缺失_3001失败闭合():
    worker, uow, orch, task, run = make_resume_env(with_message=False)
    assert await worker.poll_once() is True
    assert orch.commands == []
    assert run.status is RunStatus.FAILED and run.error["code"] == 3001

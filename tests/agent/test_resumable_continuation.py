# tests/agent/test_resumable_continuation.py
"""对账续跑 v1 注入测试（H-0c ②，2026-09-29 批；评审行 20「锚点零消费者」销项）。

重放路径（重试/恢复认领）重建 ChatCommand 时：前序 Run 的 kernel.step_validated 投影
（task_events，载荷含 sink 注入的 run_id）→ continuation 系统注记注入重放消息 +
锚点摘要写 task.payload.resumable_anchors（可观测）。内核级步跳过登记遗留（独立批）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from services.agent.business.chat_events import ChatCommand, ChatEvent
from services.agent.business.task_worker import TaskRunWorker
from services.agent.data.repo_impl.task_poller import WorkerClaim
from services.agent.domain.model.session import Message, Session
from services.agent.domain.model.task import RunRetryPolicy, RunStatus, Task, TaskEvent

_TENANT = uuid.uuid4()
_MSG = "分析昨夜城东线路停电原因"
_IRI = "http://ontology.example/action/%s"


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
    """编排器桩：捕获重放命令（无事件流——终态回写不在本测面）。"""

    def __init__(self) -> None:
        self.commands: list[ChatCommand] = []

    def stream_chat(self, command: ChatCommand) -> AsyncIterator[ChatEvent]:
        return self._empty(command)

    async def _empty(self, command: ChatCommand) -> AsyncIterator[ChatEvent]:
        self.commands.append(command)
        return
        yield  # pragma: no cover


class QueuedPoller:
    def __init__(self) -> None:
        self.queue: list[WorkerClaim] = []

    async def next_work(self) -> WorkerClaim | None:
        return self.queue.pop(0) if self.queue else None


def make_replay_env(
    *, prior_validated: list[dict[str, Any]], include_current_run_event: bool = False
) -> tuple[TaskRunWorker, FakeUow, CaptureOrchestrator, Task]:
    """重放形态构造：首 Run 已 failed（attempt=1）→ 重试 Run queued（attempt=2）。

    prior_validated 为前序 Run 的 validated 步锚点（step_seq/action_iri）；
    include_current_run_event=True 时额外塞一条当前 Run 的 validated 事件（应被排除）。
    """
    uow = FakeUow()
    session = Session(id=uuid.uuid4(), tenant_id=_TENANT, agent_id=uuid.uuid4(), user_id=uuid.uuid4())
    uow.session_repo.sessions[session.id] = session
    uow.session_repo.messages[(session.id, 1)] = Message(session_id=session.id, seq=1, role="user", content=_MSG)

    task = Task(tenant_id=_TENANT, type="chat", session_id=session.id, payload={"message_seq": 1})
    first = task.start_run()  # attempt=1
    first._transition(RunStatus.RUNNING)
    first.fail({"code": 5001, "message": "模型超时", "retryable": True})
    retry = task.start_retry_run()  # attempt=2（重放对象，queued）
    uow.task_repo.tasks[task.id] = task

    events: list[TaskEvent] = []
    for anchor in prior_validated:
        events.append(
            TaskEvent(
                task_id=task.id,
                event_type="kernel.step_validated",
                data={"run_id": str(first.id), "stage": "observation", **anchor},
            )
        )
    if prior_validated:  # 干扰项：非锚点事件不进注记
        events.append(
            TaskEvent(
                task_id=task.id,
                event_type="kernel.step_failed",
                data={"run_id": str(first.id), "step_seq": 99, "action_iri": _IRI % "failed_step"},
            )
        )
        events.append(  # 历史事件（无 run_id，早于投影 sink 注入 run_id 的存量形态）：视为前序
            TaskEvent(
                task_id=task.id, event_type="kernel.step_validated", data={"step_seq": 9, "action_iri": _IRI % "legacy"}
            )
        )
    if include_current_run_event:
        events.append(
            TaskEvent(
                task_id=task.id,
                event_type="kernel.step_validated",
                data={"run_id": str(retry.id), "step_seq": 7, "action_iri": _IRI % "current_only"},
            )
        )
    uow.task_repo.events[task.id] = events

    orchestrator = CaptureOrchestrator()
    poller = QueuedPoller()
    poller.queue.append(WorkerClaim(kind="queued", tenant_id=_TENANT, task_id=task.id, run_id=retry.id))
    worker = TaskRunWorker(
        uow=uow,
        poller=poller,
        orchestrator_provider=lambda: orchestrator,
        policy=RunRetryPolicy(base_seconds=0.001, cap_seconds=0.002, jitter_ratio=0.0),
        orphan_sweep_interval_s=30.0,
        orphan_running_timeout_s=300.0,
    )
    return worker, uow, orchestrator, task


async def test_重放注入前序validated锚点注记_并写payload锚点摘要():
    anchors = [
        {"step_seq": 2, "action_iri": _IRI % "query_grid"},
        {"step_seq": 1, "action_iri": _IRI % "read_data"},
    ]
    worker, uow, orchestrator, task = make_replay_env(prior_validated=anchors, include_current_run_event=True)
    assert await worker.poll_once() is True
    assert len(orchestrator.commands) == 1
    command = orchestrator.commands[0]
    assert command.message.startswith(_MSG)  # 原触发消息保留在首位
    assert "[系统注记｜对账续跑]" in command.message and "勿重做" in command.message
    assert (_IRI % "read_data") in command.message and (_IRI % "query_grid") in command.message
    assert (_IRI % "failed_step") not in command.message  # step_failed 非锚点
    assert (_IRI % "current_only") not in command.message  # 当前 Run 自身事件排除
    assert (_IRI % "legacy") in command.message  # 无 run_id 历史事件视为前序
    stored = uow.task_repo.tasks[task.id]
    current_run_id = stored.active_run_id
    assert stored.payload["resumable_anchors"] == {
        "run_id": str(current_run_id),
        "steps": [
            {"step_seq": 1, "action_iri": _IRI % "read_data"},
            {"step_seq": 2, "action_iri": _IRI % "query_grid"},
            {"step_seq": 9, "action_iri": _IRI % "legacy"},
        ],  # 排序稳定（step_seq 升序）；干扰项与当前 Run 事件不入摘要
    }


async def test_无锚点_消息原样重放():
    worker, uow, orchestrator, task = make_replay_env(prior_validated=[])
    assert await worker.poll_once() is True
    assert orchestrator.commands[0].message == _MSG  # 无前序锚点：零注入
    assert "resumable_anchors" not in uow.task_repo.tasks[task.id].payload


async def test_首试执行不查锚点_attempt为1时零注入():
    """首次执行（非重放）即使存在 validated 投影也不注入（重放语义=attempt>1）。"""
    worker, uow, orchestrator, task = make_replay_env(prior_validated=[{"step_seq": 1, "action_iri": _IRI % "x"}])
    stored = uow.task_repo.tasks[task.id]
    stored.attempt_count = 1  # 型内改口径：模拟首试（聚合纪律豁免，桩直改）
    assert await worker.poll_once() is True
    assert orchestrator.commands[0].message == _MSG
    assert "resumable_anchors" not in uow.task_repo.tasks[task.id].payload

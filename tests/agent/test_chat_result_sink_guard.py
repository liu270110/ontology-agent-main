# tests/agent/test_chat_result_sink_guard.py
"""对话结果汇落史守卫单元测试（B-④ 联调修复：失败/空答案 run 不落空 assistant 行）。

断言目标（services/agent/api/sessions.py ``build_chat_result_sink``）：
- 失败 run（error_code 非空）：不调 tx.sessions.append_message（无空 assistant 行）、
  聚合 next_seq 不前进；run.error 终态事件与 task 行回写照常（失败仍留痕不丢账）；
- 成功 run 有答案：照常落史（对照，防守卫误伤正常路径）；
- 成功但空答案：跳过落史（对齐编排器 L1「if outcome.answer」守卫口径，
  chat_orchestrator 步骤④）。

零外部依赖：UoW/仓储以最小桩替身（消费面=for_tenant/sessions.get/append_message +
tasks.append_event/get/save，聚合用真 Session/Task 域模型）；与 tests/gateway
/test_task_worker 的 PG 结果汇用例互补——彼处锁终态回写落库，本文件锁落史守卫形状。
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

from services.agent.api.sessions import build_chat_result_sink
from services.agent.business.chat_events import ChatOutcome
from services.agent.domain.model.session import Message, Session
from services.agent.domain.model.task import Task, TaskEvent, TaskStatus


class _StubSessionsRepo:
    """sessions 仓储桩：只登记 append_message 调用（落史观察口）。"""

    def __init__(self, session: Session | None) -> None:
        self._session = session
        self.appended: list[Message] = []

    async def get(self, session_id: uuid.UUID) -> Session | None:
        return self._session if self._session.id == session_id else None

    async def append_message(self, session_id: uuid.UUID, message: Message) -> None:
        self.appended.append(message)


class _StubTasksRepo:
    """tasks 仓储桩：登记终态事件与 save 调用（失败留痕观察口）。"""

    def __init__(self, task: Task | None) -> None:
        self._task = task
        self.events: list[TaskEvent] = []
        self.saved = 0

    async def append_event(self, task_id: uuid.UUID, event: TaskEvent) -> None:
        self.events.append(event)

    async def get(self, task_id: uuid.UUID) -> Task | None:
        return self._task if self._task is not None and self._task.id == task_id else None

    async def save(self, task: Task) -> None:
        self.saved += 1


class _StubUow:
    """AsyncUnitOfWork 消费面桩：for_tenant → 预置 tx 的空事务。"""

    def __init__(self) -> None:
        self.sessions: _StubSessionsRepo | None = None
        self.tasks: _StubTasksRepo | None = None

    def wire(self, session: Session | None, task: Task | None) -> None:
        self.sessions = _StubSessionsRepo(session)
        self.tasks = _StubTasksRepo(task)

    def for_tenant(self, tenant_id: uuid.UUID) -> Any:
        uow = self

        @asynccontextmanager
        async def _tx() -> Any:
            assert uow.sessions is not None and uow.tasks is not None
            yield type("_Tx", (), {"sessions": uow.sessions, "tasks": uow.tasks})()

        return _tx()


def _make_aggregates() -> tuple[Session, Task, uuid.UUID]:
    """真聚合：活跃会话 + 已受理 run（id 供 outcome 对齐 finalize 回写）。"""
    tenant_id = uuid.uuid4()
    session = Session(id=uuid.uuid4(), tenant_id=tenant_id, agent_id=uuid.uuid4(), user_id=uuid.uuid4())
    task = Task(tenant_id=tenant_id, type="chat", session_id=session.id, agent_id=session.agent_id, payload={})
    run_id = task.start_run().id
    return session, task, run_id


def _outcome(session: Session, task: Task, run_id: uuid.UUID, **kw: Any) -> ChatOutcome:
    base: dict[str, Any] = {
        "tenant_id": session.tenant_id,
        "session_id": session.id,
        "task_id": task.id,
        "run_id": run_id,
        "status": "completed",
        "answer": "结论：线路过载。",
    }
    base.update(kw)
    return ChatOutcome(**base)


async def test_失败run_不落assistant行_终态留痕照常() -> None:
    # Arrange：失败 outcome（5001 模型超时，答案空——失败路径的真实形状）
    session, task, run_id = _make_aggregates()
    uow = _StubUow()
    uow.wire(session, task)
    sink = build_chat_result_sink(uow)  # type: ignore[arg-type]
    # Act
    await sink(_outcome(session, task, run_id, status="failed", answer="", error_code=5001, error_message="模型超时"))
    # Assert：零空 assistant 行（B-④ 主断言）+ 聚合 seq 不前进
    assert uow.sessions is not None and uow.sessions.appended == []
    assert session.next_seq == 0
    # Assert：失败仍留痕——run.error 终态事件 + task 行回写（守卫不误伤账务）
    assert uow.tasks is not None
    assert [e.event_type for e in uow.tasks.events] == ["run.error"]
    assert uow.tasks.events[0].data["code"] == 5001
    assert uow.tasks.saved == 1


async def test_成功run有答案_照常落史() -> None:
    # Arrange
    session, task, run_id = _make_aggregates()
    uow = _StubUow()
    uow.wire(session, task)
    sink = build_chat_result_sink(uow)  # type: ignore[arg-type]
    # Act
    await sink(_outcome(session, task, run_id))
    # Assert：assistant 行恰一条（seq 由聚合分配）+ 成功终态
    assert uow.sessions is not None
    assert len(uow.sessions.appended) == 1
    assert uow.sessions.appended[0].role == "assistant"
    assert uow.sessions.appended[0].content == "结论：线路过载。"
    assert uow.sessions.appended[0].seq == session.next_seq - 1
    assert uow.tasks is not None and [e.event_type for e in uow.tasks.events] == ["run.finished"]


async def test_成功但空答案_跳过落史() -> None:
    # Arrange：L1 同款守卫口径（chat_orchestrator 步骤④「if outcome.answer」）
    session, task, run_id = _make_aggregates()
    uow = _StubUow()
    uow.wire(session, task)
    sink = build_chat_result_sink(uow)  # type: ignore[arg-type]
    # Act
    await sink(_outcome(session, task, run_id, answer=""))
    # Assert：零空 assistant 行；完成终态事件照发
    assert uow.sessions is not None and uow.sessions.appended == []
    assert session.next_seq == 0
    assert uow.tasks is not None and [e.event_type for e in uow.tasks.events] == ["run.finished"]


async def test_不可重试失败_task落failed_仍零assistant行() -> None:
    # Arrange：终局失败（retryable=False）——守卫与终态回写正交（04 §3 失败终局）
    session, task, run_id = _make_aggregates()
    uow = _StubUow()
    uow.wire(session, task)
    sink = build_chat_result_sink(uow)  # type: ignore[arg-type]
    # Act
    await sink(_outcome(session, task, run_id, status="failed", answer="", error_code=3001, error_message="门禁拒绝"))
    # Assert：零 assistant 行 + task 终局 failed（finalize 回写未受守卫影响）
    assert uow.sessions is not None and uow.sessions.appended == []
    assert uow.tasks is not None
    assert task.status is TaskStatus.FAILED
    assert uow.tasks.saved == 1

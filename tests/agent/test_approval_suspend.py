# tests/agent/test_approval_suspend.py
"""W2-2b 审批挂起语义测试（docs/api/对账-对话执行事件后端提案 W2-2b + 11 篇 §6.2）。

覆盖（方案=内核三段式：挂起结算 / SLA 超时默认拒绝 / 开关回退）：
- 内核挂起：缺回执 → 步停 waiting_approval（不 FAILED）、后续步不再调度、
  计划项保持 in_progress、结算 waiting_tool；事件序 = approval_pending → settled
  （无 approval_denied / step_failed / RUN_ERROR 形态的 approval_denied）；
- 开关回退：kernel_approval_suspend=False → 立即 FAILED 默认拒绝（2026-10-07 前语义）；
- 编排器映射：挂起 waiting_tool outcome 非失败（error_code=None）；
  blocked_by_trust 形态（步全终态）维持既有失败映射（零行为变化面）；
- 聚合回写：finalize_outcome_on_task → run.wait_external + task 保持 RUNNING；
- worker SLA 超时默认拒绝：cancel 形态（B5 同码 2001）+ 步 FAILED 合成闭合 +
  kernel.approval_timeout 审计行 + run.error 行；护栏=无锚点不误杀 / 开关关不扫 /
  状态机幂等跳过；
- approve→resume 全链（fake 票）：内核挂起 → finalize 落 waiting_tool →
  RunApprovalService.decide(approve) 过核验链（waiting_tool 检查）→ 票仓+resume 触发 →
  worker 携票重放 → 内核带票执行完成。
桩：内存 FakeUow + 捕获型编排器 + FakeTool/FakePlanner 内核直跑（零真网零真库）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from services.agent.api.sessions import build_kernel_ledger_sink_factory
from services.agent.business.approval_service import PENDING_KEY, TICKETS_KEY, RunApprovalService
from services.agent.business.chat_events import ChatCommand, ChatEvent, ChatOutcome
from services.agent.business.chat_orchestrator import ChatOrchestrator
from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.gate_baseline import canonical_param_hash
from services.agent.business.kernel.loop import AgentKernel
from services.agent.business.task_worker import TaskRunWorker, finalize_outcome_on_task
from services.agent.data.repo_impl.task_poller import WorkerClaim
from services.agent.domain.model.kernel_actions import ExecutionMode
from services.agent.domain.model.kernel_context import KernelEvent
from services.agent.domain.model.session import Message, Session
from services.agent.domain.model.step_state import StepStatus
from services.agent.domain.model.task import Run, RunStatus, Task, TaskEvent, TaskStatus
from services.platform.errors import ErrorCode
from tests.agent.conftest import (
    ACTION_IRI,
    WRITE_ACTION_IRI,
    FakePlanner,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)

_TENANT = uuid.uuid4()
_USER = uuid.uuid4()
_FIXED_NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)


async def _capture(
    sink: list[tuple[uuid.UUID | None, str, dict[str, Any]]],
    sid: uuid.UUID | None,
    name: str,
    data: dict[str, Any],
) -> None:
    """SSE 发布捕获桩（worker event_publisher 签名：session_id, name, data）。"""
    sink.append((sid, name, dict(data)))


def _patch_suspend(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    """钉 W2-2b 开关（loop 与 execution 两处消费命名空间同源替换，test_kernel_timeout_settings 先例）。"""
    from services.platform.config import Settings

    fake = Settings(kernel_approval_suspend=enabled)
    monkeypatch.setattr("services.agent.business.kernel.loop.get_settings", lambda: fake)
    monkeypatch.setattr("services.agent.business.kernel.execution.get_settings", lambda: fake)


def _suspend_plan(params: dict[str, Any]) -> tuple[Any, Any, Any]:
    """写步（审批面，缺回执必挂起）+ 后继读步（断言挂起后不再调度）的工具与计划。"""
    write_tool = FakeTool(action_iri=WRITE_ACTION_IRI)
    read_tool = FakeTool(action_iri=ACTION_IRI)
    steps = (
        make_step(seq=1, action_iri=WRITE_ACTION_IRI, mode=ExecutionMode.EXTERNAL_WRITE, params=params),
        make_step(seq=2, action_iri=ACTION_IRI, mode=ExecutionMode.READ),
    )
    planner = FakePlanner(make_candidate(steps))  # type: ignore[arg-type]
    dispatcher = make_tool_dispatcher(write_tool, register_planning_strategy=(planner,))
    dispatcher.register_tool(read_tool)
    return write_tool, read_tool, dispatcher


# ── 内核挂起（开关默认开）─────────────────────────────────────────────────────


async def test_挂起缺回执_步停waiting_approval_run落waiting_tool_后继步不再执行(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_suspend(monkeypatch, enabled=True)
    params = {"q": "高危写操作"}
    write_tool, read_tool, dispatcher = _suspend_plan(params)
    kernel = AgentKernel(dispatcher)
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))

    # run 终态=waiting_tool（approval_service 核验链要求的态）；步停 waiting_approval 非终态
    assert outcome.status == str(RunStatus.WAITING_TOOL)
    assert outcome.reason_code is None
    states = sorted(outcome.terminal_states, key=lambda s: s.seq)
    assert states[0].status is StepStatus.WAITING_APPROVAL  # 写步：挂起在途
    assert write_tool.calls == []  # 无回执不执行（B5 不变式保持）
    assert read_tool.calls == []  # 挂起中止调度：后继读步不再执行

    ledger = kernel.last_ledger
    assert ledger is not None
    types = [e.event_type for e in ledger.events]
    assert "kernel.approval_pending" in types
    assert "kernel.approval_denied" not in types  # 挂起 ≠ 默认拒绝
    assert "kernel.step_failed" not in types
    # 事件序：pending 锚点先于 settled（APPROVAL_REQUIRED 转译源先于 run 终态锚点）
    assert types.index("kernel.approval_pending") < types.index("kernel.settled")
    settled = next(e for e in ledger.events if e.event_type == "kernel.settled")
    assert settled.data["status"] == "waiting_tool"


async def test_挂起结算_计划项保持in_progress_不误标completed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_suspend(monkeypatch, enabled=True)
    _, _, dispatcher = _suspend_plan({"q": "高危写操作"})
    kernel = AgentKernel(dispatcher)
    await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    plan_events = [e for e in kernel.last_ledger.events if e.event_type == "kernel.plan_updated"]  # type: ignore[union-attr]
    assert plan_events
    items = plan_events[-1].data["items"]
    by_id = {i["id"]: i["status"] for i in items}
    assert by_id["p1"] == "in_progress"  # 挂起步未跑完：诚实呈现，不标 completed
    assert by_id["p2"] == "pending"  # 未调度步保持 pending


async def test_开关False回退_立即FAILED默认拒绝_旧行为逐字保留(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_suspend(monkeypatch, enabled=False)
    params = {"q": "高危写操作"}
    write_tool, read_tool, dispatcher = _suspend_plan(params)
    kernel = AgentKernel(dispatcher)
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))

    assert outcome.status == str(RunStatus.FAILED)
    states = sorted(outcome.terminal_states, key=lambda s: s.seq)
    assert states[0].status is StepStatus.FAILED
    assert "默认拒绝" in (states[0].error or "")
    assert write_tool.calls == []
    assert len(read_tool.calls) == 1  # 旧行为对照：失败步后继续调度（挂起语义才中止后续步）
    ledger = kernel.last_ledger
    assert ledger is not None
    types = [e.event_type for e in ledger.events]
    assert "kernel.approval_denied" in types  # 旧拒绝锚点保留
    assert "kernel.approval_pending" in types  # pending 锚点两态共发（现状不变）


# ── 编排器 outcome 映射 ───────────────────────────────────────────────────────


def _fake_kernel_outcome(status: str, terminal: StepStatus) -> SimpleNamespace:
    return SimpleNamespace(
        status=status, reason_code=None, reason="x", terminal_states=(SimpleNamespace(status=terminal),)
    )


def _build_outcome(kernel_outcome: object) -> ChatOutcome:
    command = SimpleNamespace(
        tenant_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        agent_id=None,
    )
    context = SimpleNamespace(citations=[], degraded=False)
    box = SimpleNamespace(answer="", usage={}, ok=False, error_code=None, error_message=None)
    orchestrator = object.__new__(ChatOrchestrator)  # _build_outcome 纯映射不触实例状态
    return orchestrator._build_outcome(command, context, box, kernel_outcome, None, 0.0)  # type: ignore[arg-type]


def test_编排器挂起outcome_非失败_无错误码() -> None:
    outcome = _build_outcome(_fake_kernel_outcome("waiting_tool", StepStatus.WAITING_APPROVAL))
    assert outcome.status == "waiting_tool"
    assert outcome.error_code is None  # 不走失败映射 → wire 无 RUN_ERROR
    assert outcome.retryable is False


def test_编排器_blocked_by_trust形态_维持既有失败映射() -> None:
    """waiting_tool 但步全终态（判据 blocked）→ 既有失败映射不变（W2-2b 零行为变化面）。"""
    outcome = _build_outcome(_fake_kernel_outcome("waiting_tool", StepStatus.VALIDATED))
    assert outcome.status == "waiting_tool"
    assert outcome.error_code == int(ErrorCode.INTERNAL_ERROR)  # 2026-10-07 前口径


# ── 聚合回写（finalize_outcome_on_task）──────────────────────────────────────


def _running_task() -> tuple[Task, Run]:
    task_id, run_id = uuid.uuid4(), uuid.uuid4()
    run = Run(id=run_id, tenant_id=_TENANT, task_id=task_id, status=RunStatus.RUNNING)
    task = Task(
        id=task_id,
        tenant_id=_TENANT,
        type="chat",
        status=TaskStatus.RUNNING,
        session_id=uuid.uuid4(),
        active_run_id=run_id,
        payload={"message_seq": 0},
        runs=[run],
    )
    return task, run


def _waiting_outcome(run_id: uuid.UUID) -> ChatOutcome:
    return ChatOutcome(
        tenant_id=_TENANT,
        session_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        run_id=run_id,
        status="waiting_tool",
        answer="",
    )


def test_finalize挂起_run落waiting_tool_task保持RUNNING() -> None:
    task, run = _running_task()
    finalize_outcome_on_task(task, _waiting_outcome(run.id))
    assert run.status is RunStatus.WAITING_TOOL  # H-0b 核验链要求态
    assert task.status is TaskStatus.RUNNING  # 合法长等：不落终态、不入重试监督


def test_finalize挂起_queued未认领形态_补认领再入等待() -> None:
    task, run = _running_task()
    run.status = RunStatus.QUEUED  # 内联受理未认领形态
    finalize_outcome_on_task(task, _waiting_outcome(run.id))
    assert run.status is RunStatus.WAITING_TOOL


def test_finalize挂起带错误码_维持既有失败路径() -> None:
    task, run = _running_task()
    outcome = ChatOutcome(
        tenant_id=_TENANT,
        session_id=uuid.uuid4(),
        task_id=task.id,
        run_id=run.id,
        status="waiting_tool",
        error_code=int(ErrorCode.INTERNAL_ERROR),
        error_message="blocked",
    )
    finalize_outcome_on_task(task, outcome)
    assert run.status is RunStatus.FAILED  # 旧口径（blocked_by_trust 失败映射）不变
    assert task.status is TaskStatus.FAILED


# ── worker SLA 超时默认拒绝 ───────────────────────────────────────────────────


class FakeTaskRepo:
    def __init__(self) -> None:
        self.tasks: dict[uuid.UUID, Task] = {}
        self.events: dict[uuid.UUID, list[TaskEvent]] = {}
        self.projections: list[tuple[str, dict[str, Any]]] = []

    async def get(self, task_id: uuid.UUID) -> Task | None:
        return self.tasks.get(task_id)

    async def save(self, task: Task) -> None:
        self.tasks[task.id] = task

    async def append_event(self, task_id: uuid.UUID, event: TaskEvent, **_: Any) -> int:
        events = self.events.setdefault(task_id, [])
        event.seq = len(events)
        events.append(event)
        return event.seq

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
    def __init__(self, uow: FakeUow) -> None:
        self._uow = uow
        self.tasks = uow.task_repo
        self.sessions = uow.session_repo

    def enqueue_projection(self, kind: str, key: uuid.UUID, data: dict[str, Any]) -> None:
        self._uow.task_repo.projections.append((kind, data))

    async def __aenter__(self) -> FakeTx:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeUow:
    def __init__(self) -> None:
        self.task_repo = FakeTaskRepo()
        self.session_repo = FakeSessionRepo()

    def for_tenant(self, tenant_id: uuid.UUID) -> FakeTx:
        return FakeTx(self)


class CaptureOrchestrator:
    def __init__(self) -> None:
        self.commands: list[ChatCommand] = []

    def stream_chat(self, command: ChatCommand) -> AsyncIterator[ChatEvent]:
        return self._empty(command)

    async def _empty(self, command: ChatCommand) -> AsyncIterator[ChatEvent]:
        self.commands.append(command)
        return
        yield  # pragma: no cover


class StubPoller:
    """sweep 探测桩：find_approval_timeouts 返回预设 claim 并记录调用。"""

    def __init__(self, claims: list[WorkerClaim] | None = None) -> None:
        self.claims = claims or []
        self.calls: list[float] = []

    async def next_work(self) -> WorkerClaim | None:
        return None

    async def find_approval_timeouts(self, *, older_than_s: float, **_: Any) -> list[WorkerClaim]:
        self.calls.append(older_than_s)
        return list(self.claims)

    async def find_orphans(self, *, older_than_s: float, **_: Any) -> list[WorkerClaim]:
        return []


async def _suspended_task(
    *, with_anchor: bool = True, run_status: RunStatus = RunStatus.WAITING_TOOL
) -> tuple[FakeUow, Task, uuid.UUID]:
    """直构挂起形态聚合：run waiting_tool + approval_pending 锚点 + 投影行（pending/settled）。"""
    uow = FakeUow()
    task_id, run_id = uuid.uuid4(), uuid.uuid4()
    payload: dict[str, Any] = {"message_seq": 0}
    if with_anchor:
        payload[PENDING_KEY] = {
            "run_id": str(run_id),
            "step_seq": 1,
            "param_hash": "a" * 64,
            "action_iri": WRITE_ACTION_IRI,
            "execution_mode": "external_write",
            "waiting_since": _FIXED_NOW.isoformat(),
        }
    task = Task(
        id=task_id,
        tenant_id=_TENANT,
        type="chat",
        status=TaskStatus.RUNNING,
        session_id=uuid.uuid4(),
        active_run_id=run_id,
        payload=payload,
        runs=[Run(id=run_id, tenant_id=_TENANT, task_id=task_id, status=run_status)],
    )
    uow.task_repo.tasks[task.id] = task
    await uow.task_repo.append_event(
        task_id,
        TaskEvent(
            task_id=task_id,
            event_type="kernel.approval_pending",
            data={"run_id": str(run_id), "step_seq": 1, "param_hash": "a" * 64, "trace_id": "trace-t1"},
        ),
    )
    await uow.task_repo.append_event(
        task_id,
        TaskEvent(task_id=task_id, event_type="kernel.settled", data={"run_id": str(run_id), "status": "waiting_tool"}),
    )
    return uow, task, run_id


def _timeout_claim(task_id: uuid.UUID, run_id: uuid.UUID) -> WorkerClaim:
    return WorkerClaim(kind="approval_timeout", tenant_id=_TENANT, task_id=task_id, run_id=run_id, hang_s=1810.0)


async def test_worker_SLA超时默认拒绝_cancel形态_步闭合审计与RUN_ERROR() -> None:
    uow, task, run_id = await _suspended_task()
    sse: list[tuple[uuid.UUID | None, str, dict[str, Any]]] = []
    worker = TaskRunWorker(
        uow=uow,
        poller=StubPoller([_timeout_claim(task.id, run_id)]),
        orchestrator_provider=lambda: CaptureOrchestrator(),
        approval_suspend=True,
        approval_sla_s=1800.0,
        event_publisher=lambda sid, name, data: _capture(sse, sid, name, data),
    )
    recovered = await worker.sweep_once()

    assert recovered == 1
    run = task.runs[0]
    assert run.status is RunStatus.CANCELLED  # waiting_tool 唯一合法失败出口（reject 分支同型）
    assert run.error is not None and run.error["code"] == int(ErrorCode.SCOPE_INSUFFICIENT)  # B5 同码 2001
    assert run.error["retryable"] is False
    assert task.status is TaskStatus.FAILED
    assert PENDING_KEY not in task.payload  # 锚点消费（decide 同口径）
    events = uow.task_repo.events[task.id]
    types = [e.event_type for e in events]
    assert "kernel.approval_timeout" in types  # 审计行
    assert "kernel.interrupted" in types and "kernel.step_failed" in types  # 步 FAILED 合成闭合
    assert "run.error" in types  # RUN_ERROR 恢复终态（落库行）
    # 「先修复账本、后落终态」：步闭合行先于 run.error 行
    assert types.index("kernel.step_failed") < types.index("run.error")
    step_failed = next(e for e in events if e.event_type == "kernel.step_failed")
    assert step_failed.data["step_seq"] == 1 and step_failed.data["reason"] == "approval_timeout"
    assert any(name == "RUN_ERROR" for _, name, _ in sse)  # 事务提交后 SSE 推送


async def test_worker_SLA超时_工作流等待无锚点不误杀() -> None:
    uow, task, run_id = await _suspended_task(with_anchor=False)
    worker = TaskRunWorker(
        uow=uow,
        poller=StubPoller([_timeout_claim(task.id, run_id)]),
        orchestrator_provider=lambda: CaptureOrchestrator(),
        approval_suspend=True,
        approval_sla_s=1800.0,
    )
    assert await worker.sweep_once() == 0
    assert task.runs[0].status is RunStatus.WAITING_TOOL  # X16 工作流合法等待：不动
    assert task.status is TaskStatus.RUNNING


async def test_worker_开关关_不扫审批超时面() -> None:
    uow, task, run_id = await _suspended_task()
    poller = StubPoller([_timeout_claim(task.id, run_id)])
    worker = TaskRunWorker(
        uow=uow,
        poller=poller,
        orchestrator_provider=lambda: CaptureOrchestrator(),
        approval_suspend=False,
        approval_sla_s=1800.0,
    )
    assert await worker.sweep_once() == 0
    assert poller.calls == []  # 开关关：探测面不触（回退现状，无挂起态产生）
    assert task.runs[0].status is RunStatus.WAITING_TOOL


async def test_worker_SLA超时_已裁决态幂等跳过() -> None:
    uow, task, run_id = await _suspended_task(run_status=RunStatus.RUNNING)  # approve 已把 run 恢复 running
    worker = TaskRunWorker(
        uow=uow,
        poller=StubPoller([_timeout_claim(task.id, run_id)]),
        orchestrator_provider=lambda: CaptureOrchestrator(),
        approval_suspend=True,
        approval_sla_s=1800.0,
    )
    assert await worker.sweep_once() == 0
    assert task.runs[0].status is RunStatus.RUNNING
    assert task.status is TaskStatus.RUNNING


# ── 锚点投影（sessions ledger sink）──────────────────────────────────────────


async def test_锚点投影写入waiting_since_挂起时刻可追溯() -> None:
    class _SinkTx:
        def __init__(self, repo: FakeTaskRepo) -> None:
            self.tasks = repo

        async def __aenter__(self) -> _SinkTx:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    class _SinkUow:
        def __init__(self, repo: FakeTaskRepo) -> None:
            self.repo = repo

        def for_tenant(self, tenant_id: uuid.UUID) -> _SinkTx:
            return _SinkTx(self.repo)

    repo = FakeTaskRepo()
    task_id, run_id = uuid.uuid4(), uuid.uuid4()
    repo.tasks[task_id] = Task(  # sink 只投影已存在任务（get None 即跳过）
        id=task_id, tenant_id=_TENANT, type="chat", status=TaskStatus.RUNNING
    )
    occurred = datetime(2026, 10, 7, 8, 30, 0, tzinfo=UTC)
    sink = build_kernel_ledger_sink_factory(_SinkUow(repo))(task_id, run_id)  # type: ignore[arg-type]
    await sink(
        KernelEvent(
            event_type="kernel.approval_pending",
            tenant_id=_TENANT,
            run_id=run_id,
            trace_id="trace-anchor",
            data={"step_seq": 1, "param_hash": "a" * 64, "execution_mode": "external_write"},
            occurred_at=occurred,
        )
    )
    task = repo.tasks[task_id]
    anchor = task.payload["approval_pending"]
    assert anchor["run_id"] == str(run_id)
    assert anchor["waiting_since"] == occurred.isoformat()  # 挂起时刻（SLA 起算参照）


# ── approve→resume 全链（fake 票）────────────────────────────────────────────


async def test_approve到resume全链_核验链过waiting_tool_携票重放执行完成(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_suspend(monkeypatch, enabled=True)
    params = {"q": "高危写操作"}
    param_hash = canonical_param_hash(params)

    # ① 内核挂起：缺回执 → waiting_tool 结算 + pending 锚点（param_hash 即锚点键）
    write_tool, _, dispatcher = _suspend_plan(params)
    kernel = AgentKernel(dispatcher)
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == str(RunStatus.WAITING_TOOL)

    # ② 聚合回写：run 落 waiting_tool、task 保持 RUNNING
    uow = FakeUow()
    session_id, task_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session = Session(id=session_id, tenant_id=_TENANT, agent_id=uuid.uuid4(), user_id=_USER)
    uow.session_repo.sessions[session_id] = session
    uow.session_repo.messages[(session_id, 0)] = Message(
        session_id=session_id, seq=0, role="user", content="触发消息"
    )
    task = Task(
        id=task_id,
        tenant_id=_TENANT,
        type="chat",
        status=TaskStatus.RUNNING,
        session_id=session_id,
        active_run_id=run_id,
        payload={
            "message_seq": 0,
            PENDING_KEY: {
                "run_id": str(run_id),
                "step_seq": 1,
                "param_hash": param_hash,
                "action_iri": WRITE_ACTION_IRI,
                "execution_mode": "external_write",
                "waiting_since": _FIXED_NOW.isoformat(),
            },
        },
        runs=[Run(id=run_id, tenant_id=_TENANT, task_id=task_id, status=RunStatus.RUNNING)],
    )
    uow.task_repo.tasks[task_id] = task
    finalize_outcome_on_task(task, ChatOutcome(
        tenant_id=_TENANT, session_id=session_id, task_id=task_id, run_id=run_id, status="waiting_tool"
    ))
    assert task.runs[0].status is RunStatus.WAITING_TOOL

    # ③ 审批核验链（H-0b）：waiting_tool 检查通过 → 票仓 + run.resume_requested
    service = RunApprovalService(uow, now=lambda: _FIXED_NOW)
    result = await service.decide(
        tenant_id=_TENANT,
        approver_id=_USER,
        task_id=task_id,
        run_id=run_id,
        decision="approve",
        param_hash=param_hash,
    )
    assert result.run_status == "running"  # waiting_tool→running（resume 语义）
    assert PENDING_KEY not in task.payload
    ticket_rows = task.payload[TICKETS_KEY]
    assert len(ticket_rows) == 1 and ticket_rows[0]["param_hash"] == param_hash
    assert any(kind == "run.resume_requested" for kind, _ in uow.task_repo.projections)

    # ④ worker 携票重放：resume 认领命令携票（票仓移除防双消费）
    # 验票时钟与签票时钟同源（③ 用 _FIXED_NOW 签票，expires=_FIXED_NOW+TTL）：
    # _execute_resume 用模块级 _utcnow() 真实时钟验票，若不固定，真实时间越过
    # expires_at 后票被误判过期（B5 默认拒绝分支返回 True 但零重放）——日期敏感炸弹。
    monkeypatch.setattr("services.agent.business.task_worker._utcnow", lambda: _FIXED_NOW)
    capture = CaptureOrchestrator()
    worker = TaskRunWorker(
        uow=uow,
        poller=StubPoller(),
        orchestrator_provider=lambda: capture,
        approval_suspend=True,
        approval_sla_s=1800.0,
    )
    claim = WorkerClaim(kind="resume", tenant_id=_TENANT, task_id=task_id, run_id=run_id)
    assert await worker._execute_resume(claim) is True
    assert len(capture.commands) == 1
    command = capture.commands[0]
    resumed_tickets = tuple(command.approvals or ())
    assert len(resumed_tickets) == 1
    assert resumed_tickets[0].param_hash == param_hash
    assert task.payload[TICKETS_KEY] == []  # 票已消费

    # ⑤ 内核重放：带票执行 → 该步不再挂起，run completed（fake 票全链闭合）
    replay_tool = FakeTool(action_iri=WRITE_ACTION_IRI)
    replay_planner = FakePlanner(
        make_candidate(
            (make_step(seq=1, action_iri=WRITE_ACTION_IRI, mode=ExecutionMode.EXTERNAL_WRITE, params=params),)
        )
    )
    replay_kernel = AgentKernel(make_tool_dispatcher(replay_tool, register_planning_strategy=(replay_planner,)))
    replay_outcome = await replay_kernel.run(
        make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10), approvals=resumed_tickets
    )
    assert replay_outcome.status == str(RunStatus.COMPLETED)
    assert len(replay_tool.calls) == 1
    assert replay_outcome.terminal_states[0].status is StepStatus.VALIDATED

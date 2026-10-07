# tests/agent/test_worker_idempotency_key.py
"""C2 EXTERNAL_WRITE 幂等锚贯通测试（红队审查 docs/评审/红队攻击性审查-2026-10-06 §5，2026-10-07 修复批）。

红队 C2：「EXTERNAL_WRITE 步在重试时二次执行（付款×2），无副作用幂等记录」——工具实现侧
幂等=后续批；本批先保**键贯通可见**（attempt 维 key=task_id:attempt）：

- worker 重试监督链：重试 Run 认领命令携 key（attempt 维度），run.idempotency_key 审计行落账；
- worker 审批 resume 重放：同 attempt 同键（与首过写动作同锚），审计行不重复落；
- 内核注入：EXTERNAL_WRITE 步参数注入 idempotency_key **先于 param_hash**——审批工单
  （param_hash 绑定）与工具调用参数天然同键（「工单+工具调用都带」）；
- 负向：工单按**未注入**参数签发而内核注入键 → 哈希不匹配 → B5 默认拒绝（键受工单约束）。
桩：内存 FakeUow + 捕获型编排器 + FakeTool/FakePlanner 内核直跑（零真网零真库）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from services.agent.business.chat_events import ChatCommand, ChatEvent
from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.gate_baseline import canonical_param_hash
from services.agent.business.kernel.loop import AgentKernel
from services.agent.business.task_worker import TaskRunWorker
from services.agent.data.repo_impl.task_poller import WorkerClaim
from services.agent.domain.model.kernel_actions import ApprovalTicket, ExecutionMode
from services.agent.domain.model.session import Message, Session
from services.agent.domain.model.task import RunRetryPolicy, RunStatus, Task, TaskEvent
from tests.agent.conftest import (
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
_MSG = "触发消息"


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


class CaptureOrchestrator:
    def __init__(self) -> None:
        self.commands: list[ChatCommand] = []

    def stream_chat(self, command: ChatCommand) -> AsyncIterator[ChatEvent]:
        return self._empty(command)

    async def _empty(self, command: ChatCommand) -> AsyncIterator[ChatEvent]:
        self.commands.append(command)
        return
        yield  # pragma: no cover


class QueuedPoller:
    def __init__(self, claim: WorkerClaim) -> None:
        self.claim = claim

    async def next_work(self) -> WorkerClaim:
        claim, self.claim = self.claim, None
        return claim


def _claim_env(*, attempt: int) -> tuple[TaskRunWorker, FakeUow, CaptureOrchestrator, Task, Any]:
    """重试 Run 认领形态：session+消息+任务（attempt=N，活跃 Run queued）。"""
    session = Session(id=uuid.uuid4(), tenant_id=_TENANT, agent_id=uuid.uuid4(), user_id=uuid.uuid4())
    uow = FakeUow(session)
    uow.session_repo.messages[(session.id, 1)] = Message(session_id=session.id, seq=1, role="user", content=_MSG)
    task = Task(tenant_id=_TENANT, type="chat", session_id=session.id, payload={"message_seq": 1})
    run = task.start_run()
    run.start()  # queued→running（认领形态；04 §3 状态机 running→failed 才合法）
    for _ in range(attempt - 1):  # attempt=N：start_run 已计 1，逐次失败+聚合重试补足
        run.fail({"code": 5001, "message": "首跑失败（retryable）", "retryable": True})
        run = task.start_retry_run()  # 聚合断言：attempt≤3；新 Run queued 交 worker 认领
    uow.task_repo.tasks[task.id] = task
    orchestrator = CaptureOrchestrator()
    worker = TaskRunWorker(
        uow=uow,
        poller=QueuedPoller(WorkerClaim(kind="queued", tenant_id=_TENANT, task_id=task.id, run_id=run.id)),
        orchestrator_provider=lambda: orchestrator,
        policy=RunRetryPolicy(base_seconds=0.001, cap_seconds=0.002, jitter_ratio=0.0),
        orphan_sweep_interval_s=30.0,
        orphan_running_timeout_s=300.0,
    )
    return worker, uow, orchestrator, task, run


def _idempotency_events(uow: FakeUow, task_id: uuid.UUID) -> list[TaskEvent]:
    return [e for e in uow.task_repo.events.get(task_id, []) if e.event_type == "run.idempotency_key"]


async def test_重试Run认领_命令携attempt维幂等键_审计行落账():
    """红队 C2 主断言：attempt=2 重试 Run 认领 → key=task_id:2 随命令下发 + 审计留痕。"""
    # Arrange：首跑 failed 的 attempt=2 任务（活跃 Run queued 交 worker 认领）
    worker, uow, orch, task, run = _claim_env(attempt=2)
    # Act：认领执行
    assert await worker.poll_once() is True
    # Assert ①：命令携 attempt 维幂等键
    command = orch.commands[0]
    assert command.idempotency_key == f"{task.id}:2"
    # Assert ②：审计事件 run.idempotency_key 落账（run/键/attempt 三元可归因）
    events = _idempotency_events(uow, task.id)
    assert len(events) == 1
    assert events[0].data["idempotency_key"] == f"{task.id}:2"
    assert events[0].data["run_id"] == str(run.id)
    assert events[0].data["attempt"] == 2


async def test_审批resume重放_同attempt同键_审计行不重复():
    # Arrange：running Run（attempt=1，等待审批后重放形态）
    session = Session(id=uuid.uuid4(), tenant_id=_TENANT, agent_id=uuid.uuid4(), user_id=uuid.uuid4())
    uow = FakeUow(session)
    uow.session_repo.messages[(session.id, 1)] = Message(session_id=session.id, seq=1, role="user", content=_MSG)
    task = Task(tenant_id=_TENANT, type="chat", session_id=session.id, payload={"message_seq": 1})
    run = task.start_run()
    run._status = RunStatus.RUNNING  # waiting_tool 承载态
    uow.task_repo.tasks[task.id] = task
    # Act ①：认领（审计行在此落账）
    claim_worker = TaskRunWorker(
        uow=uow,
        poller=QueuedPoller(WorkerClaim(kind="queued", tenant_id=_TENANT, task_id=task.id, run_id=run.id)),
        orchestrator_provider=lambda: CaptureOrchestrator(),
        policy=RunRetryPolicy(base_seconds=0.001, cap_seconds=0.002, jitter_ratio=0.0),
        orphan_sweep_interval_s=30.0,
        orphan_running_timeout_s=300.0,
    )
    assert await claim_worker.poll_once() is True
    # Act ②：审批回执 resume 重放（同 Run 同 attempt）
    task.payload = {
        **(task.payload or {}),
        "approvals": [{"ticket_id": str(uuid.uuid4()), "run_id": str(run.id), "param_hash": "h", "approved_by": None}],
    }
    orch2 = CaptureOrchestrator()
    resume_worker = TaskRunWorker(
        uow=uow,
        poller=QueuedPoller(WorkerClaim(kind="resume", tenant_id=_TENANT, task_id=task.id, run_id=run.id)),
        orchestrator_provider=lambda: orch2,
        policy=RunRetryPolicy(base_seconds=0.001, cap_seconds=0.002, jitter_ratio=0.0),
        orphan_sweep_interval_s=30.0,
        orphan_running_timeout_s=300.0,
    )
    assert await resume_worker.poll_once() is True
    # Assert：resume 命令同键（task:1）；审计行仍只有认领时一条（不重复落）
    assert orch2.commands[0].idempotency_key == f"{task.id}:1"
    assert len(_idempotency_events(uow, task.id)) == 1


async def test_内核写动作注入幂等键_工单与工具调用同键():
    """键贯通终点：EXTERNAL_WRITE 工具调用参数带 idempotency_key，且工单（按注入后参数签发）
    通过 B5 校验——「工单+工具调用都带（同键）」。"""
    # Arrange：写步 + 按注入后参数签发的工单（键=task:1）
    params = {"q": "付款指令"}
    key = "task-1:1"
    ticket = ApprovalTicket(param_hash=canonical_param_hash({**params, "idempotency_key": key}))
    tool = FakeTool(action_iri=WRITE_ACTION_IRI)
    planner = FakePlanner(
        make_candidate(
            (make_step(seq=1, action_iri=WRITE_ACTION_IRI, mode=ExecutionMode.EXTERNAL_WRITE, params=params),)
        )
    )
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))
    # Act：携键执行（ChatCommand→kernel.run 同形：worker 注入路径的内核直跑等价面）
    outcome = await kernel.run(
        make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10), approvals=(ticket,), idempotency_key=key
    )
    # Assert ①：执行完成（工单按注入后参数签发 → B5 通过）
    assert outcome.status == str(RunStatus.COMPLETED)
    # Assert ②：工具调用参数带同键（下游工具实现侧幂等消费的接键面）
    assert len(tool.calls) == 1
    assert tool.calls[0].parameters["idempotency_key"] == key
    assert tool.calls[0].param_hash == ticket.param_hash


async def test_工单按未注入参数签发_内核注入键后哈希失配_B5默认拒绝():
    """负向：键进参数即进 param_hash——拿「未含键」的旧工单重放被拒（键受工单约束，
    防「工单批的是 A 参数、工具实际执行带键的 B 参数」的绑定旁路）。"""
    # Arrange：工单按原始参数签发（不含键）
    params = {"q": "付款指令"}
    tool = FakeTool(action_iri=WRITE_ACTION_IRI)
    planner = FakePlanner(
        make_candidate(
            (make_step(seq=1, action_iri=WRITE_ACTION_IRI, mode=ExecutionMode.EXTERNAL_WRITE, params=params),)
        )
    )
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))
    stale_ticket = ApprovalTicket(param_hash=canonical_param_hash(params))
    # Act：携键执行（内核注入键 → 实际 param_hash ≠ 工单哈希）
    outcome = await kernel.run(
        make_task(),
        make_ctx(),
        budget=Budget(max_steps=5, duration_s=10),
        approvals=(stale_ticket,),
        idempotency_key="task-1:1",
    )
    # Assert：B5 默认拒绝、工具零执行（工具实现侧可安全依赖「调用必经有效工单」不变式）
    assert outcome.status == str(RunStatus.FAILED)
    assert tool.calls == []

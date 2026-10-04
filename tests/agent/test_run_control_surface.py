# tests/agent/test_run_control_surface.py
"""M4.5-A 运行中输入面「面」级验收：运行注册表 + estop 生效点①（worker 前检）+ API（docs/Agent/12 §1.2/§1.4）。

覆盖：
- RunRegistry：注册/查询/终态注销（显式 remove 防泄漏）、重复注册拒绝；
- worker estop 前检（生效点①）：激活→拒新 Run（4104，queued→cancelled + task 终局 +
  run.estop_rejected 审计行）、重试 spawn 同拒；DELETE 后恢复执行；
- inbox 端点：会话归属校验（404/活跃 Run 绑定）、注册表未命中 4105、受理 202+seq、
  容量超限 429+4203、INBOX_SPLICED 回执发布；
- admin estop 三端点：激活/状态/解除信封与幂等。
桩：内存 FakeUow + 注册表/收件箱直构 + 桩 Request（app.state），不依赖 PG——
PG 路径由 tests/gateway 集成批覆盖。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from services.agent.api.control import (
    activate_estop,
    deactivate_estop,
    get_estop_state,
    get_or_build_estop_store,
    get_or_build_run_registry,
    submit_run_inbox,
)
from services.agent.api.schemas.control import EStopActivateIn, InboxSubmitIn
from services.agent.business.chat_events import ChatCommand, ChatEvent
from services.agent.business.kernel.inbox import KernelInbox
from services.agent.business.run_registry import RunRegistry
from services.agent.business.task_worker import TaskRunWorker
from services.agent.data.repo_impl.task_poller import WorkerClaim
from services.agent.domain.model.session import Message, Session
from services.agent.domain.model.task import RunRetryPolicy, RunStatus, Task, TaskEvent, TaskStatus
from services.platform.deps import Principal
from services.platform.errors import ErrorCode, GatewayError
from services.platform.ports.estop import build_estop_store
from tests.agent.conftest import make_candidate

_TENANT = uuid.uuid4()
_USER = uuid.uuid4()
_MSG = "分析昨夜城东线路停电原因"


# ── 桩（test_resumable_continuation / test_run_approval_api 同风格内存 UoW）──────
class FakeSessionRepo:
    def __init__(self) -> None:
        self.sessions: dict[uuid.UUID, Session] = {}
        self.messages: dict[tuple[uuid.UUID, int], Message] = {}

    async def get(self, session_id: uuid.UUID) -> Session | None:
        return self.sessions.get(session_id)

    async def get_message_by_seq(self, session_id: uuid.UUID, seq: int) -> Message | None:
        return self.messages.get((session_id, seq))


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

    async def find_running_by_session(self, session_id: uuid.UUID) -> Task | None:
        """端点归属校验消费面（api/control）；返回 session 的 running task（不含 runs 明细）。"""
        return next(
            (t for t in self.tasks.values() if t.session_id == session_id and t.status is TaskStatus.RUNNING), None
        )


class FakeUow:
    def __init__(self) -> None:
        self.task_repo = FakeTaskRepo()
        self.session_repo = FakeSessionRepo()

    def for_tenant(self, tenant_id: uuid.UUID) -> FakeTx:
        return FakeTx(self)


class FakeTx:
    def __init__(self, uow: FakeUow) -> None:
        self.tasks = uow.task_repo
        self.sessions = uow.session_repo

    async def __aenter__(self) -> FakeTx:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _principal(*scopes: str) -> Principal:
    return Principal(
        {
            "sub": str(_USER),
            "tenant_id": str(_TENANT),
            "roles": ["member"],
            "scopes": list(scopes or ("session:chat", "session:read", "session:write", "admin:read", "admin:write")),
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


class _RunningTaskEnv:
    """queued Run 形态构造：session + task(running) + 活跃 Run(queued)。"""

    def __init__(self, uow: FakeUow) -> None:
        session = Session(id=uuid.uuid4(), tenant_id=_TENANT, agent_id=uuid.uuid4(), user_id=_USER)
        uow.session_repo.sessions[session.id] = session
        uow.session_repo.messages[(session.id, 1)] = Message(session_id=session.id, seq=1, role="user", content=_MSG)
        self.session = session
        task = Task(tenant_id=_TENANT, type="chat", session_id=session.id, payload={"message_seq": 1})
        self.run = task.start_run()  # queued + task running
        uow.task_repo.tasks[task.id] = task
        self.task = task


def _request(state: Any) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _state(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {"settings": SimpleNamespace(), "redis_client": None}
    base.update(kw)
    return SimpleNamespace(**base)


# ── RunRegistry ──────────────────────────────────────────────────────────────────
async def test_run_registry_注册查询终态注销_重复注册拒绝():
    # Arrange
    registry = RunRegistry()
    inbox = KernelInbox()
    run_id = uuid.uuid4()
    # Act / Assert：注册可查；未注册 4105 语义（None）
    registry.register(run_id, inbox=inbox, control_probe=lambda: None)
    assert registry.get(run_id) is not None and registry.get(run_id).inbox is inbox
    assert registry.get(uuid.uuid4()) is None
    # 终态注销（显式 remove）：注销后即查无，重复注销幂等
    assert registry.unregister(run_id) is not None
    assert registry.get(run_id) is None
    assert registry.unregister(run_id) is None
    # 重复注册=组合根违例（防条目互踩泄漏）
    registry.register(run_id, inbox=inbox)
    with pytest.raises(RuntimeError):
        registry.register(run_id, inbox=KernelInbox())


# ── worker estop 生效点①（§1.2）─────────────────────────────────────────────────
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
    def __init__(self, claim: WorkerClaim | None) -> None:
        self.claim = claim

    async def next_work(self) -> WorkerClaim | None:
        claim, self.claim = self.claim, None
        return claim


def _worker(
    uow: FakeUow, store: Any, orchestrator: CaptureOrchestrator, task_id: uuid.UUID, run_id: uuid.UUID
) -> TaskRunWorker:
    claim = WorkerClaim(kind="queued", tenant_id=_TENANT, task_id=task_id, run_id=run_id)
    poller = QueuedPoller(claim)
    return TaskRunWorker(
        uow=uow,
        poller=poller,
        orchestrator_provider=lambda: orchestrator,
        policy=RunRetryPolicy(base_seconds=0.001, cap_seconds=0.002, jitter_ratio=0.0),
        orphan_sweep_interval_s=30.0,
        orphan_running_timeout_s=300.0,
        estop_store=store,
    )


async def test_worker_estop激活_拒新Run_4104_解除后恢复执行():
    # Arrange：queued Run + estop 激活
    uow = FakeUow()
    env = _RunningTaskEnv(uow)
    store = build_estop_store(None)
    await store.activate(_TENANT, reason="演练停机", by=_USER)
    orchestrator = CaptureOrchestrator()
    worker = _worker(uow, store, orchestrator, env.task.id, env.run.id)
    # Act：认领（estop 前检命中）
    assert await worker.poll_once() is True
    # Assert ①：queued→cancelled + 4104 结构化 error；task 终局 failed；审计行 run.estop_rejected
    run = next(r for r in uow.task_repo.tasks[env.task.id].runs if r.id == env.run.id)
    assert run.status is RunStatus.CANCELLED
    assert run.error["code"] == int(ErrorCode.ESTOP_ACTIVE) and "演练停机" in run.error["message"]
    assert run.error["retryable"] is False
    assert uow.task_repo.tasks[env.task.id].status is TaskStatus.FAILED
    rejected = [e for e in uow.task_repo.events[env.task.id] if e.event_type == "run.estop_rejected"]
    assert len(rejected) == 1 and rejected[0].data["code"] == int(ErrorCode.ESTOP_ACTIVE)
    assert orchestrator.commands == []  # 编排器未被触达（拒新工作）
    # Act ②：DELETE 后恢复——重建 queued Run 再认领，正常进入编排器
    await store.deactivate(_TENANT)
    task2 = Task(tenant_id=_TENANT, type="chat", session_id=env.session.id, payload={"message_seq": 1})
    run2 = task2.start_run()
    uow.task_repo.tasks[task2.id] = task2
    worker2 = _worker(uow, store, orchestrator, task2.id, run2.id)
    assert await worker2.poll_once() is True
    # Assert ②：恢复执行（编排器收到命令；run 无 4104）
    assert len(orchestrator.commands) == 1 and orchestrator.commands[0].run_id == run2.id
    assert uow.task_repo.tasks[task2.id].status is TaskStatus.RUNNING


async def test_worker_未装配estop_store_零行为变化():
    # Arrange：estop_store=None（直跑/旧形态）
    uow = FakeUow()
    env = _RunningTaskEnv(uow)
    orchestrator = CaptureOrchestrator()
    worker = _worker(uow, None, orchestrator, env.task.id, env.run.id)
    # Act / Assert：认领执行不受影响
    assert await worker.poll_once() is True
    assert len(orchestrator.commands) == 1


async def test_worker_重放携resume对账锚点_零新增消息行():
    """P-4 接线（§1.3）：重试重放命令携 resumed_validated 三元组；跳过不产生任何消息行
    （重放复用原触发消息，messages 只追加不变式不破——会话历史不变式）。"""
    # Arrange：首 Run failed（validated 投影含 param_hash）→ 重试 Run queued（attempt=2）
    uow = FakeUow()
    session = Session(id=uuid.uuid4(), tenant_id=_TENANT, agent_id=uuid.uuid4(), user_id=_USER)
    uow.session_repo.sessions[session.id] = session
    uow.session_repo.messages[(session.id, 1)] = Message(session_id=session.id, seq=1, role="user", content=_MSG)
    task = Task(tenant_id=_TENANT, type="chat", session_id=session.id, payload={"message_seq": 1})
    first = task.start_run()
    first._transition(RunStatus.RUNNING)
    first.fail({"code": 5001, "message": "模型超时", "retryable": True})
    retry = task.start_retry_run()
    uow.task_repo.tasks[task.id] = task
    _hash = "b" * 64
    uow.task_repo.events[task.id] = [
        TaskEvent(
            task_id=task.id,
            event_type="kernel.step_validated",
            data={
                "run_id": str(first.id),
                "step_seq": 1,
                "action_iri": "http://ontology.example/action/read_data",
                "param_hash": _hash,
            },
        )
    ]
    orchestrator = CaptureOrchestrator()
    worker = _worker(uow, None, orchestrator, task.id, retry.id)
    # Act：认领重试（重放）
    assert await worker.poll_once() is True
    # Assert ①：命令携对账锚点三元组（seq 键归一）+ continuation 注记保留（注记与对账互补）
    command = orchestrator.commands[0]
    assert command.resumed_validated == (
        {"seq": 1, "action_iri": "http://ontology.example/action/read_data", "param_hash": _hash},
    )
    assert "[系统注记｜对账续跑]" in command.message
    # Assert ②：零新增消息行——messages 仍只有原触发消息一条（内核跳过不产生消息行）
    assert len(uow.session_repo.messages) == 1
    # Assert ③：payload 锚点摘要保留（可观测面，H-0c ② 既有行为不回退）
    stored = uow.task_repo.tasks[task.id]
    assert stored.payload["resumable_anchors"]["steps"] == [
        {"step_seq": 1, "action_iri": "http://ontology.example/action/read_data", "param_hash": _hash}
    ]


# ── inbox 端点（§1.4；直调+桩 Request）───────────────────────────────────────────
class FakeHub:
    def __init__(self) -> None:
        self.published: list[tuple[uuid.UUID, str, dict]] = []

    def publish(self, session_id: uuid.UUID, name: str, data: dict) -> tuple[int, bytes]:
        self.published.append((session_id, name, data))
        return len(self.published), b"frame"


def _inbox_env(*, with_run: bool = True, registry: RunRegistry | None = None, inbox: KernelInbox | None = None):
    uow = FakeUow()
    env = _RunningTaskEnv(uow)
    hub = FakeHub()
    reg = registry if registry is not None else get_or_build_run_registry(_state())
    box = inbox if inbox is not None else KernelInbox()
    if with_run:
        reg.register(env.run.id, inbox=box)
    state = _state(run_registry=reg, sse_hub=hub)
    return uow, env, hub, reg, box, state


async def test_inbox端点_受理202回执seq_并发布INBOX_SPLICED():
    # Arrange：session+活跃 Run+注册表命中
    uow, env, hub, reg, box, state = _inbox_env()
    # Act
    resp = await submit_run_inbox(
        env.session.id,
        env.run.id,
        InboxSubmitIn(kind="steer", text="优先备用线路"),
        principal=_principal(),
        uow=uow,
        request=_request(state),
    )
    # Assert：202 信封 + seq 回执 + 三通道入箱 + INBOX_SPLICED 发布（run_id/kind/text 载荷）
    assert set(resp) == {"data", "meta"}
    assert resp["data"]["status"] == "accepted" and resp["data"]["seq"] == 1
    assert box.pending_count == 1
    assert len(hub.published) == 1
    sid, name, data = hub.published[0]
    assert (sid, name) == (env.session.id, "INBOX_SPLICED")
    assert data["run_id"] == str(env.run.id) and data["text"] == "优先备用线路" and data["kind"] == "steer"


async def test_inbox端点_会话不存在404_活跃Run不符4105():
    # Arrange
    uow, env, _, _, _, state = _inbox_env()
    principal = _principal()
    # Act / Assert ①：会话不存在 → 404（归属校验前置）
    with pytest.raises(GatewayError) as ei:
        await submit_run_inbox(
            uuid.uuid4(),
            env.run.id,
            InboxSubmitIn(kind="steer", text="x"),
            principal=principal,
            uow=uow,
            request=_request(state),
        )
    assert (ei.value.code, ei.value.status_code) == (404, 404)
    # Act / Assert ②：会话在、但 run 与该会话活跃 Run 不符 → 4105 RUN_NOT_LOCAL
    other_run = uuid.uuid4()
    with pytest.raises(GatewayError) as ei2:
        await submit_run_inbox(
            env.session.id,
            other_run,
            InboxSubmitIn(kind="steer", text="x"),
            principal=principal,
            uow=uow,
            request=_request(state),
        )
    assert (ei2.value.code, ei2.value.status_code) == (int(ErrorCode.RUN_NOT_LOCAL), 409)
    assert "RUN_NOT_LOCAL" in ei2.value.message


async def test_inbox端点_注册表未命中4105_终态注销后同码():
    # Arrange：活跃 Run 绑定成立，但注册表无此 run（他副本执行/终态已注销）
    uow, env, _, reg, _, state = _inbox_env(with_run=False)
    assert reg.get(env.run.id) is None
    # Act / Assert
    with pytest.raises(GatewayError) as ei:
        await submit_run_inbox(
            env.session.id,
            env.run.id,
            InboxSubmitIn(kind="inject", text="x"),
            principal=_principal(),
            uow=uow,
            request=_request(state),
        )
    assert (ei.value.code, ei.value.status_code) == (int(ErrorCode.RUN_NOT_LOCAL), 409)


async def test_inbox端点_容量超限429_4203():
    # Arrange：容量 1 的收件箱（占满）
    uow, env, _, _, box, state = _inbox_env(inbox=KernelInbox(max_per_run=1))
    box.submit("steer", "占位", source="user-1")
    # Act / Assert：4203 → HTTP 429（结构化拒绝经端点映射）
    with pytest.raises(GatewayError) as ei:
        await submit_run_inbox(
            env.session.id,
            env.run.id,
            InboxSubmitIn(kind="steer", text="超限"),
            principal=_principal(),
            uow=uow,
            request=_request(state),
        )
    assert (ei.value.code, ei.value.status_code) == (int(ErrorCode.INBOX_CAPACITY), 429)


# ── admin estop 三端点（§1.2）────────────────────────────────────────────────────
async def test_estop三端点_激活状态解除_幂等():
    # Arrange：内存兜底 store（Redis 形态由 estop store 专项用例覆盖）
    state = _state()
    get_or_build_estop_store(state)
    principal = _principal()
    # Act ①：激活
    resp = await activate_estop(EStopActivateIn(reason="联调环境停机"), principal=principal, request=_request(state))
    # Assert ①：202 信封 + 状态视图 active=true
    assert resp["data"]["active"] is True and resp["data"]["reason"] == "联调环境停机"
    view = await get_estop_state(principal=principal, request=_request(state))
    assert view["data"]["active"] is True and view["data"]["by"] == str(_USER)
    # Act ②：覆盖写幂等（二次激活以最后一次为准）
    await activate_estop(EStopActivateIn(reason="二次激活"), principal=principal, request=_request(state))
    view2 = await get_estop_state(principal=principal, request=_request(state))
    assert view2["data"]["reason"] == "二次激活"
    # Act ③：解除 → 204（无体）+ 视图 active=false；重复解除幂等
    await deactivate_estop(principal=principal, request=_request(state))
    await deactivate_estop(principal=principal, request=_request(state))
    view3 = await get_estop_state(principal=principal, request=_request(state))
    assert view3["data"]["active"] is False and view3["data"]["reason"] is None


async def test_estop激活后_内核control_gate探针同步命中():
    # Arrange：端点激活 → 同一 store 的探针（内核步边界闸门）应同步命中
    state = _state()
    store = get_or_build_estop_store(state)
    tenant = uuid.uuid4()
    admin = Principal(
        {
            "sub": str(_USER),
            "tenant_id": str(tenant),
            "roles": ["admin"],
            "scopes": ["admin:write"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )
    # Act
    await activate_estop(EStopActivateIn(reason="租户级停机"), principal=admin, request=_request(state))
    # Assert：闸门探针（同步面）命中；active_reason（worker 前检异步面）一致
    assert store.probe(tenant)() == "租户级停机"
    assert await store.active_reason(tenant) == "租户级停机"
    # 其他租户不受影响（租户隔离）
    assert store.probe(uuid.uuid4())() is None


# ── 编排器 ↔ 注册表契约（spawn 注册/终态注销）────────────────────────────────────
class _StubAssembler:
    """编排器装配桩（test_chat_orchestrator Fake 族最小形态）：零记忆零检索。"""

    async def append_window_message(self, *a: Any, **k: Any) -> None:
        return None

    async def assemble(self, **k: Any) -> Any:
        return SimpleNamespace(memory=None, chunks=[], graph_paths=[], degraded=False, citations=[], context_text="")


class _StubAdapter:
    """适配器桩：可注入计划候选 / 规划故障（终态注销两路径的驱动器）。"""

    def __init__(self, candidate: Any = None, *, planner_error: Exception | None = None) -> None:
        from services.agent.domain.model.kernel_context import ExtensionMeta

        self.meta = ExtensionMeta(
            name="fixture.stub_adapter", version="1.0.0", semantic_annotation={"concept_iri": "http://o/概念/桩"}
        )
        self._candidate = candidate
        self._planner_error = planner_error

    def turn_planner(self, turn: Any) -> Any:
        from tests.agent.conftest import FakePlanner

        if self._planner_error is not None:
            planner = FakePlanner(make_candidate(()))
            planner.plan = self._boom  # type: ignore[method-assign]
            return planner
        return FakePlanner(self._candidate or make_candidate(()))

    async def _boom(self, *a: Any, **k: Any) -> Any:
        raise self._planner_error  # type: ignore[misc]

    def turn_context_provider(self, turn: Any) -> Any:
        adapter = self

        class _Provider:
            meta = adapter.meta  # 注册面元数据（grounding 装载取材）

            async def provide(self, task: Any, step: Any, ctx: Any, *, budget_tokens: int, timeout_ms: int = 3_000):
                from services.agent.domain.model.kernel_context import ContextBlock

                return ContextBlock(source="fixture.stub", content="", tokens=0)

        return _Provider()

    def turn_tool(self, turn: Any, box: Any, on_event: Any) -> Any:
        adapter = self

        class _Tool:
            meta = adapter.meta  # 注册面元数据（空计划不触达分发，注册即可）

        return _Tool()


async def test_编排器spawn注册_inbox与estop探针_终态注销防泄漏():
    from services.agent.business.chat_events import ChatCommand
    from services.agent.business.chat_orchestrator import ChatOrchestrator

    # Arrange：编排器接注册表+estop 探针工厂；空计划（直线完成）
    registry = RunRegistry()
    store = build_estop_store(None)
    orchestrator = ChatOrchestrator(
        adapters={"builtin": _StubAdapter()},
        assembler=_StubAssembler(),  # type: ignore[arg-type]
        run_registry=registry,
        estop_probe_factory=store.probe,
    )
    run_id = uuid.uuid4()
    command = ChatCommand(
        tenant_id=_TENANT,
        user_id=_USER,
        session_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        run_id=run_id,
        message="x",
        trace_id="t-registry",
    )
    # Act：跑一轮
    _ = [e async for e in orchestrator.stream_chat(command)]
    # Assert ①：终态注销（正常完成路径）——注册表无泄漏条目
    assert registry.get(run_id) is None and len(registry) == 0
    # Act ②：规划故障路径（异常收敛）→ 同样注销
    run_id2 = uuid.uuid4()
    failing = ChatOrchestrator(
        adapters={"builtin": _StubAdapter(planner_error=RuntimeError("规划炸了"))},
        assembler=_StubAssembler(),  # type: ignore[arg-type]
        run_registry=registry,
        estop_probe_factory=store.probe,
    )
    command2 = ChatCommand(
        tenant_id=_TENANT,
        user_id=_USER,
        session_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        run_id=run_id2,
        message="x",
        trace_id="t-registry-2",
    )
    _ = [e async for e in failing.stream_chat(command2)]
    # Assert ②：异常路径同样零泄漏
    assert registry.get(run_id2) is None and len(registry) == 0

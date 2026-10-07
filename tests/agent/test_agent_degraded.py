# tests/agent/test_agent_degraded.py
"""适配器 degraded 态测试（H-0c ③，2026-09-29 批；04 篇 §10 裁决代码化）。

- 计数迁移：探活失败 +1、成功清零；连续失败 ≥阈值 → degrade()（enabled→degraded）；
  成功自 degraded 自愈回 enabled；disabled 终态语义不变；
- 拒绑：DEGRADED 不得被新会话引用（AgentError→409，sessions 端点既有映射）；
- 存量放行：worker 认领执行路径不校验 agent 状态（degraded 存量 Run 跑完不中断）；
- 服务函数 record_adapter_health_outcome：仓储往返（api/ 端点接线遗留，本批冻结面）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from services.agent.business.agent_health import record_adapter_health_outcome
from services.agent.business.chat_events import ChatCommand, ChatEvent
from services.agent.business.task_worker import TaskRunWorker
from services.agent.data.repo_impl.task_poller import WorkerClaim
from services.agent.domain.model.agent import Agent, AgentError, AgentStatus
from services.agent.domain.model.session import Message, Session
from services.agent.domain.model.task import RunRetryPolicy, Task, TaskEvent

_TENANT = uuid.uuid4()


def make_agent(**kw: Any) -> Agent:
    base: dict[str, Any] = {
        "tenant_id": _TENANT,
        "name": "停电分析助手",
        "agent_tool": "builtin",
        "adapter_id": uuid.uuid4(),
    }
    base.update(kw)
    return Agent.register(**base)


def make_agent_at(status: AgentStatus) -> Agent:
    """指定状态构造（register 只产 enabled；非 enabled 经聚合迁移口到位）。"""
    agent = make_agent()
    agent.status = status  # validate_assignment 允许枚举直置（桩预置，非业务迁移）
    return agent


# ── 计数迁移（聚合方法）─────────────────────────────────────────────────


def test_连续探活失败计数_达阈值迁移degraded():
    agent = make_agent()
    for i in (1, 2):
        agent.record_adapter_health(healthy=False, degrade_threshold=3)
        assert agent.adapter_failure_count == i and agent.status is AgentStatus.ENABLED
    agent.record_adapter_health(healthy=False, degrade_threshold=3)  # 第 3 次：达阈值
    assert agent.status is AgentStatus.DEGRADED and agent.adapter_failure_count == 3


def test_探活成功清零计数_degraded自愈回enabled():
    agent = make_agent()
    for _ in range(3):
        agent.record_adapter_health(healthy=False, degrade_threshold=3)
    assert agent.status is AgentStatus.DEGRADED
    agent.record_adapter_health(healthy=True)  # 成功：清零 + 自愈（04 §3 degraded→recovered）
    assert agent.adapter_failure_count == 0 and agent.status is AgentStatus.ENABLED


def test_阈值可配_中途成功打断连续计数():
    agent = make_agent()
    agent.record_adapter_health(healthy=False, degrade_threshold=2)
    agent.record_adapter_health(healthy=True)  # 清零打断
    agent.record_adapter_health(healthy=False, degrade_threshold=2)  # 重新从 1 起算
    assert agent.status is AgentStatus.ENABLED and agent.adapter_failure_count == 1


def test_degraded后继续失败不重复迁移_disabled不受探活影响():
    degraded = make_agent_at(AgentStatus.DEGRADED)
    degraded.record_adapter_health(healthy=False, degrade_threshold=3)  # 计数续涨、状态不动
    assert degraded.status is AgentStatus.DEGRADED and degraded.adapter_failure_count == 1
    disabled = make_agent_at(AgentStatus.DISABLED)
    for _ in range(5):
        disabled.record_adapter_health(healthy=False)  # 终态不被探活结果迁移
        disabled.record_adapter_health(healthy=True)
    assert disabled.status is AgentStatus.DISABLED and disabled.adapter_failure_count == 0


# ── 状态机（enabled⇄degraded 双向；disabled 终态语义不变）────────────────


def test_迁移合法性_双向与终态():
    agent = make_agent()
    agent.degrade()
    assert agent.status is AgentStatus.DEGRADED
    agent.enable()  # 自愈通道（degraded→enabled）
    assert agent.status is AgentStatus.ENABLED
    agent.degrade()
    agent.disable()  # degraded→disabled：停用仍可达（终态语义不变）
    assert agent.status is AgentStatus.DISABLED
    with pytest.raises(AgentError, match="非法状态迁移"):
        agent.degrade()  # disabled→degraded 非法（终态仅 enable() 出口）
    agent.enable()  # disabled→enabled 既有语义不变
    assert agent.status is AgentStatus.ENABLED


# ── 拒绑（ensure_usable_for_new_session：新会话闸门）──────────────────────


def test_degraded拒绑_enabled放行_disabled语义不变():
    ok = make_agent()
    ok.ensure_usable_for_new_session()  # enabled：放行
    degraded = make_agent_at(AgentStatus.DEGRADED)
    with pytest.raises(AgentError, match="AGENT_DEGRADED"):
        degraded.ensure_usable_for_new_session()  # sessions 端点既有映射：AgentError→409
    disabled = make_agent_at(AgentStatus.DISABLED)
    with pytest.raises(AgentError, match="AGENT_DISABLED"):
        disabled.ensure_usable_for_new_session()


# ── 服务函数（health-check 端点待接线的业务侧落点）───────────────────────


class FakeAgentRepo:
    def __init__(self) -> None:
        self.agents: dict[uuid.UUID, Agent] = {}

    async def get(self, agent_id: uuid.UUID) -> Agent | None:
        return self.agents.get(agent_id)

    async def save_meta(self, agent: Agent) -> None:
        self.agents[agent.id] = agent


class FakeHealthTx:
    def __init__(self, agents: FakeAgentRepo) -> None:
        self.agents = agents

    async def __aenter__(self) -> FakeHealthTx:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeHealthUow:
    def __init__(self) -> None:
        self.agent_repo = FakeAgentRepo()

    def for_tenant(self, tenant_id: uuid.UUID) -> FakeHealthTx:
        return FakeHealthTx(self.agent_repo)


async def test_服务函数_计数往返持久_达阈值迁移_成功自愈():
    uow = FakeHealthUow()
    agent = make_agent()
    uow.agent_repo.agents[agent.id] = agent
    for i in range(3):
        stored = await record_adapter_health_outcome(
            uow, tenant_id=_TENANT, agent_id=agent.id, healthy=False, degrade_threshold=3
        )
        assert stored is not None and stored.adapter_failure_count == i + 1
    assert uow.agent_repo.agents[agent.id].status is AgentStatus.DEGRADED
    stored = await record_adapter_health_outcome(uow, tenant_id=_TENANT, agent_id=agent.id, healthy=True)
    assert stored is not None
    assert stored.status is AgentStatus.ENABLED and stored.adapter_failure_count == 0
    assert uow.agent_repo.agents[agent.id].status is AgentStatus.ENABLED  # 仓储往返持久


async def test_服务函数_agent不存在返回None():
    uow = FakeHealthUow()
    assert await record_adapter_health_outcome(uow, tenant_id=_TENANT, agent_id=uuid.uuid4(), healthy=False) is None


# ── 存量放行：worker 认领执行不校验 agent 状态（存量 Run 跑完不中断）──────


class _FakeStore:
    def __init__(self) -> None:
        self.tasks: dict[uuid.UUID, Task] = {}
        self.events: dict[uuid.UUID, list[TaskEvent]] = {}
        self.sessions: dict[uuid.UUID, Session] = {}
        self.messages: dict[tuple[uuid.UUID, int], Message] = {}
        self.agents: dict[uuid.UUID, Agent] = {}


class _Tx:
    def __init__(self, store: _FakeStore, tasks: Any, sessions: Any, agents: Any) -> None:
        self.tasks = tasks
        self.sessions = sessions
        self.agents = agents

    async def __aenter__(self) -> _Tx:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Tasks:
    def __init__(self, store: _FakeStore) -> None:
        self._store = store

    async def get(self, task_id: uuid.UUID) -> Task | None:
        return self._store.tasks.get(task_id)

    async def save(self, task: Task) -> None:
        self._store.tasks[task.id] = task

    async def append_event(self, task_id: uuid.UUID, event: TaskEvent) -> int:
        self._store.events.setdefault(task_id, []).append(event)
        return 1

    async def list_events(self, task_id: uuid.UUID, **_: Any) -> list[TaskEvent]:
        return []


class _Sessions:
    def __init__(self, store: _FakeStore) -> None:
        self._store = store

    async def get(self, session_id: uuid.UUID) -> Session | None:
        return self._store.sessions.get(session_id)

    async def get_message_by_seq(self, session_id: uuid.UUID, seq: int) -> Message | None:
        return self._store.messages.get((session_id, seq))


class _Agents:
    def __init__(self, store: _FakeStore) -> None:
        self._store = store

    async def get(self, agent_id: uuid.UUID) -> Agent | None:
        return self._store.agents.get(agent_id)

    async def save_meta(self, agent: Agent) -> None:
        self._store.agents[agent.id] = agent


class _Uow:
    def __init__(self) -> None:
        self.store = _FakeStore()

    def for_tenant(self, tenant_id: uuid.UUID) -> _Tx:
        return _Tx(self.store, _Tasks(self.store), _Sessions(self.store), _Agents(self.store))


class _CaptureOrchestrator:
    def __init__(self) -> None:
        self.commands: list[ChatCommand] = []

    def stream_chat(self, command: ChatCommand) -> AsyncIterator[ChatEvent]:
        return self._empty(command)

    async def _empty(self, command: ChatCommand) -> AsyncIterator[ChatEvent]:
        self.commands.append(command)
        return
        yield  # pragma: no cover


async def test_存量放行_degraded_agent的活跃Run照常认领执行():
    """degraded 只挡新会话（bind 闸门）；worker 认领路径不查 agent 状态（04 §10 存量跑完）。"""
    uow = _Uow()
    degraded_agent = make_agent_at(AgentStatus.DEGRADED)
    uow.store.agents[degraded_agent.id] = degraded_agent
    session = Session(id=uuid.uuid4(), tenant_id=_TENANT, agent_id=degraded_agent.id, user_id=uuid.uuid4())
    uow.store.sessions[session.id] = session
    uow.store.messages[(session.id, 1)] = Message(session_id=session.id, seq=1, role="user", content="继续分析")
    task = Task(tenant_id=_TENANT, type="chat", session_id=session.id, payload={"message_seq": 1})
    run = task.start_run()
    uow.store.tasks[task.id] = task

    class _Poller:
        async def next_work(self) -> WorkerClaim | None:
            return WorkerClaim(kind="queued", tenant_id=_TENANT, task_id=task.id, run_id=run.id)

    orchestrator = _CaptureOrchestrator()
    worker = TaskRunWorker(
        uow=uow,
        poller=_Poller(),
        orchestrator_provider=lambda: orchestrator,
        policy=RunRetryPolicy(),
        orphan_sweep_interval_s=30.0,
        orphan_running_timeout_s=300.0,
    )
    assert await worker.poll_once() is True  # degraded 不阻断存量 Run 执行
    assert len(orchestrator.commands) == 1 and orchestrator.commands[0].message == "继续分析"

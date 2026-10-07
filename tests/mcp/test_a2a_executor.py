# tests/mcp/test_a2a_executor.py
"""A2A 委托执行器用例（docs/api/04 §3/§4/§6；FakeModelPort 链 + Fake UoW/存储，零外部依赖）。

断言目标（M5 登记缓议项：委托执行通路接线）：
- 委托→轮询 completed：artifacts 含回答与 citations（chat 编排链真跑，FakeModelPort 承载）；
- 执行异常→failed 可查（模型不可用 / agent 缺失 / 硬超时三分支）；
- 取消映射不变：tasks/cancel 落 canceled，在途执行被 cancel_hook 终止且不覆写终态；
- 会话语义（api/04 §3）：无 session 新建会话 + 首条用户消息落库；带 sessionId 复用；
- 未接线形态回归：无 executor 受理保持 working（M5-2 行为不漂移）。
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any

from services.agent.business.adapters.builtin import BuiltinAdapter
from services.agent.business.chat_context import ChatContextAssembler
from services.agent.business.chat_orchestrator import ChatOrchestrator
from services.agent.domain.model.agent import Agent
from services.agent.domain.model.session import Message, Session
from services.agent.domain.model.task import Task, TaskStatus
from services.kb.business.search_service import KnowledgeCitation, KnowledgeSearchResult
from services.mcp.a2a.executor import (
    InMemoryA2aResultStore,
    a2a_delegate_user_id,
    build_a2a_task_executor,
)
from services.mcp.a2a.jsonrpc import dispatch_jsonrpc
from services.mcp.a2a.service import A2aService
from services.mcp.audit import InMemoryAuditSink
from services.memory.domain.model.l1 import L1Snapshot, MemoryBlock, WindowMessage
from services.platform.ports.model_port import ModelUnavailableError

TENANT = uuid.uuid4()
AGENT_ID = uuid.uuid4()
FULL_SCOPES = ("session:read", "session:write", "session:chat")
_ANSWER = "线路 A 于 14:02 跳闸，原因认定为雷击。"


# ── Fakes（UoW/聚合仓储 + chat 面，tests/agent 同款桩形）────────────────────


class FakeTaskRepo:
    def __init__(self) -> None:
        self.tasks: dict[uuid.UUID, Task] = {}
        self.events: list[tuple[uuid.UUID, str]] = []

    async def get(self, task_id: uuid.UUID) -> Task | None:
        return self.tasks.get(task_id)

    async def save(self, task: Task) -> None:
        self.tasks[task.id] = task

    async def append_event(self, task_id: uuid.UUID, event: Any) -> int:
        self.events.append((task_id, getattr(event, "event_type", "")))
        return len(self.events)

    async def find_active_run(self, task_id: uuid.UUID) -> Any:
        return None

    async def find_running_by_session(self, session_id: uuid.UUID) -> Task | None:
        return None

    async def find_running_by_agent(self, agent_id: uuid.UUID) -> Task | None:
        return None

    async def list(self, **kwargs: Any) -> list[Task]:
        return list(self.tasks.values())


class FakeSessionRepo:
    def __init__(self) -> None:
        self.sessions: dict[uuid.UUID, Session] = {}
        self.messages: list[Message] = []

    async def get(self, session_id: uuid.UUID) -> Session | None:
        return self.sessions.get(session_id)

    async def add(self, session: Session, *, channel: str = "web") -> None:
        self.sessions[session.id] = session

    async def save_meta(self, session: Session) -> None:
        self.sessions[session.id] = session

    async def append_message(self, session_id: uuid.UUID, message: Message) -> int:
        self.messages.append(message)
        return message.seq

    async def list_for_user(self, user_id: uuid.UUID, *, offset: int = 0, limit: int = 20) -> list[Session]:
        return list(self.sessions.values())

    async def list_messages(
        self, session_id: uuid.UUID, *, before_id: uuid.UUID | None = None, limit: int = 20
    ) -> list[Message]:
        return [m for m in self.messages if m.session_id == session_id]

    async def count_by_agent(self, agent_id: uuid.UUID) -> int:
        return 0


class FakeAgentRepo:
    def __init__(self, agent: Agent | None) -> None:
        self._agent = agent

    async def get(self, agent_id: uuid.UUID) -> Agent | None:
        return self._agent if self._agent is None or self._agent.id == agent_id else None

    async def add(self, agent: Agent) -> None: ...
    async def save_meta(self, agent: Agent) -> None: ...
    async def delete(self, agent_id: uuid.UUID) -> None: ...
    async def list(self, *, status: str | None = None, offset: int = 0, limit: int = 20) -> list[Agent]:
        return [self._agent] if self._agent else []

    async def get_adapter(self, adapter_id: uuid.UUID) -> None:
        return None

    async def ensure_platform_adapter(self, agent_tool: str) -> uuid.UUID:
        return uuid.uuid4()


class FakeTx:
    def __init__(self, uow: FakeUow) -> None:
        self.tasks = uow.tasks
        self.sessions = uow.sessions
        self.agents = uow.agents

    async def __aenter__(self) -> FakeTx:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class FakeUow:
    def __init__(self, *, agent: Agent | None = None) -> None:
        self.tasks = FakeTaskRepo()
        self.sessions = FakeSessionRepo()
        self.agents = FakeAgentRepo(agent)

    def for_tenant(self, tenant_id: uuid.UUID) -> FakeTx:
        return FakeTx(self)


class FakeChatModel:
    """ModelPort 桩（chat 形态）：确定性回答；可注延迟/持续失败。"""

    def __init__(self, answer: str = _ANSWER, *, delay_s: float = 0.0, fail: bool = False) -> None:
        self.answer = answer
        self.delay_s = delay_s
        self.fail = fail
        self.calls = 0
        self.last_kwargs: dict[str, Any] = {}

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        self.last_kwargs = kwargs
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail:
            raise ModelUnavailableError("模型端点不可达（模拟）")
        return {"answer": self.answer}


class FakeL1Store:
    async def read(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> L1Snapshot:
        return L1Snapshot(tenant_id=tenant_id, session_id=session_id, window=[])

    async def write_blocks(self, tenant_id: uuid.UUID, session_id: uuid.UUID, blocks: list[MemoryBlock]) -> int:
        return 0

    async def append_window(self, tenant_id: uuid.UUID, session_id: uuid.UUID, messages: list[WindowMessage]) -> int:
        return len(messages)

    async def write_state(self, tenant_id: uuid.UUID, session_id: uuid.UUID, state: dict) -> None:
        pass

    async def delete_all(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
        pass


class FakeL2Repo:
    async def search_candidates(self, user_id: uuid.UUID, query: str, *, limit: int) -> list[Any]:
        return []

    async def recent_candidates(self, user_id: uuid.UUID, *, limit: int) -> list[Any]:
        return []


class FakeKnowledge:
    async def search(self, **kwargs: Any) -> KnowledgeSearchResult:
        citation = KnowledgeCitation(
            chunk_id=uuid.uuid4(),
            doc_id=uuid.uuid4(),
            doc_name="停电分析报告",
            quote="线路 A 于 14:02 因雷击跳闸，重合不成功。",
            score=0.83,
        )
        return KnowledgeSearchResult(query=str(kwargs.get("query") or ""), citations=[citation])


# ── 装配 Helpers ──────────────────────────────────────────────────────────


def _agent() -> Agent:
    return Agent(id=AGENT_ID, tenant_id=TENANT, name="平台委托执行", agent_tool="builtin", adapter_id=uuid.uuid4())


def _orchestrator(model: FakeChatModel) -> ChatOrchestrator:
    @asynccontextmanager
    async def fake_session_factory():
        yield None

    assembler = ChatContextAssembler(
        l1_store=FakeL1Store(),  # type: ignore[arg-type]
        session_factory=fake_session_factory,  # type: ignore[arg-type]
        knowledge=FakeKnowledge(),  # type: ignore[arg-type]
        retrieval_retry_max=0,
        repo_factory=lambda db, tenant: FakeL2Repo(),  # type: ignore[arg-type,return-value]
    )
    return ChatOrchestrator(adapters={"builtin": BuiltinAdapter(model)}, assembler=assembler)


def _executor(
    uow: FakeUow,
    model: FakeChatModel,
    sink: InMemoryAuditSink,
    *,
    result_store: InMemoryA2aResultStore | None = None,
    chat_timeout_s: float | None = None,
) -> Any:
    kwargs: dict[str, Any] = {}
    if result_store is not None:
        kwargs["result_store"] = result_store
    if chat_timeout_s is not None:
        kwargs["chat_timeout_s"] = chat_timeout_s
    return build_a2a_task_executor(
        uow=uow,
        tenant_id=TENANT,
        agent_id=AGENT_ID,
        orchestrator=_orchestrator(model),
        audit_sink=sink,
        **kwargs,
    )


def _service(
    uow: FakeUow,
    sink: InMemoryAuditSink,
    executor: Any = None,
    result_store: InMemoryA2aResultStore | None = None,
) -> A2aService:
    service: A2aService = A2aService(uow=uow, tenant_id=TENANT, audit_sink=sink)  # type: ignore[arg-type]
    if executor is not None:
        service.attach_executor(executor=executor, result_store=result_store, cancel_hook=executor.cancel)
    return service


def _send_params(text: str = "线路A停电原因是什么？", session_id: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"message": {"role": "user", "parts": [{"kind": "text", "text": text}]}}
    if session_id:
        params["sessionId"] = session_id
    return params


# ── 委托→轮询 completed（主链路）──────────────────────────────────────────


async def test_委托后轮询completed_artifacts含回答与引用() -> None:
    """E2E：message/send 受理 → 后台 chat 链执行 → tasks/get 见 completed + 产物。"""
    uow = FakeUow(agent=_agent())
    sink = InMemoryAuditSink()
    store = InMemoryA2aResultStore()
    model = FakeChatModel()
    executor = _executor(uow, model, sink, result_store=store)
    service = _service(uow, sink, executor, store)

    accepted = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": _send_params()}, service, FULL_SCOPES
    )
    task_view = accepted["result"]["task"]
    assert task_view["status"]["state"] == "submitted"  # 受理即凭证（不阻塞）
    await service.drain_background()

    polled = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"taskId": task_view["id"]}},
        service,
        FULL_SCOPES,
    )
    done = polled["result"]["task"]
    assert done["status"]["state"] == "completed"  # working → completed（api/04 §4）
    artifact = done["artifacts"][0]
    assert artifact["parts"][0]["text"] == _ANSWER  # 回答经 artifacts 回传
    assert artifact["metadata"]["citations"][0]["doc_name"] == "停电分析报告"  # 引用与 trace_id 同源回传
    assert artifact["metadata"]["trace_id"]
    assert model.calls == 1  # 单轮对话（与 REST 同一条编排链）
    # 终态落库：task 聚合 succeeded + 结果存储同源
    task = uow.tasks.tasks[uuid.UUID(task_view["id"])]
    assert task.status is TaskStatus.SUCCEEDED and task.result is not None
    assert task.result["answer"] == _ANSWER
    assert await store.get(task.id) is not None
    # 执行审计留痕（api/04 §5：执行面 ok 行，消息只留摘要）
    exec_rows = [rec for rec in sink.entries if rec.tool == "a2a.delegate"]
    assert exec_rows and exec_rows[-1].status == "ok"
    assert "message_sha256_32" in exec_rows[-1].params_digest


async def test_委托新建会话_首条用户消息落库且任务绑定() -> None:
    """api/04 §3 会话语义：无 sessionId 时新建会话，委托消息=首条用户消息，task 绑定会话。"""
    uow = FakeUow(agent=_agent())
    sink = InMemoryAuditSink()
    executor = _executor(uow, FakeChatModel(), sink)
    service = _service(uow, sink, executor)

    accepted = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 3, "method": "message/send", "params": _send_params()}, service, FULL_SCOPES
    )
    task_id = uuid.UUID(accepted["result"]["task"]["id"])
    await service.drain_background()

    task = uow.tasks.tasks[task_id]
    assert task.session_id is not None
    session = uow.sessions.sessions[task.session_id]
    assert session.status.value == "active"  # 首条用户消息激活（04 §3）
    assert session.user_id == a2a_delegate_user_id(TENANT)  # 租户级委托主体（确定性合成身份）
    assert session.agent_id == AGENT_ID
    user_rows = [m for m in uow.sessions.messages if m.role == "user"]
    assert user_rows and "线路A" in user_rows[-1].content
    assert ("a2a.session.bound" in [e for _, e in uow.tasks.events]) or any(
        e == "a2a.session.bound" for _, e in uow.tasks.events
    )


async def test_带sessionId_复用既有会话不新建() -> None:
    """params.sessionId 平台扩展位：payload 落库 → 执行器复用会话（不新建）。"""
    uow = FakeUow(agent=_agent())
    sink = InMemoryAuditSink()
    existing = Session(id=uuid.uuid4(), tenant_id=TENANT, agent_id=AGENT_ID, user_id=a2a_delegate_user_id(TENANT))
    uow.sessions.sessions[existing.id] = existing
    executor = _executor(uow, FakeChatModel(), sink)
    service = _service(uow, sink, executor)

    accepted = await dispatch_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "message/send",
            "params": _send_params(session_id=str(existing.id)),
        },
        service,
        FULL_SCOPES,
    )
    task_id = uuid.UUID(accepted["result"]["task"]["id"])
    await service.drain_background()

    task = uow.tasks.tasks[task_id]
    assert task.session_id == existing.id
    assert set(uow.sessions.sessions) == {existing.id}  # 未新建会话


# ── 执行异常 → failed 可查 ────────────────────────────────────────────────


async def test_模型不可用_任务落failed可查且审计error() -> None:
    """生成失败（5002）→ RUN_ERROR → task failed；tasks/get 映射 failed、无 artifacts。"""
    uow = FakeUow(agent=_agent())
    sink = InMemoryAuditSink()
    executor = _executor(uow, FakeChatModel(fail=True), sink)
    service = _service(uow, sink, executor)

    accepted = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 5, "method": "message/send", "params": _send_params()}, service, FULL_SCOPES
    )
    task_id = uuid.UUID(accepted["result"]["task"]["id"])
    await service.drain_background()

    polled = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 6, "method": "tasks/get", "params": {"taskId": str(task_id)}}, service, FULL_SCOPES
    )
    done = polled["result"]["task"]
    assert done["status"]["state"] == "failed"  # working → failed（api/04 §4）
    assert done["artifacts"] == []  # failed 无产物
    assert uow.tasks.tasks[task_id].status is TaskStatus.FAILED
    exec_rows = [rec for rec in sink.entries if rec.tool == "a2a.delegate"]
    assert exec_rows and exec_rows[-1].status == "error"


async def test_委托绑定agent缺失_任务落failed_不执行对话() -> None:
    """agent 不存在（装配错）→ DelegateFailure → failed；模型零调用（不产生对话）。"""
    uow = FakeUow(agent=None)
    sink = InMemoryAuditSink()
    model = FakeChatModel()
    executor = _executor(uow, model, sink)
    service = _service(uow, sink, executor)

    accepted = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 7, "method": "message/send", "params": _send_params()}, service, FULL_SCOPES
    )
    task_id = uuid.UUID(accepted["result"]["task"]["id"])
    await service.drain_background()

    assert uow.tasks.tasks[task_id].status is TaskStatus.FAILED
    assert model.calls == 0
    assert any(rec.tool == "a2a.delegate" and rec.status == "error" for rec in sink.entries)


async def test_委托硬超时_落failed_审计timeout() -> None:
    """执行超时必设：chat_timeout_s 兜底触发 → run timeout → task failed、审计 timeout。"""
    uow = FakeUow(agent=_agent())
    sink = InMemoryAuditSink()
    executor = _executor(uow, FakeChatModel(delay_s=30.0), sink, chat_timeout_s=0.05)
    service = _service(uow, sink, executor)

    accepted = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 8, "method": "message/send", "params": _send_params()}, service, FULL_SCOPES
    )
    task_id = uuid.UUID(accepted["result"]["task"]["id"])
    await service.drain_background()

    assert uow.tasks.tasks[task_id].status is TaskStatus.FAILED
    assert any(rec.tool == "a2a.delegate" and rec.status == "timeout" for rec in sink.entries)


# ── 取消映射不变 + 取消传播 ───────────────────────────────────────────────


async def test_取消传播_在途执行终止_终态canceled不被覆写() -> None:
    """tasks/cancel：聚合落 canceled + cancel_hook 终止在途执行；终态不可逆（api/04 §3）。"""
    uow = FakeUow(agent=_agent())
    sink = InMemoryAuditSink()
    executor = _executor(uow, FakeChatModel(delay_s=30.0), sink)
    service = _service(uow, sink, executor)

    accepted = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 9, "method": "message/send", "params": _send_params()}, service, FULL_SCOPES
    )
    task_id = accepted["result"]["task"]["id"]
    cancelled = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 10, "method": "tasks/cancel", "params": {"taskId": task_id}}, service, FULL_SCOPES
    )
    assert cancelled["result"]["task"]["status"]["state"] == "canceled"  # 映射不变
    await service.drain_background()

    polled = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 11, "method": "tasks/get", "params": {"taskId": task_id}}, service, FULL_SCOPES
    )
    assert polled["result"]["task"]["status"]["state"] == "canceled"  # 终态不被执行侧覆写
    assert polled["result"]["task"]["artifacts"] == []
    assert uow.tasks.tasks[uuid.UUID(task_id)].status is TaskStatus.CANCELLED


# ── 未接线形态回归（M5-2 行为）───────────────────────────────────────────


async def test_未接线executor_受理保持working() -> None:
    """无 executor 注入：受理返回 submitted，轮询恒 working（裸受理形态不漂移）。"""
    uow = FakeUow(agent=_agent())
    sink = InMemoryAuditSink()
    service = _service(uow, sink)

    accepted = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 12, "method": "message/send", "params": _send_params()}, service, FULL_SCOPES
    )
    task_id = uuid.UUID(accepted["result"]["task"]["id"])
    await service.drain_background()  # 空集排空（无后台任务）

    polled = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 13, "method": "tasks/get", "params": {"taskId": str(task_id)}}, service, FULL_SCOPES
    )
    assert polled["result"]["task"]["status"]["state"] == "working"
    assert polled["result"]["task"]["artifacts"] == []


def test_委托主体身份_同租户确定性_跨租户隔离() -> None:
    """租户级委托主体：同租户恒等（记忆连续），跨租户不同（隔离）。"""
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    assert a2a_delegate_user_id(tenant_a) == a2a_delegate_user_id(tenant_a)
    assert a2a_delegate_user_id(tenant_a) != a2a_delegate_user_id(tenant_b)

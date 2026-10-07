# tests/mcp/test_a2a_service.py
"""A2A v1.0 服务面单测（docs/api/04 契约；Fake UoW + 内存审计汇，零外部依赖）。

断言目标：
- Agent Card：字段表/camelCase wire 形状/authentication 如实声明（api/04 §2/§5）；
- 鉴权：API Key 缺失 1001/无效 1002、PDP deny-by-default 2001（api/04 §5 + 08 §2.5）；
- 委托数据流：message/send 受理凭证（submitted）→ tasks/get 状态映射 → tasks/cancel；
- 状态映射表（api/04 §4）逐态断言；审计全留痕（委托/查询/取消/拒绝）。
"""

from __future__ import annotations

import uuid

import pytest

from services.agent.domain.model.task import Run, RunStatus, Task, TaskStatus
from services.mcp.a2a.auth import A2aAuthError, ApiKeyAuthorizer, check_scopes
from services.mcp.a2a.card import PROTOCOL_VERSION, AgentSkill, build_agent_card
from services.mcp.a2a.errors import A2aAppError
from services.mcp.a2a.jsonrpc import (
    INVALID_PARAMS,
    JSONRPC_APP_ERROR,
    METHOD_NOT_FOUND,
    dispatch_jsonrpc,
)
from services.mcp.a2a.service import A2aService, map_task_state
from services.mcp.audit import InMemoryAuditSink

TENANT = uuid.uuid4()
FULL_SCOPES = ("session:read", "session:write", "session:chat")
EMPTY_SCOPES: tuple[str, ...] = ()


# ── Fake UoW（Task 受理路径的最小事务形状）────────────────────────────────


class FakeTaskRepo:
    def __init__(self) -> None:
        self.tasks: dict[uuid.UUID, Task] = {}
        self.events: list[tuple[uuid.UUID, str]] = []

    async def get(self, task_id: uuid.UUID) -> Task | None:
        return self.tasks.get(task_id)

    async def save(self, task: Task) -> None:
        self.tasks[task.id] = task

    async def append_event(self, task_id: uuid.UUID, event: object) -> int:
        self.events.append((task_id, getattr(event, "event_type", "")))
        return len(self.events)


class FakeTx:
    def __init__(self, repo: FakeTaskRepo) -> None:
        self.tasks = repo

    async def __aenter__(self) -> FakeTx:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class FakeUow:
    def __init__(self) -> None:
        self.repo = FakeTaskRepo()

    def for_tenant(self, tenant_id: uuid.UUID) -> FakeTx:
        return FakeTx(self.repo)


def _service() -> tuple[A2aService, FakeUow, InMemoryAuditSink]:
    uow = FakeUow()
    sink = InMemoryAuditSink()
    service = A2aService(uow=uow, tenant_id=TENANT, audit_sink=sink)
    return service, uow, sink


def _send_params(text: str = "为城东馈线 F12 创建抢修工单", skill_id: str | None = "create-outage-ticket") -> dict:
    params: dict = {"message": {"role": "user", "parts": [{"kind": "text", "text": text}]}}
    if skill_id:
        params["skillId"] = skill_id
    return params


# ── Agent Card ────────────────────────────────────────────────────────────


def test_agent_card_字段表与wire形状() -> None:
    card = build_agent_card(
        base_url="https://onto.example.cn",
        skills=[AgentSkill(id="cancel-order", name="取消订单", description="取消指定订单", tags=["supply"])],
    )
    wire = card.to_wire()
    assert card.protocol_version == PROTOCOL_VERSION == "1.0"
    assert wire["protocolVersion"] == "1.0"  # camelCase wire 形状
    assert wire["url"] == "https://onto.example.cn/a2a"  # 任务端点
    assert wire["defaultInputModes"] == ["text/plain", "application/json"]
    assert wire["capabilities"]["streaming"] is False  # SSE 未启用如实声明
    assert wire["capabilities"]["pushNotifications"] is False
    assert wire["capabilities"]["stateTransitionHistory"] is True
    assert wire["authentication"] == {"schemes": ["api_key"]}  # api/04 §5：本批 api_key 起步
    assert wire["skills"][0]["id"] == "cancel-order" and wire["skills"][0]["tags"] == ["supply"]


# ── 鉴权与 PDP ────────────────────────────────────────────────────────────


def test_api_key_缺失1001_无效1002_有效返回scopes() -> None:
    authorizer = ApiKeyAuthorizer({"k1": FULL_SCOPES})
    with pytest.raises(A2aAuthError) as missing:
        authorizer.authenticate(None)
    assert missing.value.code == 1001  # TOKEN_MISSING
    with pytest.raises(A2aAuthError) as invalid:
        authorizer.authenticate("Bearer wrong-key")
    assert invalid.value.code == 1002  # TOKEN_INVALID
    assert authorizer.authenticate("Bearer k1") == FULL_SCOPES


def test_pdp_scope不足_2001_deny_by_default() -> None:
    with pytest.raises(A2aAppError) as denied:
        check_scopes(EMPTY_SCOPES, "session:chat")
    assert denied.value.code == 2001  # SCOPE_INSUFFICIENT
    check_scopes(FULL_SCOPES, "session:chat")  # 命中不抛


# ── 委托数据流（受理 → 状态回查询 → 取消）─────────────────────────────────


async def test_message_send_受理凭证_task聚合与事件留痕() -> None:
    service, uow, sink = _service()
    response = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 11, "method": "message/send", "params": _send_params()},
        service,
        FULL_SCOPES,
    )
    assert response["id"] == 11 and "result" in response
    task_view = response["result"]["task"]
    assert task_view["status"]["state"] == "submitted"  # api/04：受理凭证语义
    assert uuid.UUID(task_view["id"])  # 合法 task_id

    stored = uow.repo.tasks[uuid.UUID(task_view["id"])]
    assert stored.type == "a2a" and stored.status is TaskStatus.RUNNING  # start_run 聚合方法
    assert stored.payload["skill_id"] == "create-outage-ticket" and stored.payload["message"]
    assert stored.runs and stored.runs[0].status is RunStatus.QUEUED  # 活跃 Run 受理
    assert uow.repo.events and uow.repo.events[-1][1] == "task.created"
    assert any(rec.tool == "a2a.message/send" and rec.status == "ok" for rec in sink.entries)


async def test_tasks_get_受理后映射working_未找到404语义() -> None:
    service, uow, sink = _service()
    response = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 12, "method": "message/send", "params": _send_params()}, service, FULL_SCOPES
    )
    task_id = response["result"]["task"]["id"]

    polled = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 13, "method": "tasks/get", "params": {"taskId": task_id}}, service, FULL_SCOPES
    )
    assert polled["result"]["task"]["status"]["state"] == "working"  # queued → working（api/04 §4）
    assert polled["result"]["task"]["artifacts"] == []  # 执行通路未接线，产物位空

    missing = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 14, "method": "tasks/get", "params": {"taskId": str(uuid.uuid4())}},
        service,
        FULL_SCOPES,
    )
    assert missing["error"]["code"] == JSONRPC_APP_ERROR
    assert missing["error"]["data"]["code"] == 404  # api/03 §3.9 同款 404 语义
    assert any(rec.tool == "a2a.tasks/get" for rec in sink.entries)


async def test_tasks_cancel_取消后canceled_重复取消聚合拒绝() -> None:
    service, uow, sink = _service()
    response = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 15, "method": "message/send", "params": _send_params()}, service, FULL_SCOPES
    )
    task_id = response["result"]["task"]["id"]

    cancelled = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 16, "method": "tasks/cancel", "params": {"taskId": task_id}},
        service,
        FULL_SCOPES,
    )
    assert cancelled["result"]["task"]["status"]["state"] == "canceled"  # cancelled → canceled 映射

    again = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 17, "method": "tasks/cancel", "params": {"taskId": task_id}},
        service,
        FULL_SCOPES,
    )
    assert again["error"]["code"] == JSONRPC_APP_ERROR  # 终态不可逆（聚合断言）→ 应用错误
    assert again["error"]["data"]["code"] == 3001  # PARAM_INVALID（02 §7 已登记码）


async def test_scope不足拒绝_审计记denied() -> None:
    service, _uow, sink = _service()
    response = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 18, "method": "message/send", "params": _send_params()}, service, EMPTY_SCOPES
    )
    assert response["error"]["code"] == JSONRPC_APP_ERROR
    assert response["error"]["data"]["code"] == 2001
    assert response["error"]["data"]["detail"]["required"] == ["session:chat"]
    denied = [rec for rec in sink.entries if rec.tool == "a2a.message/send"]
    assert denied and denied[-1].status == "denied" and denied[-1].code == 2001


async def test_未知方法与未启用方法_32601() -> None:
    service, _uow, _sink = _service()
    unknown = await dispatch_jsonrpc({"jsonrpc": "2.0", "id": 19, "method": "tasks/list"}, service, FULL_SCOPES)
    assert unknown["error"]["code"] == METHOD_NOT_FOUND
    stream = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 20, "method": "message/stream", "params": _send_params()}, service, FULL_SCOPES
    )
    assert stream["error"]["code"] == METHOD_NOT_FOUND
    assert "streaming" in stream["error"]["message"]  # 未启用说明（capabilities.streaming=false）


async def test_入参缺陷_32602_携带3001() -> None:
    service, _uow, _sink = _service()
    bad = await dispatch_jsonrpc(
        {"jsonrpc": "2.0", "id": 21, "method": "message/send", "params": {"message": {"parts": []}}},
        service,
        FULL_SCOPES,
    )
    assert bad["error"]["code"] == INVALID_PARAMS
    assert bad["error"]["data"]["code"] == 3001  # PARAM_INVALID
    no_params = await dispatch_jsonrpc({"jsonrpc": "2.0", "id": 22, "method": "tasks/get"}, service, FULL_SCOPES)
    assert no_params["error"]["code"] == INVALID_PARAMS


# ── 状态映射表（api/04 §4）────────────────────────────────────────────────


def test_状态映射_逐态断言() -> None:
    def task_with(status: TaskStatus, run_status: RunStatus | None = None) -> Task:
        task = Task(tenant_id=TENANT, type="a2a")
        task.status = status  # noqa: B010 ——测试构造直接置态（映射纯函数）
        if run_status is not None:
            run = Run(tenant_id=TENANT, task_id=task.id, status=run_status)
            task.runs.append(run)
            task.active_run_id = run.id
        return task

    assert map_task_state(task_with(TaskStatus.PENDING)) == "submitted"
    assert map_task_state(task_with(TaskStatus.RUNNING, RunStatus.QUEUED)) == "working"
    assert map_task_state(task_with(TaskStatus.RUNNING, RunStatus.RUNNING)) == "working"
    assert map_task_state(task_with(TaskStatus.RUNNING, RunStatus.WAITING_TOOL)) == "input-required"
    assert map_task_state(task_with(TaskStatus.SUCCEEDED)) == "completed"
    assert map_task_state(task_with(TaskStatus.FAILED)) == "failed"
    assert map_task_state(task_with(TaskStatus.CANCELLED)) == "canceled"
    # task 未终态 + 终态 Run（执行侧先落终态窗口）：
    assert map_task_state(task_with(TaskStatus.RUNNING, RunStatus.FAILED)) == "failed"
    assert map_task_state(task_with(TaskStatus.RUNNING, RunStatus.TIMEOUT)) == "failed"
    assert map_task_state(task_with(TaskStatus.RUNNING, RunStatus.CANCELLED)) == "canceled"

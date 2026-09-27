# tests/mcp/test_a2a_http.py
"""A2A 独立 HTTP 应用用例（httpx ASGITransport，零外部依赖）。

断言目标（docs/api/04 §2/§4/§5）：
- GET /.well-known/agent-card.json：发现入口无需鉴权（200 + camelCase Card）；
- POST /a2a 鉴权：无凭据 401（1001）/坏凭据 401（1002）+ 平台统一错误体四字段；
- JSON-RPC 全链：message/send 受理 → tasks/get → tasks/cancel（Fake UoW 承载受理路径）；
- 未知方法 -32601；非 JSON 体 -32600。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from services.mcp.a2a.app import build_a2a_app
from services.mcp.a2a.auth import ApiKeyAuthorizer
from services.mcp.a2a.card import AgentSkill, build_agent_card
from services.mcp.a2a.service import A2aService
from services.mcp.audit import InMemoryAuditSink

TENANT = uuid.uuid4()
SCOPES = ("session:read", "session:write", "session:chat")


class FakeTaskRepo:
    def __init__(self) -> None:
        self.tasks: dict[uuid.UUID, Any] = {}

    async def get(self, task_id: uuid.UUID) -> Any:
        return self.tasks.get(task_id)

    async def save(self, task: Any) -> None:
        self.tasks[task.id] = task

    async def append_event(self, task_id: uuid.UUID, event: Any) -> int:
        return 1


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


@pytest.fixture
async def client() -> AsyncClient:
    card = build_agent_card(
        base_url="http://127.0.0.1:9801",
        skills=[AgentSkill(id="task-delegate", name="平台任务委托", description="占位")],
    )
    service = A2aService(uow=FakeUow(), tenant_id=TENANT, audit_sink=InMemoryAuditSink())
    app = build_a2a_app(card=card, service=service, authorizer=ApiKeyAuthorizer({"k-good": SCOPES}), timeout_s=5.0)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http:
        yield http


async def test_发现入口_无需鉴权返回camelCase卡片(client: AsyncClient) -> None:
    resp = await client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()
    assert card["protocolVersion"] == "1.0"
    assert card["url"].endswith("/a2a")
    assert card["skills"][0]["id"] == "task-delegate"
    assert card["authentication"]["schemes"] == ["api_key"]


async def test_任务端点_无凭据401_1001_统一错误体(client: AsyncClient) -> None:
    resp = await client.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}})
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == 1001 and body["trace_id"]  # 四字段统一错误体（02 §7）


async def test_任务端点_坏凭据401_1002(client: AsyncClient) -> None:
    resp = await client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 2, "method": "message/send", "params": {}},
        headers={"Authorization": "Bearer k-bad"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 1002


async def test_任务端点_全链_受理_查询_取消(client: AsyncClient) -> None:
    headers = {"Authorization": "Bearer k-good"}
    send = await client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 11,
            "method": "message/send",
            "params": {
                "message": {"role": "user", "parts": [{"kind": "text", "text": "委托任务"}]},
                "skillId": "task-delegate",
            },
        },
        headers=headers,
    )
    assert send.status_code == 200
    assert send.json()["result"]["task"]["status"]["state"] == "submitted"
    task_id = send.json()["result"]["task"]["id"]

    poll = await client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 12, "method": "tasks/get", "params": {"taskId": task_id}},
        headers=headers,
    )
    assert poll.json()["result"]["task"]["status"]["state"] == "working"

    cancel = await client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 13, "method": "tasks/cancel", "params": {"taskId": task_id}},
        headers=headers,
    )
    assert cancel.json()["result"]["task"]["status"]["state"] == "canceled"


async def test_任务端点_未知方法_32601_非json体_32600(client: AsyncClient) -> None:
    headers = {"Authorization": "Bearer k-good"}
    unknown = await client.post("/a2a", json={"jsonrpc": "2.0", "id": 3, "method": "tasks/list"}, headers=headers)
    assert unknown.json()["error"]["code"] == -32601
    malformed = await client.post("/a2a", content=b"not-json", headers={**headers, "content-type": "application/json"})
    assert malformed.json()["error"]["code"] == -32600

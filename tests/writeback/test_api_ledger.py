"""writeback 台账 REST 面测试（api/01 §5.8 ★ GET /admin/writeback/ledger/{id}）。

覆盖 200（终态台账全字段投影）/ 404（未命中，api/03 §3.9「未找到 404」语义）/
2001（scope 不足 403，PDP 第 3 步 deny-by-default）/ 5004（查询面未装配 503）
+ DTO extra=forbid 铁律 + 与 MCP tool ``writeback.status`` 输出同形（REST 对应确认）。

零 PG：ActionDispatcher 直注（tests/writeback/conftest 的 Fake 台账仓储 + Scripted 适配器），
经最小 FastAPI app + GlobalException 兜底发真实 HTTP 请求。
"""

from __future__ import annotations

import sys
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from tests.writeback.conftest import NOW, TENANT_ID, ScriptedAdapter, make_dispatcher  # tests/writeback/conftest.py

if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from services.writeback.api.ledger import router as writeback_ledger_router
from services.writeback.api.schemas.ledger import WritebackLedgerOut

from services.gateway.middlewares import ErrorCode, GlobalExceptionMiddleware
from services.mcp.server import TOOL_SPECS
from services.platform.deps import Principal, get_current_principal
from services.writeback.business.action_dispatcher import project_status
from services.writeback.domain.model import WritebackAction, WritebackLedger

_ACTION_IRI = "http://ontology-agent.local/o/t1/supply#CreateOrder"


# ---------------------------------------------------------------- 装配助手


def _principal(scopes: list[str]) -> Principal:
    return Principal(
        {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(TENANT_ID),
            "roles": ["member"],
            "scopes": scopes,
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


def _succeeded_entry() -> WritebackLedger:
    """终态台账行（succeeded；status() 对终态幂等原样返回，不触适配器）。"""
    action = WritebackAction.instantiate(
        tenant_id=TENANT_ID,
        action_iri=_ACTION_IRI,
        params={"feeder": "F-1"},
        risk_level="medium",
        connector_id=uuid.uuid4(),
    )
    entry = WritebackLedger.create_pending(
        tenant_id=TENANT_ID,
        action=action,
        request_payload={"action_iri": _ACTION_IRI, "params": action.params, "risk_level": "medium"},
        now=NOW,
    )
    entry.mark_accepted({"accepted": True, "receipt_no": "ORD-1"}, NOW)
    entry.mark_succeeded(NOW)
    return entry


def _make_client(monkeypatch: pytest.MonkeyPatch, principal: Principal, *, dispatcher: object | None) -> AsyncClient:
    """最小 app：真实路由 + scope 门禁 + GlobalException 兜底；principal 经依赖覆写注入。"""
    app = FastAPI()
    app.include_router(writeback_ledger_router)
    app.add_middleware(GlobalExceptionMiddleware)
    if dispatcher is not None:
        app.state.action_dispatcher = dispatcher
    app.dependency_overrides[get_current_principal] = lambda: principal
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


# ---------------------------------------------------------------- 测试


async def test_台账按id查询_200_终态全字段(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：直注 dispatcher + Fake 台账仓储放入 succeeded 行
    dispatcher, ledger = make_dispatcher(ScriptedAdapter())
    entry = _succeeded_entry()
    ledger.rows[entry.id] = entry
    client = _make_client(monkeypatch, _principal(["action:invoke"]), dispatcher=dispatcher)
    try:
        # Act
        resp = await client.get(f"/admin/writeback/ledger/{entry.id}")
    finally:
        await client.aclose()
    # Assert：api/03 §3.9 全字段投影
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ledger_id"] == str(entry.id)
    assert body["status"] == "succeeded"
    assert body["idempotency_key"] == f"{TENANT_ID}:{entry.action_instance_id}"
    assert body["receipt"]["receipt_no"] == "ORD-1"
    assert body["attempts"] == 0 and body["needs_human"] is False
    assert body["last_error"] is None and body["updated_at"] == NOW.isoformat()


async def test_台账未命中_404语义(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatcher, _ledger = make_dispatcher(ScriptedAdapter())
    client = _make_client(monkeypatch, _principal(["action:invoke"]), dispatcher=dispatcher)
    try:
        resp = await client.get(f"/admin/writeback/ledger/{uuid.uuid4()}")
    finally:
        await client.aclose()
    # Assert：api/03 §3.9「未找到 404」口径（跨租户同口径不泄露存在性）
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == 404 and "不存在" in body["message"]


async def test_台账查询_scope不足_403_2001(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatcher, _ledger = make_dispatcher(ScriptedAdapter())
    client = _make_client(monkeypatch, _principal(["session:read"]), dispatcher=dispatcher)  # 无 action:invoke
    try:
        resp = await client.get(f"/admin/writeback/ledger/{uuid.uuid4()}")
    finally:
        await client.aclose()
    # Assert：PDP 第 3 步 deny-by-default → 403+2001（02 §7），响应不进路由
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == int(ErrorCode.SCOPE_INSUFFICIENT)
    assert body["detail"]["required"] == "action:invoke"


async def test_查询面未装配_503_5004(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch, _principal(["action:invoke"]), dispatcher=None)  # 组合根缺位
    try:
        resp = await client.get(f"/admin/writeback/ledger/{uuid.uuid4()}")
    finally:
        await client.aclose()
    assert resp.status_code == 503 and resp.json()["code"] == 5004


def test_台账DTO_extra_forbid_未知字段拒绝() -> None:
    payload = {
        "ledger_id": str(uuid.uuid4()),
        "idempotency_key": "k",
        "status": "succeeded",
        "receipt": None,
        "action_instance_id": str(uuid.uuid4()),
        "attempts": 1,
        "needs_human": False,
        "last_error": None,
        "updated_at": None,
    }
    with pytest.raises(ValidationError):
        WritebackLedgerOut.model_validate({**payload, "unexpected_field": 1})


def test_REST输出与MCP_writeback_status同形_单口径确认() -> None:
    # Arrange：同一台账行分别走服务层投影与 REST DTO
    entry = _succeeded_entry()
    # Assert：字段集逐一同形（REST=writeback.status 的对应面），MCP 侧 tool 已登记
    assert set(WritebackLedgerOut.model_fields) == set(project_status(entry))
    assert "writeback.status" in TOOL_SPECS

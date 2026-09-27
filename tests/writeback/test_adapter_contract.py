"""BizSystemAdapter 连接器契约套件（业务回写设计 §5.1 行为保证；Mock 与 HTTP 骨架同套参数化跑）。

契约断言「行为保证」而非实现细节：任何连接器实现入套即验收（§6 电力工单=第一验收对象）——
元数据四项声明与注册表（§5.2）、受理凭证三要素（§2.3）、native 幂等去重（§2.2）、
query_status 三值语义（§2.5/§4）、补偿同契约（§3.2）、健康预检布尔（§5.1）。
HTTP 形态全量 httpx.MockTransport 脚本化（零真网）：正常/429/5xx/4xx/超时/重复单 409
转查单/终态撤单失败。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Protocol

import httpx
import pytest

from services.writeback.adapters.base import (
    AdapterError,
    BizSystemAdapter,
    CompensationRequest,
    ConnectorMeta,
    ConnectorRegistry,
    WritebackReceipt,
    WritebackRequest,
)
from services.writeback.adapters.http_power_ticket import HttpPowerTicketAdapter, http_power_ticket_meta
from services.writeback.adapters.mock_power_ticket import (
    ACTION_IRI_CANCEL_ORDER,
    ACTION_IRI_CREATE_ORDER,
    ORDER_CANCELLED,
    ORDER_CREATED,
    ORDER_DONE,
    MockPowerTicketAdapter,
    mock_power_ticket_meta,
)
from services.writeback.business.policy import WritebackPolicy
from tests.writeback.conftest import TENANT_ID

KEY = f"{TENANT_ID}:contract-1"
OCCURRED_AT = "2026-09-28T08:00:00+00:00"  # fake 业务系统固定受理时间戳（可解析性断言用）


class _BizControl(Protocol):
    """契约测试控制面：推进工单状态（Mock=观测面 advance / HTTP=fake 业务系统脚本面 advance）。"""

    def advance(self, receipt_no: str, status: str) -> None: ...


class _FakeBizServer:
    """进程内「电力工单业务系统」替身（httpx.MockTransport handler，零真网）。

    幂等/状态机语义与 Mock 连接器同构（§6.1）：同键已受理 → 409 重复单（连接器按幂等语义
    转查单）；done 终态不可撤。脚本面 = 直接改 orders_by_key / fail_next / timeout_next。
    """

    def __init__(self) -> None:
        self.orders_by_key: dict[str, dict[str, Any]] = {}  # idempotency_key → 工单
        self.fail_next: list[tuple[int, dict[str, Any]]] = []  # (status_code, body) 故障注入队列
        self.timeout_next = 0
        self._seq = 0

    def advance(self, receipt_no: str, status: str) -> None:
        """推进工单状态（终态 done 不可再变更，同 Mock 业务侧不可逆语义）。"""
        for order in self.orders_by_key.values():
            if order["receipt_no"] == receipt_no:
                if order["status"] == ORDER_DONE:
                    raise AssertionError("工单已完结（done），不可再变更")
                order["status"] = status
                return
        raise AssertionError(f"工单不存在: {receipt_no}")

    async def handle(self, request: httpx.Request) -> httpx.Response:
        """MockTransport handler（async 形态）：脚本短路 → 路由 → 幂等/状态机语义。"""
        if self.timeout_next > 0:
            self.timeout_next -= 1
            raise httpx.ConnectTimeout("脚本化超时（响应丢失）", request=request)
        if self.fail_next:
            status, body = self.fail_next.pop(0)
            return httpx.Response(status, json=body)
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"system": "power-ticket"})
        if path == "/orders" and request.method == "POST":
            return self._create_order(request)
        if request.method == "GET" and path.startswith("/orders/by-key/"):
            return self._query_order(path)
        if request.method == "POST" and path.startswith("/orders/") and path.endswith("/cancel"):
            return self._cancel_order(path)
        return httpx.Response(404, json={"error": "NOT_FOUND", "message": path})

    def _create_order(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("Idempotency-Key") or ""
        existing = self.orders_by_key.get(key)
        if existing is not None:  # native 幂等：同键已受理 → 409 重复单（§2.2）
            return httpx.Response(
                409,
                json={"error": "DUPLICATE_ORDER", "message": f"同键已受理: {existing['receipt_no']}"},
            )
        self._seq += 1
        payload = json.loads(request.content)
        order = {
            "receipt_no": f"ORD-{self._seq:06d}",
            "status": ORDER_CREATED,
            "occurred_at": OCCURRED_AT,
            "idempotency_key": key,
            "action_iri": payload.get("action_iri"),
        }
        self.orders_by_key[key] = order
        return httpx.Response(201, json=dict(order))

    def _query_order(self, path: str) -> httpx.Response:
        key = path.removeprefix("/orders/by-key/")
        order = self.orders_by_key.get(key)
        if order is None:
            return httpx.Response(404, json={"error": "ORDER_NOT_FOUND", "message": key})
        return httpx.Response(200, json=dict(order))

    def _cancel_order(self, path: str) -> httpx.Response:
        receipt_no = path.removeprefix("/orders/").removesuffix("/cancel")
        for order in self.orders_by_key.values():
            if order["receipt_no"] == receipt_no:
                if order["status"] == ORDER_DONE:  # 终态 done 不可撤（§6.1 cancel_order 语义）
                    return httpx.Response(
                        409,
                        json={"error": "ORDER_TERMINAL", "message": f"工单已完结不可撤: {receipt_no}"},
                    )
                order["status"] = ORDER_CANCELLED
                return httpx.Response(
                    200, json={"receipt_no": receipt_no, "status": ORDER_CANCELLED, "occurred_at": OCCURRED_AT}
                )
        return httpx.Response(404, json={"error": "ORDER_NOT_FOUND", "message": receipt_no})


# ---------------------------------------------------------------- 参数化形态装配（mock / http）


def _mock_stack() -> tuple[MockPowerTicketAdapter, ConnectorMeta, MockPowerTicketAdapter]:
    """Mock 形态：控制面复用 adapter 观测面（advance 直推状态机）。"""
    adapter = MockPowerTicketAdapter()
    return adapter, mock_power_ticket_meta(), adapter


def _http_stack() -> tuple[HttpPowerTicketAdapter, ConnectorMeta, _FakeBizServer]:
    """HTTP 形态：MockTransport 客户端注入（零真网）；控制面 = fake 业务系统脚本面。"""
    server = _FakeBizServer()
    client = httpx.AsyncClient(transport=httpx.MockTransport(server.handle))
    adapter = HttpPowerTicketAdapter(base_url="http://biz.power.test", api_token="test-token", client=client)
    return adapter, http_power_ticket_meta(), server


ADAPTER_STACKS = [_mock_stack, _http_stack]


def _request(key: str = KEY) -> WritebackRequest:
    return WritebackRequest(
        idempotency_key=key,
        tenant_id=TENANT_ID,
        action_instance_id=uuid.uuid4(),
        action_iri=ACTION_IRI_CREATE_ORDER,
        params={"feeder": "F-101", "reason": "10kV 馈线接地"},
    )


def _compensation(receipt: WritebackReceipt) -> CompensationRequest:
    return CompensationRequest(
        idempotency_key=f"{KEY}:compensate",
        tenant_id=TENANT_ID,
        action_instance_id=uuid.uuid4(),
        action_iri=ACTION_IRI_CANCEL_ORDER,
        original_receipt=receipt.to_dict(),
        reason="契约验收撤销",
    )


# ---------------------------------------------------------------- 契约：元数据与注册（§5.2）


@pytest.mark.parametrize("adapter_factory", ADAPTER_STACKS, ids=["mock", "http"])
def test_元数据合法_绑定IRI非空_注册后按IRI解析命中(adapter_factory):
    adapter, meta, _ = adapter_factory()

    assert meta.action_iris  # §5.2 第 1 项：绑定行动类 IRI 非空
    assert isinstance(adapter, BizSystemAdapter)  # 协议结构满足（runtime_checkable 方法面）

    registry = ConnectorRegistry()
    registry.register(adapter, meta)

    for iri in meta.action_iris:  # 注册后 resolve 命中：IRI → 绑定（adapter+meta 原样）
        binding = registry.resolve(iri)
        assert binding is not None
        assert binding.adapter is adapter and binding.meta is meta


# ---------------------------------------------------------------- 契约：execute 受理凭证三要素（§2.3）


@pytest.mark.parametrize("adapter_factory", ADAPTER_STACKS, ids=["mock", "http"])
async def test_受理凭证三要素_受理号非空_幂等键逐字回显_时间戳可解析(adapter_factory):
    adapter, _, _ = adapter_factory()

    receipt = await adapter.execute(_request())

    assert receipt.accepted is True
    assert receipt.receipt_no  # 受理号非空（无凭证的成功一律视为 unknown 的反面）
    assert receipt.idempotency_key == KEY  # 键回显（逐字）
    datetime.fromisoformat(receipt.occurred_at)  # 业务侧受理时间戳可解析


# ---------------------------------------------------------------- 契约：幂等去重（§2.2 native）


@pytest.mark.parametrize("adapter_factory", ADAPTER_STACKS, ids=["mock", "http"])
async def test_同键重复投递_返回同一受理结果(adapter_factory):
    adapter, _, _ = adapter_factory()

    first = await adapter.execute(_request())
    replay = await adapter.execute(_request())  # HTTP 形态：业务侧 409 重复单 → 连接器转查单回归

    assert replay.receipt_no == first.receipt_no  # 同键已受理 → 返回首次受理结果
    assert replay.idempotency_key == KEY


# ---------------------------------------------------------------- 契约：query_status 三值语义（§2.5/§4）


@pytest.mark.parametrize("adapter_factory", ADAPTER_STACKS, ids=["mock", "http"])
async def test_状态查询_在途不可定性(adapter_factory):
    adapter, _, _ = adapter_factory()
    receipt = await adapter.execute(_request())

    status = await adapter.query_status(KEY)

    assert status.status == ORDER_CREATED  # 业务侧原生态透传
    assert status.finished is False and status.success is None  # 在途：不能定性
    assert status.receipt_no == receipt.receipt_no


@pytest.mark.parametrize("adapter_factory", ADAPTER_STACKS, ids=["mock", "http"])
async def test_状态查询_done_终态成功(adapter_factory):
    adapter, _, biz = adapter_factory()
    receipt = await adapter.execute(_request())
    biz.advance(receipt.receipt_no, ORDER_DONE)

    status = await adapter.query_status(KEY)

    assert status.status == ORDER_DONE
    assert status.finished is True and status.success is True  # done = finished & success


@pytest.mark.parametrize("adapter_factory", ADAPTER_STACKS, ids=["mock", "http"])
async def test_状态查询_cancelled_终态失败(adapter_factory):
    adapter, _, biz = adapter_factory()
    receipt = await adapter.execute(_request())
    biz.advance(receipt.receipt_no, ORDER_CANCELLED)

    status = await adapter.query_status(KEY)

    assert status.status == ORDER_CANCELLED
    assert status.finished is True and status.success is False  # cancelled = finished & 非 success


@pytest.mark.parametrize("adapter_factory", ADAPTER_STACKS, ids=["mock", "http"])
async def test_未受理键查询_三值unknown(adapter_factory):
    adapter, _, _ = adapter_factory()

    status = await adapter.query_status("no-such-key")

    assert status.status == "unknown"
    assert status.finished is None and status.success is None  # 三值语义：业务侧无法判定
    assert status.receipt_no is None


# ---------------------------------------------------------------- 契约：compensate 同契约（§3.2）


@pytest.mark.parametrize("adapter_factory", ADAPTER_STACKS, ids=["mock", "http"])
async def test_补偿撤单_补偿键逐字回显_业务侧置cancelled(adapter_factory):
    adapter, _, _ = adapter_factory()
    receipt = await adapter.execute(_request())

    comp = await adapter.compensate(_compensation(receipt))

    assert comp.accepted is True
    assert comp.idempotency_key == f"{KEY}:compensate"  # 补偿键「{原键}:compensate」逐字回显
    assert comp.receipt_no == receipt.receipt_no  # 补偿也回凭证（§3.2 同一契约）
    status = await adapter.query_status(KEY)  # 经公共契约面核实业务侧状态
    assert status.status == ORDER_CANCELLED and status.success is False


@pytest.mark.parametrize("adapter_factory", ADAPTER_STACKS, ids=["mock", "http"])
async def test_补偿终态done_业务失败码不可重试(adapter_factory):
    adapter, _, biz = adapter_factory()
    receipt = await adapter.execute(_request())
    biz.advance(receipt.receipt_no, ORDER_DONE)

    with pytest.raises(AdapterError) as exc:
        await adapter.compensate(_compensation(receipt))

    assert exc.value.code == "ORDER_TERMINAL"  # 两形态同一业务失败码词汇
    assert exc.value.code not in WritebackPolicy.retryable_codes  # 重试与否归 dispatcher 裁决


# ---------------------------------------------------------------- 契约：check_health（§5.1）


@pytest.mark.parametrize("adapter_factory", ADAPTER_STACKS, ids=["mock", "http"])
async def test_健康预检_返回布尔报告(adapter_factory):
    adapter, _, _ = adapter_factory()

    health = await adapter.check_health()

    assert isinstance(health.ok, bool) and health.ok is True


# ---------------------------------------------------------------- HTTP 形态：响应/异常 → 契约错误码（§5.3）


async def test_http_限流429_映射可重试码RATE_LIMITED() -> None:
    adapter, _, server = _http_stack()
    server.fail_next.append((429, {"error": "RATE_LIMITED"}))

    with pytest.raises(AdapterError) as exc:
        await adapter.execute(_request())

    assert exc.value.code == "RATE_LIMITED"  # §5.3 第 4 项：限流映射可重试码
    assert exc.value.code in WritebackPolicy.retryable_codes


async def test_http_服务端5xx_映射临时不可用可重试码() -> None:
    adapter, _, server = _http_stack()
    server.fail_next.append((503, {"error": "MAINTENANCE"}))

    with pytest.raises(AdapterError) as exc:
        await adapter.execute(_request())

    assert exc.value.code == "TEMP_UNAVAILABLE"  # 与 WritebackPolicy.retryable_codes 同词汇
    assert exc.value.code in WritebackPolicy.retryable_codes


async def test_http_脏数据4xx_映射不可重试VALIDATION_ERROR() -> None:
    adapter, _, server = _http_stack()
    server.fail_next.append((400, {"error": "BAD_PARAMS"}))

    with pytest.raises(AdapterError) as exc:
        await adapter.execute(_request())

    assert exc.value.code == "VALIDATION_ERROR"  # §5.3 第 2 项：4xx 脏数据语义=不可重试
    assert exc.value.code not in WritebackPolicy.retryable_codes


async def test_http_超时_抛TimeoutError对齐unknown裁决() -> None:
    """§2.5 超时=受理未知态：连接器抛 TimeoutError（dispatcher wait_for 分支裁决 unknown）；
    AdapterError("TIMEOUT") 会落 mark_failed 终态，违背红线（ocr 评审 high 项）。"""
    adapter, _, server = _http_stack()
    server.timeout_next = 1

    with pytest.raises(TimeoutError) as exc:
        await adapter.execute(_request())

    assert "受理未知态" in str(exc.value)


async def test_http_重复单409_按幂等语义转查单回执() -> None:
    adapter, _, server = _http_stack()
    first = await adapter.execute(_request())
    server.fail_next.append((409, {"error": "DUPLICATE_ORDER", "message": "同键已受理"}))

    replay = await adapter.execute(_request())  # 409 → 转查单 → 首次受理结果

    assert replay.receipt_no == first.receipt_no
    assert replay.idempotency_key == KEY


async def test_http_健康检查宕机_ok为False不抛错() -> None:
    adapter, _, server = _http_stack()
    server.fail_next.append((503, {"error": "MAINTENANCE"}))

    health = await adapter.check_health()

    assert health.ok is False  # 探测面语义：只报布尔，不抛错


# ---------------------------------------------------------------- 注册表：两连接器装配 + 冲突 fail-fast（§5.2）


def test_注册表_两连接器不同connector_id_按各自IRI解析命中() -> None:
    mock_adapter, _, _ = _mock_stack()
    http_adapter, _, _ = _http_stack()
    registry = ConnectorRegistry()
    registry.register(mock_adapter, mock_power_ticket_meta(action_iris=frozenset({ACTION_IRI_CREATE_ORDER})))
    registry.register(http_adapter, http_power_ticket_meta(action_iris=frozenset({ACTION_IRI_CANCEL_ORDER})))

    assert registry.resolve(ACTION_IRI_CREATE_ORDER).adapter is mock_adapter  # type: ignore[union-attr]
    assert registry.resolve(ACTION_IRI_CANCEL_ORDER).adapter is http_adapter  # type: ignore[union-attr]
    assert len(registry.bindings()) == 2  # 不同 connector_id：两连接器共存


def test_注册表_IRI冲突_fail_fast() -> None:
    mock_adapter, mock_meta, _ = _mock_stack()
    http_adapter, _, _ = _http_stack()
    registry = ConnectorRegistry()
    registry.register(mock_adapter, mock_meta)

    with pytest.raises(ValueError, match="行动类 IRI 已绑定"):
        registry.register(http_adapter, http_power_ticket_meta(action_iris=frozenset({ACTION_IRI_CREATE_ORDER})))


def test_注册表_connector_id冲突_fail_fast() -> None:
    adapter, meta, _ = _http_stack()
    registry = ConnectorRegistry()
    registry.register(adapter, meta)

    with pytest.raises(ValueError, match="连接器已注册"):
        registry.register(adapter, meta)  # 同 connector_id 二次注册即拒

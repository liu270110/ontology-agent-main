"""cli.client 单测（信封=api/01 §3.1、错误体=§4.1、trace_id=§3.3；全程 httpx.MockTransport，禁真连）。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from cli.client import ApiClient, ApiError, build_timeout, unwrap_envelope
from cli.config import CliConfig

ENDPOINT = "http://gw.test/api/v1"


def _make_client(handler: Any, **kwargs: Any) -> ApiClient:
    config = CliConfig(endpoint=ENDPOINT, timeout_seconds=5.0)
    return ApiClient(config, transport=httpx.MockTransport(handler), **kwargs)


def test_信封解析_恰含data_meta双键时解包并返回meta():
    # Arrange：网关按 api/01 §3.1 返回包裹信封
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"run_id": "r1"}, "meta": {"page": 1}})

    client = _make_client(handler)
    # Act
    data, meta = client.request("GET", "/sessions")
    # Assert：data/meta 拆开返回
    assert data == {"run_id": "r1"}
    assert meta == {"page": 1}


def test_信封解析_未包裹体原样作为data且meta为空():
    # Arrange：当前后端实现普遍未包裹（对账行动清单在途）
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"id": "s1"}], "offset": 0, "limit": 20})

    client = _make_client(handler)
    # Act
    data, meta = client.request("GET", "/sessions")
    # Assert：宽容两态——整体作 data
    assert data == {"items": [{"id": "s1"}], "offset": 0, "limit": 20}
    assert meta == {}


def test_信封解析_meta非对象时归一为空字典():
    # Arrange / Act
    data, meta = unwrap_envelope({"data": [1], "meta": "dirty"})
    # Assert
    assert data == [1]
    assert meta == {}


def test_错误体四字段透出到ApiError():
    # Arrange：api/01 §4.1 固定四字段错误体
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"code": 1003, "message": "令牌已过期", "detail": {"exp": 1795900000}, "trace_id": "req_01J"}
        )

    client = _make_client(handler)
    # Act / Assert
    with pytest.raises(ApiError) as exc_info:
        client.request("GET", "/sessions")
    assert exc_info.value.code == 1003
    assert exc_info.value.http_status == 401
    assert exc_info.value.trace_id == "req_01J"
    assert exc_info.value.detail == {"exp": 1795900000}
    assert "令牌已过期" in str(exc_info.value)


def test_错误体非JSON时降级为HTTP状态与文本摘要():
    # Arrange
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream boom")

    client = _make_client(handler)
    # Act / Assert
    with pytest.raises(ApiError) as exc_info:
        client.request("GET", "/sessions")
    assert exc_info.value.http_status == 500
    assert exc_info.value.code is None
    assert "upstream boom" in str(exc_info.value)


def test_trace_id透传_请求头携带X_Request_ID():
    # Arrange
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["trace"] = request.headers.get("x-request-id")
        return httpx.Response(200, json={"data": {}, "meta": {}})

    client = _make_client(handler, trace_id="req_abc")
    # Act
    client.request("GET", "/healthz", auth=False)
    # Assert：api/01 §3.3 X-Request-ID 透传
    assert seen["trace"] == "req_abc"


def test_鉴权头按需携带_登录态带Bearer匿名不带():
    # Arrange
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"data": {}, "meta": {}})

    config = CliConfig(endpoint=ENDPOINT, timeout_seconds=5.0, access_token="tok-123")
    client = ApiClient(config, transport=httpx.MockTransport(handler))
    # Act
    client.request("GET", "/sessions")
    client.request("GET", "/healthz", auth=False)
    # Assert：业务请求带 Bearer；匿名探针不带（防过期令牌污染匿名白名单语义）
    assert seen[0] == "Bearer tok-123"
    assert seen[1] is None


def test_网络不可达归为http_status_0并可判网络族():
    # Arrange
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _make_client(handler)
    # Act / Assert
    with pytest.raises(ApiError) as exc_info:
        client.request("GET", "/sessions")
    assert exc_info.value.http_status == 0
    assert exc_info.value.is_network_error


def test_超时必设_connect封顶_SSE读超时放开():
    # Act / Assert：普通请求 connect 封顶 5s、read=配置值；SSE read 放开靠心跳保活（api/02 §2）
    normal = build_timeout(30.0)
    assert normal.connect == 5.0
    assert normal.read == 30.0
    sse = build_timeout(30.0, sse=True)
    assert sse.read is None
    assert sse.connect == 5.0

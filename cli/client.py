"""网关 REST 客户端薄层（cli 只经网关：仅 HTTP，禁 import services/，架构锚点 §2 分层纪律）。

- 信封（api/01 §3.1）：成功判定=HTTP 2xx（禁 code===0 风格）；恰为 {data, meta} 双键才解包，
  否则整体作 data——当前后端实现普遍未包裹（2026-09-27 对账行动清单在途），客户端宽容两态；
- 错误体（api/01 §4.1）：{code, message, detail, trace_id} 四字段透出 ApiError，由 app 层写
  stderr；令牌永不进日志/异常；
- trace_id（api/01 §3.3）：--trace-id → X-Request-ID 请求头，网关回显并贯穿全链审计；
- 超时必设：connect 封顶 5s；SSE 流读超时放开（心跳 15s 保活，api/02 §2），避免长生成被掐断。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from cli.config import CliConfig
from cli.sse import SseFrame, SseFrameParser

SSE_MEDIA_TYPE = "text/event-stream"


class ApiError(Exception):
    """网关错误（HTTP 非 2xx / 网络不可达）；字段与错误体四字段对齐（api/01 §4.1）。"""

    def __init__(
        self,
        message: str,
        *,
        http_status: int = 0,
        code: int | None = None,
        detail: Any = None,
        trace_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.detail = detail
        self.trace_id = trace_id

    @property
    def is_network_error(self) -> bool:
        """http_status=0 表示请求未达网关（连接失败/超时）。"""
        return self.http_status == 0


def build_timeout(timeout_seconds: float, *, sse: bool = False) -> httpx.Timeout:
    """超时必设：connect 封顶 5s；SSE 读超时置 None（心跳 15s 保活，api/02 §2）。"""
    return httpx.Timeout(
        timeout_seconds,
        connect=min(5.0, timeout_seconds),
        read=None if sse else timeout_seconds,
    )


def unwrap_envelope(body: Any) -> tuple[Any, dict[str, Any]]:
    """{data, meta} 信封解包（宽容两态：恰双键才解包；未包裹体整体作 data、meta 置空）。"""
    if isinstance(body, dict) and set(body.keys()) == {"data", "meta"}:
        meta = body["meta"]
        return body["data"], meta if isinstance(meta, dict) else {}
    return body, {}


class ApiClient:
    """同步 httpx 客户端（CLI 终端形态无需事件循环；SSE 流经 httpx.Client.stream）。"""

    def __init__(
        self,
        config: CliConfig,
        *,
        trace_id: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self.timeout = build_timeout(config.timeout_seconds)
        headers: dict[str, str] = {}
        if trace_id:
            headers["X-Request-ID"] = trace_id
        self._client = httpx.Client(
            base_url=config.endpoint,
            timeout=self.timeout,
            headers=headers,
            transport=transport,
            follow_redirects=True,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> tuple[Any, dict[str, Any]]:
        """同步请求：返回 (data, meta)；非 2xx / 网络异常抛 ApiError。"""
        try:
            response = self._client.request(
                method, path, json=json_body, params=params, headers=self._auth_headers(auth=auth)
            )
        except httpx.TimeoutException as exc:
            raise ApiError(f"网关请求超时（>{self.config.timeout_seconds:g}s）：{type(exc).__name__}") from exc
        except httpx.TransportError as exc:
            raise ApiError(f"无法连接网关 {self.config.endpoint}：{type(exc).__name__}") from exc
        return self._resolve(response)

    def stream_sse(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        auth: bool = True,
    ) -> Iterator[SseFrame]:
        """SSE 流式请求：Accept: text/event-stream，逐帧产出；非 2xx 抛 ApiError。"""
        headers = {"Accept": SSE_MEDIA_TYPE, **self._auth_headers(auth=auth)}
        try:
            with self._client.stream(
                method,
                path,
                json=json_body,
                headers=headers,
                timeout=build_timeout(self.config.timeout_seconds, sse=True),
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise self._to_api_error(response)
                parser = SseFrameParser()
                for chunk in response.iter_bytes():
                    yield from parser.feed(chunk)
                yield from parser.flush()
        except httpx.TimeoutException as exc:
            raise ApiError(f"SSE 流读取超时：{type(exc).__name__}") from exc
        except httpx.TransportError as exc:
            raise ApiError(f"SSE 流连接中断：{type(exc).__name__}") from exc

    def _auth_headers(self, *, auth: bool) -> dict[str, str]:
        if auth and self.config.access_token:
            return {"Authorization": f"Bearer {self.config.access_token}"}
        return {}

    def _resolve(self, response: httpx.Response) -> tuple[Any, dict[str, Any]]:
        if response.is_error:
            raise self._to_api_error(response)
        if response.status_code == 204 or not response.content:
            return None, {}
        data, meta = unwrap_envelope(self._parse_json(response))
        return data, meta

    def _parse_json(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            snippet = response.text[:200].replace("\n", " ")
            raise ApiError(f"网关返回非 JSON 响应（HTTP {response.status_code}）：{snippet}") from None

    def _to_api_error(self, response: httpx.Response) -> ApiError:
        """错误体四字段解析（api/01 §4.1）；非 JSON 错误体降级为状态码+文本摘要。"""
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            code = body.get("code")
            trace_id = body.get("trace_id")
            return ApiError(
                str(body.get("message", f"HTTP {response.status_code}")),
                http_status=response.status_code,
                code=int(code) if isinstance(code, int) else None,
                detail=body.get("detail"),
                trace_id=str(trace_id) if trace_id is not None else None,
            )
        snippet = response.text[:200].replace("\n", " ")
        return ApiError(f"HTTP {response.status_code}：{snippet}", http_status=response.status_code)

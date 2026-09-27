"""A2A 独立 HTTP 应用（docs/api/04 §2/§4：发现端点 + JSON-RPC 任务端点）。

- ``GET /.well-known/agent-card.json``：Agent Card 发现（无需鉴权，api/04 §2）；
- ``POST /a2a``：JSON-RPC 2.0 任务端点（鉴权：API Key Bearer 起步；app 错误按 JSON-RPC
  惯例以 HTTP 200 + error 对象应答；鉴权失败以 HTTP 401 + 平台统一错误体应答——
  1001/1002 属传输层凭据问题，不进 JSON-RPC 信封）。

组合根：build_a2a_app 由独立入口（services/mcp/a2a/__main__）装配；与 MCP 出口同构的
扩展位=网关同进程挂载（api/04 §7「同进程不同前缀 or 独立监听」待 M5 裁决，本批独立监听）。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from services.mcp.a2a.auth import A2aAuthError, ApiKeyAuthorizer
from services.mcp.a2a.card import AgentCard
from services.mcp.a2a.jsonrpc import DEFAULT_TIMEOUT_S, dispatch_jsonrpc, new_trace_id
from services.mcp.a2a.service import A2aExecutor, A2aResultStore, A2aService

logger = logging.getLogger("services.mcp.a2a")


def _unified_error(code: int, message: str, status_code: int, trace_id: str, detail: Any = None) -> JSONResponse:
    """平台统一错误体（02 §7 四字段；鉴权失败等传输层错误用）。"""
    return JSONResponse(
        {"code": code, "message": message, "detail": detail, "trace_id": trace_id},
        status_code=status_code,
    )


def build_a2a_app(
    *,
    card: AgentCard,
    service: A2aService,
    authorizer: ApiKeyAuthorizer,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    executor: A2aExecutor | None = None,
    result_store: A2aResultStore | None = None,
    cancel_hook: Callable[[uuid.UUID], bool] | None = None,
) -> FastAPI:
    """A2A 应用工厂：发现 + 任务端点（fastapi 独立监听，uvicorn 承载）。

    executor 接线（M5 登记缓议项）：组合根经本工厂参数注入委托执行缝（A2aTaskExecutor）
    ——工厂 build 时经 ``service.attach_executor`` 一次接线（执行器 + 结果存储 + 取消传播
    钩子）；未注入=裸受理形态（M5-2 行为，任务保持 working）。"""
    if executor is not None:
        service.attach_executor(executor=executor, result_store=result_store, cancel_hook=cancel_hook)
    app = FastAPI(title="ontology-agent-a2a", version=card.protocol_version, docs_url=None, redoc_url=None)

    @app.get("/.well-known/agent-card.json", summary="Agent Card 发现（无需鉴权，api/04 §2）")
    async def agent_card() -> dict[str, Any]:
        return card.to_wire()

    @app.post("/a2a", summary="A2A 任务端点（JSON-RPC 2.0：message/send、tasks/get、tasks/cancel）")
    async def a2a_endpoint(request: Request) -> JSONResponse:
        trace_id = new_trace_id()
        try:
            scopes = authorizer.authenticate(request.headers.get("authorization"))
        except A2aAuthError as exc:
            return _unified_error(exc.code, exc.message, 401, trace_id)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 ——非 JSON 体 → -32600 非法请求对象
            payload = None
        response = await dispatch_jsonrpc(payload, service, scopes, timeout_s=timeout_s)
        return JSONResponse(response)

    return app

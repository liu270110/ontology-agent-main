"""A2A JSON-RPC 2.0 信封与方法分派（docs/api/04 §4 方法集的平台行为映射）。

方法集（api/04 §4 表）：
- ``message/send``  → A2aService.message_send（受理，返回 submitted 受理凭证）；
- ``tasks/get``     → A2aService.tasks_get（状态回查询）；
- ``tasks/cancel``  → A2aService.tasks_cancel（取消未终态任务）；
- ``message/stream`` / ``tasks/resubscribe`` → 未启用（capabilities.streaming=false，
  api/04 §7 随 M5 裁决），按 -32601 应答并附未启用说明。

错误映射：协议错误用 JSON-RPC 保留码（-32700/-32600/-32601/-32602/-32603）；
应用错误统一 -32000 + data 四字段（与 REST 统一错误体 {code, message, detail, trace_id} 同构，
code=02 §7 已登记码）。超时纪律：分派层 asyncio.wait_for 统一强制（异步+超时必设）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from services.mcp.a2a.errors import A2aAppError
from services.mcp.a2a.service import A2aService
from services.mcp.errors import McpToolError
from services.platform.errors import ErrorCode

JSONRPC_VERSION = "2.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
JSONRPC_APP_ERROR = -32000  # 应用错误保留位：data.code=平台已登记错误码

SUPPORTED_METHODS = ("message/send", "tasks/get", "tasks/cancel")
UNSUPPORTED_METHODS = ("message/stream", "tasks/resubscribe")

DEFAULT_TIMEOUT_S = 15.0  # 单请求处理超时（异步+超时必设）

# JSON-RPC 方法处理器形状（A2aService 服务方法的解绑引用；scopes 仅关键字——
# 解绑方法的 mypy 渲染含隐式 self，精确 Callable 难以表达，签名约束由调用点服务方法承担）
HandlerFn = Callable[..., Awaitable[dict[str, Any]]]


def error_response(request_id: Any, code: int, message: str, *, data: Any = None) -> dict[str, Any]:
    """JSON-RPC error 对象（2.0：{jsonrpc, id, error:{code, message, data}}）。"""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def result_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    """JSON-RPC result 对象（2.0：{jsonrpc, id, result}）。"""
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _app_data(code: int, message: str, detail: Any = None) -> dict[str, Any]:
    """应用错误 data 明细（REST 统一错误体四字段同构）。"""
    return {"code": code, "message": message, "detail": detail, "trace_id": None}


async def dispatch_jsonrpc(
    payload: Any,
    service: A2aService,
    scopes: tuple[str, ...],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """分派一个 JSON-RPC 请求（调用方已认证）；永不抛出——异常一律转 error 对象。"""
    if not isinstance(payload, dict):
        return error_response(None, INVALID_REQUEST, "请求须为 JSON-RPC 2.0 对象")
    request_id = payload.get("id")
    if payload.get("jsonrpc") != JSONRPC_VERSION or not isinstance(payload.get("method"), str):
        return error_response(request_id, INVALID_REQUEST, "jsonrpc 版本须为 2.0 且 method 必须为字符串")
    method = payload["method"]
    params = payload.get("params")
    if params is not None and not isinstance(params, dict):
        return error_response(request_id, INVALID_PARAMS, "params 须为对象")
    handler = _HANDLERS.get(method)
    if handler is None:
        note = "（SSE 流式未启用，capabilities.streaming=false）" if method in UNSUPPORTED_METHODS else ""
        return error_response(request_id, METHOD_NOT_FOUND, f"未知方法: {method}{note}")
    try:
        result = await asyncio.wait_for(handler(service, params or {}, scopes=scopes), timeout=timeout_s)
        return result_response(request_id, result)
    except A2aAppError as exc:
        return error_response(
            request_id, JSONRPC_APP_ERROR, exc.message, data=_app_data(exc.code, exc.message, exc.detail)
        )
    except McpToolError as exc:
        return error_response(request_id, JSONRPC_APP_ERROR, exc.message, data=exc.payload())
    except (KeyError, ValueError, TypeError) as exc:
        return error_response(
            request_id, INVALID_PARAMS, str(exc), data=_app_data(int(ErrorCode.PARAM_INVALID), str(exc))
        )
    except TimeoutError:
        return error_response(
            request_id, JSONRPC_APP_ERROR, "处理超时", data=_app_data(int(ErrorCode.MCP_TARGET_UNAVAILABLE), "处理超时")
        )
    except Exception as exc:  # noqa: BLE001 ——兜底禁止裸 500（api/03 §8 同款未分类 → 5999）
        return error_response(
            request_id, INTERNAL_ERROR, f"内部错误: {exc}", data=_app_data(int(ErrorCode.INTERNAL_ERROR), str(exc))
        )


def _route(fn: HandlerFn) -> HandlerFn:
    return fn


_HANDLERS: dict[str, HandlerFn] = {
    "message/send": _route(A2aService.message_send),
    "tasks/get": _route(A2aService.tasks_get),
    "tasks/cancel": _route(A2aService.tasks_cancel),
}


def new_trace_id() -> str:
    """请求级 trace_id（app 层无网关中间件时自生成；X-Request-ID 接入随 M5+）。"""
    return uuid.uuid4().hex

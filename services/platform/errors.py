"""平台 HTTP 内核：统一错误码/统一错误体/GatewayError/trace 上下文（02 §3/§7）。

2026-09-27 模块轴重构：错误契约是 gateway 中间件与各模块 api 的共享面，归 platform 底座；
HTTP 中间件链本体仍在 services.gateway.middlewares（re-export 兼容旧 import）。
"""

from __future__ import annotations

from contextvars import ContextVar
from enum import IntEnum

from starlette.requests import Request
from starlette.responses import JSONResponse

# ---------------------------------------------------------------- 统一错误码（02 §7 全表登记）


class ErrorCode(IntEnum):
    """02 §7 / api/01 §4.3 已登记错误码（新增必须先回 02 §7 登记）。"""

    TOKEN_MISSING = 1001
    TOKEN_INVALID = 1002
    TOKEN_EXPIRED = 1003
    SCOPE_INSUFFICIENT = 2001
    ROLE_FORBIDDEN = 2002
    TENANT_MISMATCH = 2003
    TENANT_DISABLED = 2004
    RATE_LIMITED = 2005
    PARAM_INVALID = 3001
    BODY_MALFORMED = 3002
    VERSION_CONFLICT = 3003
    UNSUPPORTED_MEDIA_TYPE = 3004
    SESSION_CLOSED = 4101
    TASK_ALREADY_RUNNING = 4102
    TOOL_BUSY = 4103  # 2026-09-26 缺口核查修复补登记（41xx session 段）
    VERSION_IMMUTABLE = 4201
    SSE_REPLAY_EXPIRED = 4301
    OBJECT_ALREADY_IN_REVIEW = 4701
    LLM_TIMEOUT = 5001
    LLM_UNAVAILABLE = 5002
    MCP_TARGET_UNAVAILABLE = 5003
    STORAGE_UNAVAILABLE = 5004
    RETRY_BUDGET_EXHAUSTED = 5005  # 2026-09-26 缺口核查修复补登记（5xxx 段）
    # 2026-09-29 H-0c 批登记（孤儿 Run 回收：running 悬挂超时→对账回收；02 §7 表格回填随文档批）
    ORPHAN_RUN_RECOVERED = 5006
    # 2026-10-03 B-② 批登记（中断账本合成闭合：崩溃恢复对撕裂投影合成 close/步终态的中断语义码；
    # 既有族无「工具被中断」码——4101 是用户取消清单口径，5003 是工具超时口径，均不复用；
    # 依据 docs/Agent/10 §4.1 ②，02 §7 表格回填随文档批）
    TOOL_CALL_INTERRUPTED = 5007
    INTERNAL_ERROR = 5999


# ---------------------------------------------------------------- 统一错误体与网关异常


class GatewayError(Exception):
    """网关层业务异常：路由/依赖抛出，由全局异常中间件转统一错误体（02 §7）。"""

    def __init__(
        self,
        code: int | ErrorCode,
        message: str,
        *,
        status_code: int = 400,
        detail: object = None,
    ) -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = message
        self.status_code = status_code
        self.detail = detail


def error_response(
    request: Request,
    code: int | ErrorCode,
    message: str,
    *,
    status_code: int = 400,
    detail: object = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """02 §3 约定：③④⑤⑦ 共用同一构造函数，错误体四字段全局同构。"""
    body = {
        "code": int(code),
        "message": message,
        "detail": detail,
        "trace_id": getattr(request.state, "trace_id", None),
    }
    return JSONResponse(body, status_code=status_code, headers=headers)


# ---------------------------------------------------------------- trace 上下文（02 §3 ② / 08 §1）

trace_id_ctx: ContextVar[str | None] = ContextVar("trace_id", default=None)
tenant_id_ctx: ContextVar[str | None] = ContextVar("tenant_id", default=None)

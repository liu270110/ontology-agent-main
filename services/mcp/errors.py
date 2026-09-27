"""MCP 出口错误映射（docs/api/03 §8：工具执行层 isError + 平台错误码同构错误体）。

契约：工具执行层错误体四字段 ``{code, message, trace_id, detail}`` 与 REST 统一错误体同构
（api/03 §8 末行）；fastmcp 2.12 的 ToolError 仅承载文本消息，故错误体以 JSON 文本随
``isError=true`` 返回（版本限制登记于报告，FastMCP 支持结构化错误后平移）。

码表纪律：code 一律取 02 §7 已登记码（platform.errors.ErrorCode），禁新编——
3001 参数 / 2001 scope 不足 / 42xx 本体业务 / 5002 LLM 不可用 / 5003 外部 MCP 目标不可用 /
5004 存储降级 / 5999 未分类内部错误。唯一例外=writeback.status 未找到的 404 语义
（api/03 §2 显式登记；与 REST 层 GatewayError(404) 同族的 HTTP 状态码对齐语义）。
"""

from __future__ import annotations

import json
from typing import Any

from services.platform.errors import ErrorCode, GatewayError
from services.platform.ports.capability_provider import CapabilityError
from services.platform.ports.model_port import ModelPortError


class McpToolError(Exception):
    """MCP 工具执行错误：携带与 REST 同构的四字段错误体（api/03 §8）。"""

    def __init__(
        self,
        code: int | ErrorCode,
        message: str,
        *,
        detail: Any = None,
        trace_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = message
        self.detail = detail
        self.trace_id = trace_id

    def payload(self) -> dict[str, Any]:
        """同构错误体（code/message/trace_id/detail 四字段，REST 错误体同形）。"""
        return {"code": self.code, "message": self.message, "detail": self.detail, "trace_id": self.trace_id}

    def wire(self) -> str:
        """ToolError 消息线格式（JSON 文本；fastmcp 2.12 结构化错误限制见模块 docstring）。"""
        return json.dumps(self.payload(), ensure_ascii=False)


def map_exception(exc: Exception, *, trace_id: str | None) -> McpToolError:
    """领域/端口异常 → McpToolError（api/03 §8 映射表；未知异常 5999 兜底，禁裸 500）。"""
    if isinstance(exc, McpToolError):
        if exc.trace_id is None:
            return McpToolError(exc.code, exc.message, detail=exc.detail, trace_id=trace_id)
        return exc
    if isinstance(exc, GatewayError):
        return McpToolError(exc.code, exc.message, detail=exc.detail, trace_id=trace_id)
    if isinstance(exc, CapabilityError):
        return McpToolError(exc.code, exc.message, trace_id=trace_id)
    if isinstance(exc, ModelPortError):
        # ModelPortError.message 已带 "5001 LLM_TIMEOUT: ..." 前缀，取 code 对齐即可
        return McpToolError(exc.code, str(exc), trace_id=trace_id)
    if isinstance(exc, TimeoutError):  # asyncio.TimeoutError 于 3.11+ 即内建 TimeoutError
        return McpToolError(ErrorCode.MCP_TARGET_UNAVAILABLE, "上游调用超时", trace_id=trace_id)
    if isinstance(exc, ValueError):
        return McpToolError(ErrorCode.PARAM_INVALID, str(exc), trace_id=trace_id)
    return McpToolError(ErrorCode.INTERNAL_ERROR, f"内部错误: {exc}", trace_id=trace_id)

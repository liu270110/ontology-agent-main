"""A2A 应用错误（api/04 §5/§8 与 02 §7 错误码纪律的 JSON-RPC 承载形）。

错误码禁新编：``code`` 一律取 02 §7 已登记码（platform.errors.ErrorCode）或 api/03 §3.9
同款 404 未找到语义；JSON-RPC 层统一映射为 -32000 应用错误 + data 四字段明细
（与 REST 统一错误体 {code, message, detail, trace_id} 同构）。
"""

from __future__ import annotations

from typing import Any


class A2aAppError(Exception):
    """A2A 应用错误：携带平台已登记错误码与同构明细（jsonrpc 层转 -32000 error 对象）。"""

    def __init__(self, code: int, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = message
        self.detail = detail

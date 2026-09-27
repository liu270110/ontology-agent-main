"""A2A 鉴权（docs/api/04 §5 的 M5-2 收缩：API Key 起步，OAuth 2.0/OIDC 委托随 M5+）。

契约（api/04 §5）：委托方按 Card ``authentication`` 声明取凭据访问 ``/a2a``；平台侧映射为
调用身份并绑定 tenant 与 scopes——本批映射为独立入口显式授予的 (key → scopes) 绑定
（services/mcp/__main__ ``--anonymous-scopes`` 同款收缩形态），PDP 第 3 步判定复用
platform.security.authorize（精确匹配、deny-by-default）。

安全纪律：key 只存 sha256 摘要（内存），校验走 hmac.compare_digest 常量时间比较；
缺失凭据=1001 TOKEN_MISSING、不匹配=1002 TOKEN_INVALID（02 §7 已登记码，错误码禁新编）。
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from collections.abc import Mapping

from services.mcp.a2a.errors import A2aAppError
from services.platform.errors import ErrorCode
from services.platform.security import authorize


class A2aAuthError(Exception):
    """鉴权失败（携带平台已登记错误码：1001/1002；app 层转 401 统一错误体）。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = message


def _key_digest(raw_key: str) -> bytes:
    return hashlib.sha256(raw_key.encode("utf-8")).digest()


class ApiKeyAuthorizer:
    """API Key → (scopes) 绑定解析器：认证（谁是调用方）+ 授权面装载（PDP 判定的输入）。"""

    def __init__(self, keys: Mapping[str, tuple[str, ...]]) -> None:
        self._digests: dict[bytes, tuple[str, ...]] = {_key_digest(key): tuple(scopes) for key, scopes in keys.items()}

    def authenticate(self, authorization_header: str | None) -> tuple[str, ...]:
        """Bearer 凭据 → 调用方 scopes；失败抛 A2aAuthError（1001/1002）。"""
        if not authorization_header or not authorization_header.strip():
            raise A2aAuthError(ErrorCode.TOKEN_MISSING, "缺少 Authorization 凭据（api/04 §5）")
        scheme, _, token = authorization_header.strip().partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise A2aAuthError(ErrorCode.TOKEN_INVALID, "Authorization 形态须为 Bearer <api_key>")
        digest = _key_digest(token)
        for known, scopes in self._digests.items():
            if hmac.compare_digest(known, digest):
                return scopes
        raise A2aAuthError(ErrorCode.TOKEN_INVALID, "API Key 无效")


def check_scopes(scopes: tuple[str, ...], required: str, *, trace_id: str | None = None) -> None:
    """PDP 第 3 步动作判定（08 §2.5）：deny-by-default；不足 → 2001（A2aAppError 由 JSON-RPC 层转）。

    scope 清单不新编，复用 api/01 §5.2 登记集：委托=session:chat、查询=session:read、
    取消=session:write（与 REST 发消息/任务端点同族——A2A 与 REST 同一条编排链路的鉴权对齐）。
    """
    if not authorize(list(scopes), required):
        raise A2aAppError(
            ErrorCode.SCOPE_INSUFFICIENT,
            "scope 不足",
            detail={"required": [required], "granted": list(scopes), "trace_id": trace_id or uuid.uuid4().hex},
        )

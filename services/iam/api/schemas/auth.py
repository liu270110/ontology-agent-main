"""L2 网关 · auth DTO（api/01 §5.9；02 §6 DTO 规范：与领域模型严格分离）。

铁律：extra="forbid"、snake_case、只数据无行为；错误分支码随 api/01 §5.9（1002 凭据无效等）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    """POST /auth/login 请求体（api/01 §5.9；MFA 两段式为 2026-09-27 预登记，M1 不做）。"""

    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=256)
    password: str = Field(min_length=1, max_length=256)


class TokenPairResponse(BaseModel):
    """令牌对：access（2h）+ refresh（14d），claims 按 08 §2.1。"""

    model_config = ConfigDict(extra="forbid")
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # access 剩余秒数


class RefreshRequest(BaseModel):
    """POST /auth/refresh 请求体（匿名；body 携 refresh token，api/01 §5.9）。"""

    model_config = ConfigDict(extra="forbid")
    refresh_token: str = Field(min_length=1, max_length=4096)


class LogoutRequest(BaseModel):
    """POST /auth/logout 请求体：可选附 refresh token 一并吊销（api/01 §5.9）。"""

    model_config = ConfigDict(extra="forbid")
    refresh_token: str | None = Field(default=None, min_length=1, max_length=4096)

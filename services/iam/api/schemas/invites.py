"""L2 网关 · invites DTO（架构设计/32 §二；02 §6 DTO 规范：与领域模型严格分离）。

铁律：extra="forbid"、snake_case、只数据无行为；expires_in_hours Literal 与业务层
_ALLOWED_EXPIRES_HOURS 同口径（24/168/720）。预览/加入为匿名端点，错误分支 410+3410
不走本模块 DTO（统一错误体由全局异常中间件产出）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CreateInviteIn(BaseModel):
    """POST /invites 请求体（32 篇 §二：role + expires_in_hours 三档）。"""

    model_config = ConfigDict(extra="forbid")
    role: str = Field(min_length=1, max_length=32)  # Role.code（08 §2.2 英文码）
    expires_in_hours: Literal[24, 168, 720] = 24


class InviteCreatedOut(BaseModel):
    """201 生成响应（32 篇 §二：不回传 URL，base 由前端 location.origin 拼接）。"""

    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    token: str  # 明文 token 仅本次响应返回一次（DB 只存 sha256）
    role: str
    expires_at: datetime
    created_by: uuid.UUID
    status: str = "active"


class InviteItemOut(BaseModel):
    """列表项：status 为派生态（active/expired/revoked，撤销优先于过期）。"""

    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    role: str
    status: str
    expires_at: datetime
    created_by: uuid.UUID
    used_count: int
    created_at: datetime


class InviteListOut(BaseModel):
    """GET /invites 响应（列表信封 {items, next_cursor} 与 admin 列表端点同款口径）。"""

    model_config = ConfigDict(extra="forbid")
    items: list[InviteItemOut]
    next_cursor: None = None


class InviteRevokeOut(BaseModel):
    """DELETE /invites/{id} 200 信封体（禁 204：{id, status:"revoked"}，api-keys revoke 同款）。"""

    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    status: str


class InvitePreviewOut(BaseModel):
    """GET /invites/preview?token= 响应（匿名；登录页受邀提示条校验面）。"""

    model_config = ConfigDict(extra="forbid")
    tenant_name: str
    role: str
    valid: bool = True


class JoinIn(BaseModel):
    """POST /invites/join 请求体（匿名；token 固定走 body，32 篇 §二路径设计）。"""

    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=256)
    email: str = Field(min_length=3, max_length=256)
    display_name: str | None = Field(default=None, max_length=128)


class JoinOut(BaseModel):
    """POST /invites/join 响应（幂等：同租户既有账号原样返回 joined=true）。"""

    model_config = ConfigDict(extra="forbid")
    joined: bool
    tenant_name: str

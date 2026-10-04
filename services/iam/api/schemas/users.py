"""L2 网关 · admin/users DTO（api/01 §5.8 users CRUD；契约源=前端追认口径）。

形状权威=frontend/src/features/admin/api.ts `AdminUser` + mocks/admin-handlers.ts users 段
（前端已建成消费方，后端按其形状追认实现，B8-WA 切片）。铁律：extra="forbid"、
snake_case、只数据无行为（invites DTO 同款）。

已知形状差异（对齐 W-C，见 tests/gateway/test_admin_users.py 头注）：
- `department`：users 表无此列（迁移冻结不可动），本切片恒回占位 `"—"`（mock 对
  邮件邀请新建行的同款口径）；PATCH 接受该字段但暂不落库（防前端编辑弹窗必发
  department 被 extra=forbid 拒成 422），待 profile 存储实装后转真。
- `status` 三态中 `invited` 为 mock 形状（pending 邀请人无 users 行，真实数据仅
  active/disabled——users.status CHECK 约束），DTO 保留字面量对齐前端联合类型。
- `invited_via`/`invite_link_id`：mock 仅 invited 行携带；后端现状无 invited 行，
  以 None 缺省序列化（前端 `=== 'link'` 判定不受影响）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

UserStatus = Literal["active", "disabled", "invited"]


class AdminUserOut(BaseModel):
    """用户列表项 / 详情 / PATCH 响应（前端 AdminUser 逐字段对齐）。"""

    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    username: str  # DB 可空 → 空则回退 email local-part（invites.join 建号同款缺省）
    email: str
    display_name: str  # DB 可空 → 空则回退 username / email local-part
    roles: list[str]  # Role.code 数组（去重，按绑定创建序）
    department: str = "—"  # 形状差异：无存储列，占位（见模块 docstring）
    status: UserStatus  # 真实数据仅 active/disabled（invited 见模块 docstring）
    last_login_at: datetime | None = None
    invited_via: Literal["email", "link"] | None = None
    invite_link_id: str | None = None


class AdminUserListOut(BaseModel):
    """GET /admin/users 响应（信封 {items, next_cursor}；M1 全量列表，游标留空）。"""

    model_config = ConfigDict(extra="forbid")
    items: list[AdminUserOut]
    next_cursor: None = None


class AdminUserPatchIn(BaseModel):
    """PATCH /admin/users/{id} 请求体（前端 AdminUserPatch 逐字段对齐）。

    roles 为**全量替换**语义（角色码数组整体覆盖 user_roles 绑定）；department
    接受但暂不落库（形状差异见模块 docstring）；密码字段不存在——CRUD 不触碰
    password_hash（建号唯一路径=invites join）。
    """

    model_config = ConfigDict(extra="forbid")
    roles: list[str] | None = Field(default=None, max_length=16)
    department: str | None = Field(default=None, max_length=128)
    display_name: str | None = Field(default=None, max_length=128)
    status: Literal["active", "disabled"] | None = None  # 启停可逆出口（DELETE 软删的回程）


class AdminUserDisabledOut(BaseModel):
    """DELETE /admin/users/{id} 200 信封体（禁 204：{id, status:"disabled"}，
    invites 撤销 / api-keys revoke 同款非空体口径；幂等——已禁用重复删除同响应）。"""

    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    status: Literal["disabled"]

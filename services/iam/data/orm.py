"""iam 模块 ORM：identity 5 表 + audit_logs（08 §3 只追加审计）+ admin 域 4 表。

DDL 权威：database/01 §3.1/§3.9；admin 域 4 表（user_groups / role_permission_matrix /
model_channels / permission_requests）= 2026-10-05 admin 域批补录（迁移
20261005_c5e9a1d3b7f5，契约源=frontend mock admin-handlers.ts + api/01 §5.8/§5.10 预登记行，
database/01 表格补录随文档批）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin, TimestampMixin


class Tenant(Base, PkMixin, TimestampMixin):  # 平台级（豁免 tenant_id）
    __tablename__ = "tenants"
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    plan: Mapped[str] = mapped_column(String(32), default="free", nullable=False)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # 含 governance_tier（08 §2.4）
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    __table_args__ = (
        CheckConstraint("plan IN ('free','pro','ent')", name="ck_tenants_plan"),
        CheckConstraint("status IN ('active','suspended')", name="ck_tenants_status"),
    )


class User(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(256), nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uk_users_tenant_id_email"),
        CheckConstraint("status IN ('active','disabled')", name="ck_users_status"),  # 09-26 缺口修复（实落名同名）
    )


class Role(Base, PkMixin, TimestampMixin):  # 平台级；种子走 Alembic 数据迁移（08 §2.2）
    __tablename__ = "roles"
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)  # 08 §2.2 英文码权威
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)


class UserRole(Base, PkMixin, TenantMixin):
    __tablename__ = "user_roles"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uk_user_roles_user_id_role_id"),)


class ApiKey(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "api_keys"
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)  # owner scopes 子集
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 状态机 08 §2.6
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        # 2026-09-26 缺口核查修复：按租户+属主查键列表
        Index("ix_api_keys_tenant_owner", "tenant_id", "owner_user_id"),
    )


class Invite(Base, PkMixin, TenantMixin, TimestampMixin):  # 邀请链接（架构设计/32 §三）
    __tablename__ = "invite_links"
    # 安全红线：DB 只存 sha256(token) hex（64 字符），明文 token 仅在生成响应出现一次（32 篇 §一）
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)  # Role.code（08 §2.2 英文码权威）
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 撤销终态（不可逆）
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # M1 仅计数，不限人数


class AuditLog(Base, PkMixin, TenantMixin):  # 只追加；无 update/delete（08 §3）
    __tablename__ = "audit_logs"
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # 如 ontology.publish / action.invoke
    resource_type: Mapped[str | None] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    params_digest: Mapped[dict | None] = mapped_column(JSONB)  # 脱敏摘要（08 §3）
    result: Mapped[str] = mapped_column(String(16), default="success", nullable=False)
    ip: Mapped[str | None] = mapped_column(INET)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint("actor_type IN ('user','api_key','agent','system')", name="ck_audit_logs_actor_type"),
        Index("ix_audit_tenant_time", "tenant_id", "created_at"),
        Index("ix_audit_resource", "resource_type", "resource_id"),  # 2026-09-26 缺口核查修复（按资源查审计）
    )


# ================================================================
# admin 域 4 表（2026-10-05 批；契约源=mock admin-handlers.ts + api/01 §5.8/§5.10 预登记行）
# ================================================================


class UserGroup(Base, PkMixin, TenantMixin, TimestampMixin):
    """用户组（api/01 §5.10 GET/POST /admin/groups 预登记；RBAC 之上的批量授权单元，25 篇 F-10/X6）。

    members=M1 展示位（display_name 字符串数组，mock 契约同形；组内用户与角色的批量绑定
    解析随 X6 资源 ACL 批转真，本批不建 group_members 关联表——最小够用）。"""

    __tablename__ = "user_groups"
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    role_template: Mapped[str] = mapped_column(String(32), nullable=False)  # Role.code（08 §2.2 英文码）
    members: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uk_user_groups_tenant_id_name"),)


class RolePermissionMatrix(Base, PkMixin, TenantMixin, TimestampMixin):
    """角色-权限矩阵覆写行（api/01 §5.8 GET/PUT /admin/roles/matrix；08 §2.2 矩阵的管理面）。

    只存**覆写**：缺省布尔=代码内基线矩阵（mock MATRIX 常量同源，08 §2.2 快照）；
    GET=基线 ∪ 覆写合并投影，PUT=覆写 upsert（未知角色/权限点 422+3001）。"""

    __tablename__ = "role_permission_matrix"
    role_code: Mapped[str] = mapped_column(String(32), nullable=False)
    permission: Mapped[str] = mapped_column(String(64), nullable=False)  # 11 篇 §2 资源:动作 词汇
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "role_code", "permission", name="uk_role_matrix_tenant_role_permission"),
    )


class ModelChannel(Base, PkMixin, TenantMixin, TimestampMixin):
    """模型渠道（api/01 §5.8 models 族；LiteLLM 网关渠道登记）。

    安全红线（08 §2.0）：密钥类只存掩码串 api_key_masked，明文不落库；usage_30d 为读时
    从 llm_calls 聚合的展示串，不落列。"""

    __tablename__ = "model_channels"
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_label: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    models: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    api_key_masked: Mapped[str | None] = mapped_column(String(64))  # 掩码串；本地渠道 None
    priority: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    budget_daily: Mapped[int | None] = mapped_column(Integer)  # 日预算（元）
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    __table_args__ = (CheckConstraint("status IN ('active','disabled')", name="ck_model_channels_status"),)


class PermissionRequest(Base, PkMixin, TenantMixin, TimestampMixin):
    """权限申请单（api/01 §5.10 POST/GET /permission-requests 预登记；403 页申请闭环，25 篇 F-11/X7）。

    审批联动=第六类 permission_request 工单（review_tickets，CHECK 枚举随本批迁移扩展；
    review_ticket_id 指针不设 FK——跨模块表，随 M4 收口转端口化）。requester 取自令牌
    （api/01 §5.10 注记：正式实现从令牌解析，M1 前端过渡 body 上送字段后端不落库）。"""

    __tablename__ = "permission_requests"
    route: Mapped[str] = mapped_column(String(256), nullable=False)  # 被拒资源路径（403 页 useLocation）
    permission: Mapped[str | None] = mapped_column(String(64))  # 缺失权限点（11 篇 资源:动作）
    reason: Mapped[str] = mapped_column(Text, nullable=False)  # ≥10 字（mock 同规）
    desired_role: Mapped[str | None] = mapped_column(String(32))
    requester_name: Mapped[str] = mapped_column(String(128), nullable=False)  # 令牌解析（用户行 display_name）
    requester_email: Mapped[str] = mapped_column(String(256), nullable=False)  # 令牌解析（用户行 email）
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    review_ticket_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    __table_args__ = (
        CheckConstraint("status IN ('pending','approved','rejected')", name="ck_permission_requests_status"),
        Index("ix_permission_requests_tenant_status", "tenant_id", "status"),
    )

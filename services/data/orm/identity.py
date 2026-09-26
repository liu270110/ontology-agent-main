"""域① 身份与租户（5 表 | M1）。DDL 权威：database/01 §3.1。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, PkMixin, TenantMixin, TimestampMixin


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
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uk_users_tenant_id_email"),)


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

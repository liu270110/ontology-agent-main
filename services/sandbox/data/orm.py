"""域⑩ 沙箱（5 表 | M5，S0 可提前）。DDL 权威：database/01 §3.10；状态机权威：domain/model/sandbox.py。"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TimestampMixin, _now


class SandboxProfile(Base, PkMixin, TimestampMixin):
    __tablename__ = "sandbox_profiles"
    # NULL = 平台内置模板（映射表平台内置，Sandbox §3.2）
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    scenario: Mapped[str] = mapped_column(String(4), nullable=False)
    trust_level: Mapped[str] = mapped_column(String(2), nullable=False)
    cpu_limit: Mapped[Decimal] = mapped_column(nullable=False, default=Decimal("0.5"))
    mem_limit_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=512)
    pids_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=256)
    output_quota_kb: Mapped[int] = mapped_column(Integer, nullable=False, default=10240)
    egress_policy_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("egress_policies.id"))
    _IMG = "docker.m.daocloud.io/library/python:3.12-slim"
    base_image: Mapped[str] = mapped_column(String(256), nullable=False, default=_IMG)
    toolkit_ref: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    qos: Mapped[str] = mapped_column(String(2), nullable=False, default="ls")
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uk_sandbox_profiles_tenant_name"),
        CheckConstraint("scenario IN ('S0','S1','S2','S3','S4','S5')", name="ck_sbx_profiles_scenario"),
        CheckConstraint("trust_level IN ('T0','T1','T2','T3')", name="ck_sbx_profiles_trust"),
        CheckConstraint("qos IN ('ls','be')", name="ck_sbx_profiles_qos"),
    )


class SandboxInstance(Base, PkMixin, TimestampMixin):
    __tablename__ = "sandbox_instances"
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sandbox_profiles.id"), nullable=False)
    owner_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_ref: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # 归属聚合 id（四选一）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="creating")
    backend: Mapped[str] = mapped_column(String(16), nullable=False, default="docker")
    instance_ref: Mapped[str | None] = mapped_column(String(128))
    workspace_volume: Mapped[str | None] = mapped_column(String(128))
    credential_jti: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cpu_used: Mapped[Decimal | None] = mapped_column(nullable=True)
    mem_used_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    __table_args__ = (
        CheckConstraint("owner_kind IN ('session','pipeline','plugin','eval_run')", name="ck_sbx_inst_owner"),
        CheckConstraint(
            "status IN ('creating','ready','paused','running','hibernated','failed','terminated')",
            name="ck_sbx_inst_status",
        ),
        Index("idx_sbx_inst_owner", "tenant_id", "owner_kind", "owner_ref"),
        Index("idx_sbx_inst_status", "status", postgresql_where=text("status NOT IN ('terminated')")),
    )


class SandboxSnapshot(Base, PkMixin):
    """只追加（无 updated_at，同 audit_logs 纪律）。"""

    __tablename__ = "sandbox_snapshots"
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sandbox_instances.id"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="hibernate")
    sanitized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    __table_args__ = (CheckConstraint("source IN ('hibernate','checkpoint','artifact')", name="ck_sbx_snaps_source"),)


class EgressPolicy(Base, PkMixin, TimestampMixin):
    __tablename__ = "egress_policies"
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    rules: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)  # 空=全拒（Sandbox §6）
    review_ticket_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("review_tickets.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    __table_args__ = (UniqueConstraint("tenant_id", "name", "version", name="uk_egress_policies_tenant_name_ver"),)


class SandboxEvent(Base, PkMixin):
    """只追加（无 updated_at，同 audit_logs 纪律）。"""

    __tablename__ = "sandbox_events"
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    instance_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sandbox_instances.id"))
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    __table_args__ = (Index("idx_sbx_events_inst", "tenant_id", "instance_id", "created_at"),)

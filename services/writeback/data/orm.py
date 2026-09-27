"""writeback 模块 ORM：writeback_ledger + outbox_events（域⑧ 回写与事件 | M4）。

DDL 权威：database/01 §3.8（域⑧）逐列对齐；迁移 b8d4e6f8a0c2。迁移只增不改。
台账状态只前进不回退（MCP §8）；outbox 为只追加表（无 updated_at，database/01 §1 补充约定）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin


class WritebackLedgerORM(Base, PkMixin, TenantMixin):
    """回写台账（MCP §8：幂等键 UK 硬兜底；受理凭证/状态只前进；行永久保留不清理）。"""

    __tablename__ = "writeback_ledger"

    action_instance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # Neo4j ABox 实例，无 FK
    connector_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # 连接器注册（M4 无 FK）
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)  # "{tenant_id}:{action_instance_id}"
    request_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)  # 全量快照（高风险信封加密随 08 §3）
    receipt: Mapped[dict | None] = mapped_column(JSONB)  # 受理凭证（受理号+时间戳+键回显）
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default=text("'pending'"), nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    needs_human: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','accepted','succeeded','failed','compensated','unknown')",
            name="ck_writeback_ledger_status",
        ),
        UniqueConstraint("tenant_id", "idempotency_key", name="uk_writeback_idem"),
        Index("idx_writeback_recon", "tenant_id", "status", "updated_at"),  # database/01 §3.8 对账扫描
    )


class OutboxEventORM(Base, PkMixin, TenantMixin):
    """事务性发件箱（与业务行同事务写入；relay 异步发布；已发布行 30 天清理，database/01 §6）。"""

    __tablename__ = "outbox_events"

    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    event_version: Mapped[int] = mapped_column(SmallInteger, default=1, server_default=text("1"), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default=text("'pending'"), nullable=False)
    retry_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)  # 死信诊断（database/01 §3.8 本篇补充）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # NULL 即待发布

    __table_args__ = (
        CheckConstraint("status IN ('pending','published','failed','dead')", name="ck_outbox_events_status"),
        Index("idx_outbox_status", "status", "created_at"),
        Index("idx_outbox_unpublished", "created_at", postgresql_where=text("published_at IS NULL")),
        Index("idx_outbox_aggregate", "tenant_id", "aggregate_type", "aggregate_id", "id"),
    )

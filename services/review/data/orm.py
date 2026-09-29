"""review 模块 ORM：review_tickets（候选非成品门禁统一入口，08 §4）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin, TimestampMixin


class ReviewTicket(Base, PkMixin, TenantMixin, TimestampMixin):
    """候选非成品门禁，多场景统一入口（08 §4）；六态=03 §5 权威。"""

    __tablename__ = "review_tickets"
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # 多态引用，不设 FK
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    submitter_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    decision_note: Mapped[str | None] = mapped_column(Text)
    sla_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            # conflict = KB-G1a 冲突分诊 T2 工单（OntRAG §8.1「对齐现行 review_workflow 状态机」：
            # conflict 标记即工单入口；枚举扩展随 20260929 迁移，database/01 §3.5 同步）
            "target_type IN ('ontology_candidate','knowledge_instance','memory_l2_upgrade',"
            "'plugin_listing','writeback_incident','conflict')",
            name="target_type",
        ),
        CheckConstraint(
            "status IN ('draft','pending_review','approved','rejected','published','cancelled')", name="status"
        ),
        Index("idx_review_queue", "tenant_id", "status", "created_at"),
        Index(
            "uk_review_one_open",
            "tenant_id",
            "target_type",
            "target_id",
            unique=True,
            postgresql_where=text("status IN ('draft','pending_review')"),
        ),  # 同对象唯一 open
    )

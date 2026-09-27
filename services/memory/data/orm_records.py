"""域⑤ 记忆（2 表 | M4-计划1）。DDL 权威：database/01 §3.11 记忆域；规格：docs/架构设计/06 篇 §4。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin, TimestampMixin


class MemoryRecordORM(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "memory_records"

    layer: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    record_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_iri: Mapped[str | None] = mapped_column(String(512))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    structured: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    scope: Mapped[str] = mapped_column(String(16), default="personal", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    source_ref: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    proof_count: Mapped[int | None] = mapped_column(Integer)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    state: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    decay_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "record_type IN ('mem:Preference','mem:FactClaim','mem:Observation',"
            "'mem:Episode','mem:Decision','mem:Goal','mem:ProcedureRef')",
            name="record_type",
        ),
        CheckConstraint("layer IN (1,2,3,4)", name="layer"),
        CheckConstraint("scope IN ('personal','org')", name="scope"),
        CheckConstraint("state IN ('active','superseded','invalidated','expired')", name="state"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence"),
        Index("ix_memory_records_layer_subject_state", "layer", "subject_iri", "state"),
        Index("ix_memory_records_tenant_decay", "tenant_id", "decay_at"),
        Index("ix_memory_records_tenant_subject_created", "tenant_id", "subject_iri", "created_at"),
        Index("ix_memory_records_tenant_type_state", "tenant_id", "record_type", "state"),
    )


class MemoryPromotionORM(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "memory_promotions"

    record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memory_records.id"), nullable=False)
    from_layer: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    to_layer: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="submitted", nullable=False)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        CheckConstraint("from_layer IN (1,2,3,4)", name="from_layer"),
        CheckConstraint("to_layer IN (1,2,3,4)", name="to_layer"),
        CheckConstraint(
            "state IN ('submitted','reviewing','approved','rejected','applied')",
            name="state",
        ),
    )


class MemoryReviewItemORM(Base, PkMixin, TenantMixin, TimestampMixin):
    """待复核队列（06 篇 §5.1.1：低置信候选/冲突候选；人工复核后裁决）。"""

    __tablename__ = "memory_review_items"

    record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memory_records.id"), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)  # low_confidence / conflict
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)

    __table_args__ = (
        CheckConstraint("reason IN ('low_confidence','conflict')", name="reason"),
        CheckConstraint("state IN ('pending','approved','rejected')", name="state"),
        Index("ix_memory_review_items_tenant_state", "tenant_id", "state"),
    )

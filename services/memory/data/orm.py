"""memory 模块 ORM：memory_l2_facts（域⑦ 记忆 | M3）。

DDL 权威：database/01 §3.7（memory_l2_facts）+ docs/memory/多层记忆设计.md §7
（category/source_message_ids/supersedes_id/agent_id/decay_score/invalidated 状态等
记忆篇字段；database/01 §3.7 未收录部分的回填欠账见本批报告）。迁移只增不改。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin, TimestampMixin


class MemoryL2Fact(Base, PkMixin, TenantMixin, TimestampMixin):
    """用户级记忆事实（memory §7；向量副本在 Milvus memory_embeddings，lite 档 pgvector 同构）。"""

    __tablename__ = "memory_l2_facts"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # 指针不设 FK（防跨域 DDL 耦合）
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=Decimal("0.5"), nullable=False)
    decay_score: Mapped[float] = mapped_column(Double, default=0.0, server_default=text("0"), nullable=False)
    embedding_ref: Mapped[str | None] = mapped_column(String(64))
    source_session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id"))
    source_message_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # 版本链，不设 FK 防自引用锁
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active','superseded','invalidated')",  # memory §7 三态（expired 归 decay 阈值，不进状态）
            name="ck_memory_l2_facts_status",
        ),
        CheckConstraint("category IN ('profile','preference','fact','skill_note')", name="ck_memory_l2_facts_category"),
        # 新表唯一索引必须含 tenant_id（本批硬约束）；指纹判重 = (tenant, user, fingerprint)
        UniqueConstraint("tenant_id", "user_id", "fingerprint", name="uk_memory_l2_facts_tenant_user_fingerprint"),
        Index("idx_memory_l2_lookup", "tenant_id", "user_id", "status"),  # database/01 §3.7
        Index("idx_memory_l2_user_category", "user_id", "category"),  # memory §7 索引清单
    )

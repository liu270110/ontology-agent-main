"""platform 自有 ORM：llm_calls（域⑨ 观测，只追加；DDL 权威=database/01 §3.9）。

模型网关审计批量缓冲（audit.py）专用；不进 registry.py（platform 底座零上层依赖契约，
组合点白名单未含 memory/llm ORM——回填登记见本批报告）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin


class LlmCall(Base, PkMixin):
    """llm_calls 审计行（07 §2.3：每次调用含失败落库；月分区起步单表，database/01 §5.2）。"""

    __tablename__ = "llm_calls"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    token_in: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    token_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("kind IN ('complete','stream','embed')", name="ck_llm_calls_kind"),
        Index("idx_llm_calls_tenant_time", "tenant_id", "created_at"),
    )

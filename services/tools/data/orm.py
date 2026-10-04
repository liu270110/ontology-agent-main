"""tools 模块 ORM：tools_registry 登记表（S1 工具集市；DDL 权威=迁移 e5c7d9f1a3b5 自含 DDL 逐列对齐）。

14 §5：租户级 + created/updated（TimestampMixin）+ version 版本列 + deleted_at 软删列
（软删行对读面不可见，仓储读路径统一 ``deleted_at IS NULL``）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin, TimestampMixin


class ToolRegistryORM(Base, PkMixin, TenantMixin, TimestampMixin):
    """工具集市登记行（14 §5 tools_registry；租户作用域纪律=06 篇 §4）。"""

    # DDL：tenant_id UUID NOT NULL REFERENCES tenants(id)（覆盖 TenantMixin 无 FK 声明；
    # kb/orm.py:183、sandbox 同款覆盖先例）
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)

    __tablename__ = "tools_registry"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    action_iri: Mapped[str] = mapped_column(String(256), nullable=False)  # 本体行动类对账键（ExtensionMeta 口径）
    source_channel: Mapped[str] = mapped_column(String(8), nullable=False)  # L0~L3（14 §4 四通道）
    semantic_annotation: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # 无语义标注不上架
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)  # 市场件五态（14 §2）
    health_hint: Mapped[str | None] = mapped_column(String(128))
    evidence_uri: Mapped[str | None] = mapped_column(String(512))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 软删列（14 §5）

    __table_args__ = (
        # CHECK 短名（skills orm 同款：走 ck_%(table_name)s_%(constraint_name)s 模板编译）
        CheckConstraint("source_channel IN ('L0','L1','L2','L3')", name="source_channel"),
        CheckConstraint(
            "status IN ('draft','in_review','listed','deprecated','revoked')",
            name="status",
        ),
        UniqueConstraint("tenant_id", "name", name="uk_tools_registry_tenant_name"),
    )

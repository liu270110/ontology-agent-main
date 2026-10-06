"""workflows 模块 ORM：workflows + workflow_versions（15 §1.2；DDL 权威=迁移自含 DDL 逐列对齐）。

workflows：租户级 + created/updated（TimestampMixin）+ status 三态 + draft JSONB
（nodes/edges，15 §1.2）+ head_version；workflow_versions：不可变版本行（uk(workflow_id,
version)），快照一经落库零更新端口（15 §1.1 版本不可变）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin, TimestampMixin


class WorkflowORM(Base, PkMixin, TenantMixin, TimestampMixin):
    """工作流主表（15 §1.2 workflows；租户作用域纪律=06 篇 §4）。"""

    # DDL：tenant_id UUID NOT NULL REFERENCES tenants(id)（覆盖 TenantMixin 无 FK 声明；
    # tools/orm.py 同款覆盖先例）
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)

    __tablename__ = "workflows"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    template: Mapped[str] = mapped_column(String(64), default="blank", nullable=False)  # 实例化来源模板
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)  # draft|published|deprecated
    draft: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # {nodes, edges}（15 §1.2）
    head_version: Mapped[int | None] = mapped_column(Integer)  # None=从未发布
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (
        CheckConstraint("status IN ('draft','published','deprecated')", name="status"),
        CheckConstraint("head_version IS NULL OR head_version >= 1", name="head_version"),
    )


class WorkflowVersionORM(Base, PkMixin, TenantMixin):
    """不可变版本行（15 §1.2 workflow_versions；uk(workflow_id,version)；零软删零更新）。"""

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)

    __tablename__ = "workflow_versions"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)  # {nodes, edges} 固化形
    note: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    published_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("version >= 1", name="version"),
        UniqueConstraint("workflow_id", "version", name="uk_workflow_versions_wf_version"),
    )

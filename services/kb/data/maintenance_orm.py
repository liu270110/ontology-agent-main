"""kb 夜检/重嵌任务两表（OntRAG §8.4/§8.7；KB-G1b 2026-09-29）。

- kb_maintenance_runs：nightly 例程增量游标（§8.4 工程纪律 2）——每次运行落一行，
  watermark 为事件驱动重编译（v1.5）的 checkpoint 留位；v1 补嵌口径的幂等由
  「embedding IS NULL 才补」自然承载（§8.6 未变块跳过同源），watermark 暂存本轮
  最后补嵌 chunk id（hex32）；
- kb_reembed_jobs：向量重建 v1 最小任务模型（§8.7 五步的 lite 承载）——lite 档
  pgvector 无 Milvus 别名，版本切换由本表状态机（planning→reembedding→shadow→
  cutover→retired/rolled_back）+ document_chunks.backup_embedding 备份列承载。

backup_embedding 列不映射 ORM（与 embedding 同款纪律：迁移按 pgvector 可用性
条件创建，读写统一走 services/kb/business/reembed.py raw SQL）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin, TimestampMixin

REEMBED_JOB_STATUSES: tuple[str, ...] = (
    "planning",
    "reembedding",
    "shadow",
    "cutover",
    "rolled_back",
    "retired",
)
# 活跃态：同一租户同时至多一个（planning/shadow 之间可回退重跑）
ACTIVE_REEMBED_STATUSES: tuple[str, ...] = ("planning", "reembedding", "shadow", "cutover")


class KbMaintenanceRun(Base, PkMixin, TimestampMixin):
    """nightly 例程运行账本：增量游标 + stats 记账（§8.4 工程纪律 1/2 落点）。"""

    __tablename__ = "kb_maintenance_runs"
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id")
    )  # NULL = 平台级全租户运行（v1 默认口径）
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watermark_event_id: Mapped[str | None] = mapped_column(String(64))  # v1 事件留位：末次补嵌 chunk id（hex32）
    stats: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    __table_args__ = (
        # 游标回读：同租户（含 NULL 平台级）最近一次运行；部分索引避免跨租户误读
        Index(
            "ix_kb_maintenance_runs_scope_started",
            "tenant_id",
            "started_at",
            postgresql_where=text("finished_at IS NOT NULL"),
        ),
    )


class KbReembedJob(Base, PkMixin, TenantMixin, TimestampMixin):
    """向量重建任务行（§8.7 v1 最小：任务模型+进度+影子双读+切流开关四件）。"""

    __tablename__ = "kb_reembed_jobs"
    status: Mapped[str] = mapped_column(String(24), default="planning", nullable=False)
    new_model: Mapped[str] = mapped_column(String(64), nullable=False)
    progress: Mapped[dict] = mapped_column(
        JSONB, default=dict, nullable=False
    )  # {done,total,cursor(hex32),updated_at,…}
    __table_args__ = (
        CheckConstraint(
            "status IN ('planning','reembedding','shadow','cutover','rolled_back','retired')",
            name="status",
        ),
        Index("ix_kb_reembed_jobs_tenant_status", "tenant_id", "status"),
    )

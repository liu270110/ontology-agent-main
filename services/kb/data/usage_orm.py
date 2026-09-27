"""kb 知识活性计数 ORM：kb_usage_counters（1 表）。

多源接入设计权威：docs/OntRAG/多源接入与连接器设计.md §6.1（知识活性反馈环，v1 零成本埋点）。
隐私边界=只记计数与时间戳，不记"谁查了什么"的内容级日志（后者归审计域，权限隔离）。
维度键 (tenant_id, kb_collection_id, chunk_id) 一行一 chunk；计数列只增（ON CONFLICT 原子
递增，见 business/usage_service.py），时间戳列记最近一次访问。DDL 权威 database/01 回填
待办（与连接器两表同批登记），约束命名沿用 services/platform/db/base.py 约定。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin, TimestampMixin


class KbUsageCounter(Base, PkMixin, TenantMixin, TimestampMixin):
    """知识活性计数（§6.1）：upsert 原子递增，top_usage 按 search_hits 降序读（SLA 评审面）。

    维度列不带 FK（与 connector_orm 同款决策）：计数行=派生观测数据，绝不阻塞知识生命周期
    （文档/chunk 下线、零引用知识 nightly lint 清理不受统计残留牵制，孤儿行随清理任务收敛）。
    """

    __tablename__ = "kb_usage_counters"
    kb_collection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    search_hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    action_refs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_searched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "kb_collection_id",
            "chunk_id",
            name="uk_kb_usage_counters_dims",
        ),
        # top_usage 主查询面：按 (tenant, collection) 读 search_hits 降序前 N（SLA 升降级评审输入）
        Index("ix_kb_usage_counters_hits", "tenant_id", "kb_collection_id", "search_hits"),
    )

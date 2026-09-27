"""ORM 基座（06 篇 §1：orm/ = SQLAlchemy 模型；DDL 权威=database/01，迁移纪律=只增不改）。

命名约定固化到 MetaData（Alembic autogenerate 依赖稳定约束名）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from uuid_utils.compat import uuid7  # 2026-09-27 工程 P2：UUIDv7 选型标注=uuid-utils.compat（返回标准 uuid.UUID）

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uk_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _uuid() -> uuid.UUID:
    # UUIDv7（时间有序，PG 索引局部性友好；04 篇待办落地 2026-09-27）。
    # 迁移不重建：已落库表的库端 DEFAULT gen_random_uuid() 不受影响，本默认值仅作用于
    # ORM 侧插入的新行（base.py 默认值是 Python 端 default，非 DDL 变更）。
    return uuid7()


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class TenantMixin:
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)


class PkMixin:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)

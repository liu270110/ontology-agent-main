"""skills 模块 ORM：skills_assets 登记表（模块 12 v1 竖切；方案依据=docs/Agent/14 §5）。

14 §5 三表之一：全带 tenant_id + created/updated（TimestampMixin）+ 版本列 + 审计列
（created_by/updated_by）；软删不走 deleted 列——市场件语义由 status=deprecated/revoked
终态承载（全程可追溯，禁物理删除；14 §2 lifecycle 即软删面）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, CheckConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin, TimestampMixin


class SkillAssetORM(Base, PkMixin, TenantMixin, TimestampMixin):
    """技能资产登记行（14 §5 skills_assets；租户作用域纪律=06 篇 §4）。"""

    __tablename__ = "skills_assets"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(2048), default="", nullable=False)
    source_uri: Mapped[str] = mapped_column(String(512), nullable=False)  # 资产相对路径或外部 uri
    version: Mapped[str] = mapped_column(String(32), default="1.0.0", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="listed", nullable=False)  # 市场件五态（14 §2）
    body_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)  # SKILL.md 字节数
    origin: Mapped[str] = mapped_column(String(16), default="external", nullable=False)  # repo/external
    # K5 门 1/2（docs/Agent/13 §10）：声明名集 + 登记时点缺失快照，单行逗号分隔存储
    # （env 名不含逗号，无损）；空串=空集；missing_secrets 非空 ⇔ unprovisioned（不阻断 listed）。
    required_secrets: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    missing_secrets: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)  # 审计列（登记人；扫描入库=系统批）
    updated_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)  # 审计列（最近生命周期操作人）

    __table_args__ = (
        # CHECK 短名（kb connector_orm 先例：走 ck_%(table_name)s_%(constraint_name)s 模板
        # 编译 → 落库 ck_skills_assets_status/origin，避免显式全名被 op 层二次展开的双前缀债）
        CheckConstraint(
            "status IN ('draft','in_review','listed','deprecated','revoked')",
            name="status",
        ),
        CheckConstraint("origin IN ('repo','external')", name="origin"),
        UniqueConstraint("tenant_id", "name", "version", name="uk_skills_assets_tenant_name_version"),
    )

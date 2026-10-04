"""add skills_assets table（S2 技能集市竖切；方案依据=docs/Agent/14 §5）

skills 模块 v1 竖切建表（14 §5 三表之一）：列/约束逐字转录自 ORM
（services/skills/data/orm.py）：

- skills_assets：技能资产租户级登记表（14 §1 裁决：既有 services/skills/<slug>/SKILL.md
  资产保留原位为数据源，本表为集市注册面——扫描器 upsert + 外部登记共用）；
- 约束命名两形态（落库名=Base.metadata CreateTable 编译产物，先例 b3d5f7a9c1e3 口径）：
  ①CHECK 写 ORM 短名（status/origin）经 ck_%(table_name)s_%(constraint_name)s 模板编译
  → 落库 ck_skills_assets_status / ck_skills_assets_origin（迁移用 op.f 写编译终名）；
  ②uq/pk/ix 模板不含 %(constraint_name)s，显式名直用：uk_skills_assets_tenant_name_version、
  pk_skills_assets、ix_skills_assets_tenant_id；
- uk (tenant_id, name, version)=扫描器幂等键（14 §6 S2「name+version 已在则跳过」）；
- tenant_id 无 FK（TenantMixin 原样：invite_links b7d2e4f6a8c0 口径——租户删除策略未定）；
  created_by/updated_by 审计列无 FK（kb 五表口径：审计引用不阻塞租户清理）；
- server_default 按 kb 先例把 ORM Python default 落成库端默认（''/version '1.0.0'/
  status 'listed'/body_bytes 0/origin 'external'/时间戳 now()）；id 的 Python 端 uuid7
  默认不落库端（base.py：默认值仅作用于 ORM 侧插入）；
- 范围：只增不改——单表新建，无种子（scope 种子随本批独立迁移 f7a9c1e3f5a7）；
- downgrade 直接 drop（单表，无被引用方，零悬挂）。

Revision ID: e5f7a9c1e3f5
Revises: b835a095ffe4（S 批基线 head；跨批迁移链由主会话合入时统一调整——14 §5）
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "e5f7a9c1e3f5"
down_revision: str | None = "b835a095ffe4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skills_assets",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=2048), server_default="", nullable=False),
        sa.Column("source_uri", sa.String(length=512), nullable=False),
        sa.Column("version", sa.String(length=32), server_default="1.0.0", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="listed", nullable=False),
        sa.Column("body_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("origin", sa.String(length=16), server_default="external", nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by", UUID(as_uuid=True), nullable=True),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft','in_review','listed','deprecated','revoked')",
            name=op.f("ck_skills_assets_status"),
        ),
        sa.CheckConstraint("origin IN ('repo','external')", name=op.f("ck_skills_assets_origin")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skills_assets")),
        sa.UniqueConstraint("tenant_id", "name", "version", name="uk_skills_assets_tenant_name_version"),
    )
    op.create_index(op.f("ix_skills_assets_tenant_id"), "skills_assets", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_skills_assets_tenant_id"), table_name="skills_assets")
    op.drop_table("skills_assets")

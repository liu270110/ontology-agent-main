"""rsi_contributors + rsi_contributor_bindings（architecture/09 §14.1/§14.3；批次 A）

ORSI 外部贡献者两表（贡献者协议最小切片——注册/探测/命名空间装配原型）：

- rsi_contributors - 外部贡献者登记行（租户级；注册≠生效——只登记与探测档案，§14.1 红线
  继承：外部贡献只进候选池，apply 恒拒绝红线不变）。contributor_id 全局唯一 slug
  （^[a-z][a-z0-9-]{0,63}$，投递目录/claims 绑定/装配命名空间 `contributor:<id>` 公共
  命名面）；治理档位 governance_tier ∈ solo|team|enterprise（宪法 3 三档）；信誉分
  trust_score [0,1] 初始 1.0（§14.1 滑动窗批次 C 实装，本批只承载初始值）；delivery_dir
  投递目录约定路径记录（不触真实 FS）；probe_profile JSONB 最近探测档案（可空）。
- rsi_contributor_bindings - 贡献者命名空间绑定行（§14.3 步 4 热装配**物理分表**——
  与既有 capability 绑定面分表分键，装配/升级/回滚唯一操作面）。contributor_id 外键
  → rsi_contributors.contributor_id；surface ∈ 封闭八面 O1~O8（§13.2）；version 命名
  空间内版本号（每动作产新版本行，append-only，一键回滚=克隆目标版）；shadow 恒 TRUE
  （步 6 灰度：可调用不进计划候选；批次 A 无转正路径，CHECK 级红线）；payload JSONB
  装配载荷。(contributor_id, surface, version) 唯一=版本化兜底。
- 审计列 created_at/updated_at + 软删列 deleted_at（bindings 无软删——append-only 历史行）。

无侵扰纪律（§14.3 步 5）：本迁移只建新表，不改任何既有表；装配动作的无侵扰断言
（装配-升级-回滚三动作 × 既有绑定快照 diff 全空）见 tests/rsi/test_contributor.py。

迁移链注意：当前链上单 head（c9e1f3a5d7b2）——本迁移 down_revision 取该 head，
合入时由主会话按合入序调整链头（agents 不处理跨批迁移链，f5b9d3e7a1c4 先例同款）。

降级 drop 两表（bindings 先 drop——外键依赖；链上无其他表引用）。

Contract: architecture/09 §14.1/§14.3/§14.5（批次 A）。
ORM parity: services/rsi/data/orm.py ContributorORM / ContributorBindingORM。

Revision ID: b7d9e1f3a5c7
Revises: c9e1f3a5d7b2
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "b7d9e1f3a5c7"
down_revision: str | None = "c9e1f3a5d7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rsi_contributors",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("contributor_id", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("governance_tier", sa.String(16), nullable=False),
        sa.Column("trust_score", sa.Numeric(4, 3), nullable=False, server_default="1.000"),
        sa.Column("delivery_dir", sa.String(256), nullable=False),
        sa.Column("probe_profile", JSONB(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # 约束名=显式全名（ORM services/rsi/data/orm.py 逐字一致；命名约定仅作用于未命名约束）
        sa.CheckConstraint(
            "governance_tier IN ('solo','team','enterprise')",
            name="ck_rsi_contributors_governance_tier",
        ),
        sa.CheckConstraint("trust_score >= 0 AND trust_score <= 1", name="ck_rsi_contributors_trust_score"),
        sa.UniqueConstraint("contributor_id", name="uk_rsi_contributors_contributor_id"),
    )
    op.create_index(op.f("ix_rsi_contributors_tenant_id"), "rsi_contributors", ["tenant_id"], unique=False)

    op.create_table(
        "rsi_contributor_bindings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "contributor_id",
            sa.String(64),
            sa.ForeignKey("rsi_contributors.contributor_id", name="fk_rsi_bindings_contributor"),
            nullable=False,
        ),
        sa.Column("surface", sa.String(8), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("shadow", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("payload", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "surface IN ('O1','O2','O3','O4','O5','O6','O7','O8')",
            name="ck_rsi_contributor_bindings_surface",
        ),
        sa.CheckConstraint("version >= 1", name="ck_rsi_contributor_bindings_version"),
        sa.CheckConstraint("shadow = TRUE", name="ck_rsi_contributor_bindings_shadow"),
        sa.UniqueConstraint("contributor_id", "surface", "version", name="uk_rsi_bindings_identity"),
    )
    op.create_index(
        op.f("ix_rsi_contributor_bindings_tenant_id"), "rsi_contributor_bindings", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_rsi_contributor_bindings_contributor_id"),
        "rsi_contributor_bindings",
        ["contributor_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_rsi_contributor_bindings_contributor_id"), table_name="rsi_contributor_bindings")
    op.drop_index(op.f("ix_rsi_contributor_bindings_tenant_id"), table_name="rsi_contributor_bindings")
    op.drop_table("rsi_contributor_bindings")
    op.drop_index(op.f("ix_rsi_contributors_tenant_id"), table_name="rsi_contributors")
    op.drop_table("rsi_contributors")

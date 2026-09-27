"""add memory_l2_facts（M3 过渡方案：结构化 L2 用户级记忆事实）

计划 3.3（docs/architecture/13）；DDL 权威：database/01 §3.7 + docs/memory/多层记忆设计.md §7
（记忆篇为该域权威）。相对 database/01 §3.7 的字段补齐（category/source_message_ids/
supersedes_id/agent_id/decay_score/fingerprint/invalidated 状态）随本报告欠账清单回填 §3.7。
迁移只增不改；种子零（downgrade 可执行）。

Revision ID: d1e2f3a4b5c6
Revises: a9f3c2e1d7b8
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision: str = "d1e2f3a4b5c6"
down_revision: str | None = "a9f3c2e1d7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_l2_facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=True),  # 来源 agent 指针（不设 FK，防跨域 DDL 耦合）
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), server_default=sa.text("0.5"), nullable=False),
        sa.Column("decay_score", sa.Double(), server_default=sa.text("0"), nullable=False),
        sa.Column("embedding_ref", sa.String(length=64), nullable=True),
        sa.Column("source_session_id", sa.Uuid(), nullable=True),
        sa.Column("source_message_ids", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'active'"), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),  # 版本链不设 FK（防自引用锁，database/01 §3.11 口径）
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("keywords", ARRAY(sa.Text()), server_default=sa.text("ARRAY[]::text[]"), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_session_id"], ["sessions.id"], name="fk_memory_l2_facts_source_session_id_sessions"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_memory_l2_facts_tenant_id_tenants"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_memory_l2_facts_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_memory_l2_facts"),
        sa.CheckConstraint(
            "category IN ('profile','preference','fact','skill_note')",
            name="ck_memory_l2_facts_category",
        ),
        sa.CheckConstraint(
            "status IN ('active','superseded','invalidated')",  # 墓碑式软删：无物理删除
            name="ck_memory_l2_facts_status",
        ),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "fingerprint", name="uk_memory_l2_facts_tenant_user_fingerprint"
        ),  # 唯一索引含 tenant_id（§5.4 指纹幂等判重）
    )
    op.create_index("idx_memory_l2_lookup", "memory_l2_facts", ["tenant_id", "user_id", "status"], unique=False)
    op.create_index("idx_memory_l2_user_category", "memory_l2_facts", ["user_id", "category"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_memory_l2_user_category", table_name="memory_l2_facts")
    op.drop_index("idx_memory_l2_lookup", table_name="memory_l2_facts")
    op.drop_table("memory_l2_facts")

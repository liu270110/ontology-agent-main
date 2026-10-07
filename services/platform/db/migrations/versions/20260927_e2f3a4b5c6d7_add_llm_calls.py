"""add llm_calls（域⑨ 观测：模型网关审计/成本治理账本，只追加）

计划 3.3（docs/architecture/13）；DDL 权威：database/01 §3.9（llm_calls），
批量落库机制口径=architecture/07 §5.4（缓冲 1s 或 100 条先到者 flush）。
迁移只增不改；种子零（downgrade 可执行）。

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2f3a4b5c6d7"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("token_in", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("token_out", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), server_default=sa.text("0"), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_llm_calls_tenant_id_tenants"),
        sa.PrimaryKeyConstraint("id", name="pk_llm_calls"),
        sa.CheckConstraint("kind IN ('complete','stream','embed')", name="ck_llm_calls_kind"),
    )
    op.create_index("idx_llm_calls_tenant_time", "llm_calls", ["tenant_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_llm_calls_tenant_time", table_name="llm_calls")
    op.drop_table("llm_calls")

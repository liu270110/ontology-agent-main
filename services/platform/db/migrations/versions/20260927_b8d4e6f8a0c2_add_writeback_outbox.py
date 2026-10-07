"""add writeback_ledger + outbox_events（域⑧ 回写与事件，2 表 | M4）

计划 4.2（docs/architecture/13）；DDL 权威：database/01 §3.8（域⑧），
回写契约=docs/MCP/业务回写设计 §8（幂等键 UK 硬兜底 / 状态只前进不回退），
Outbox relay 口径=architecture/07 §5.2（published_at NULL 即待发布，at-least-once）。
迁移只增不改；种子零（downgrade 可执行）。

Revision ID: b8d4e6f8a0c2
Revises: e2f3a4b5c6d7
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8d4e6f8a0c2"
down_revision: str | None = "e2f3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("event_version", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("retry_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_outbox_events_tenant_id_tenants"),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
        sa.CheckConstraint("status IN ('pending','published','failed','dead')", name="ck_outbox_events_status"),
    )
    op.create_index("idx_outbox_status", "outbox_events", ["status", "created_at"], unique=False)
    op.create_index(
        "idx_outbox_unpublished",
        "outbox_events",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_index(
        "idx_outbox_aggregate",
        "outbox_events",
        ["tenant_id", "aggregate_type", "aggregate_id", "id"],
        unique=False,
    )

    op.create_table(
        "writeback_ledger",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("action_instance_id", sa.Uuid(), nullable=False),
        sa.Column("connector_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("receipt", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("needs_human", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_writeback_ledger_tenant_id_tenants"),
        sa.PrimaryKeyConstraint("id", name="pk_writeback_ledger"),
        sa.CheckConstraint(
            "status IN ('pending','accepted','succeeded','failed','compensated','unknown')",
            name="ck_writeback_ledger_status",
        ),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uk_writeback_idem"),
    )
    op.create_index("idx_writeback_recon", "writeback_ledger", ["tenant_id", "status", "updated_at"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_writeback_recon", table_name="writeback_ledger")
    op.drop_table("writeback_ledger")
    op.drop_index("idx_outbox_aggregate", table_name="outbox_events")
    op.drop_index("idx_outbox_unpublished", table_name="outbox_events")
    op.drop_index("idx_outbox_status", table_name="outbox_events")
    op.drop_table("outbox_events")

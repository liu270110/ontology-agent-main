"""memory review items

Revision ID: b2d4e6f8a0c1
Revises: a1c2e3f4b5d6
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2d4e6f8a0c1"
down_revision: str | None = "a1c2e3f4b5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_review_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("detail", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("reason IN ('low_confidence','conflict')", name=op.f("ck_memory_review_items_reason")),
        sa.CheckConstraint("state IN ('pending','approved','rejected')", name=op.f("ck_memory_review_items_state")),
        sa.ForeignKeyConstraint(
            ["record_id"], ["memory_records.id"], name=op.f("fk_memory_review_items_record_id_memory_records")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_review_items")),
    )
    op.create_index("ix_memory_review_items_tenant_id", "memory_review_items", ["tenant_id"])
    op.create_index(
        "ix_memory_review_items_tenant_state", "memory_review_items", ["tenant_id", "state"]
    )


def downgrade() -> None:
    op.drop_index("ix_memory_review_items_tenant_state", table_name="memory_review_items")
    op.drop_index("ix_memory_review_items_tenant_id", table_name="memory_review_items")
    op.drop_table("memory_review_items")

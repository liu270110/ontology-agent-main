"""add memory owner_user_id

Revision ID: c3d5e7f9a1b3
Revises: b2d4e6f8a0c1
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3d5e7f9a1b3"
down_revision: str | None = "b2d4e6f8a0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("memory_records", sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_memory_records_tenant_owner_layer", "memory_records", ["tenant_id", "owner_user_id", "layer"])


def downgrade() -> None:
    op.drop_index("ix_memory_records_tenant_owner_layer", table_name="memory_records")
    op.drop_column("memory_records", "owner_user_id")

"""memory tables

Revision ID: a1c2e3f4b5d6
Revises: a7c3e91d2f40（2026-09-28 集成重根：develop 单头；源链 367b405f344b 在平台迁移轴同 root 不复用）
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1c2e3f4b5d6"
down_revision: str | None = "a7c3e91d2f40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MEMORY_TYPES = (
    "mem:Preference", "mem:FactClaim", "mem:Observation",
    "mem:Episode", "mem:Decision", "mem:Goal", "mem:ProcedureRef",
)


def upgrade() -> None:
    op.create_table(
        "memory_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("layer", sa.SmallInteger(), nullable=False),
        sa.Column("record_type", sa.String(length=64), nullable=False),
        sa.Column("subject_iri", sa.String(length=512), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("structured", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source_ref", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("proof_count", sa.Integer(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("state", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("decay_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name=op.f("ck_memory_records_confidence")),
        sa.CheckConstraint("layer IN (1,2,3,4)", name=op.f("ck_memory_records_layer")),
        sa.CheckConstraint(f"record_type IN {str(_MEMORY_TYPES)}", name=op.f("ck_memory_records_record_type")),
        sa.CheckConstraint("scope IN ('personal','org')", name=op.f("ck_memory_records_scope")),
        sa.CheckConstraint(
            "state IN ('active','superseded','invalidated','expired')", name=op.f("ck_memory_records_state")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_records")),
    )
    op.create_index("ix_memory_records_tenant_id", "memory_records", ["tenant_id"])
    op.create_index(
        "ix_memory_records_layer_subject_state", "memory_records", ["layer", "subject_iri", "state"]
    )
    op.create_index("ix_memory_records_tenant_decay", "memory_records", ["tenant_id", "decay_at"])
    op.create_index(
        "ix_memory_records_tenant_subject_created",
        "memory_records",
        ["tenant_id", "subject_iri", "created_at"],
    )
    op.create_index(
        "ix_memory_records_tenant_type_state", "memory_records", ["tenant_id", "record_type", "state"]
    )
    op.create_table(
        "memory_promotions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_layer", sa.SmallInteger(), nullable=False),
        sa.Column("to_layer", sa.SmallInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="submitted", nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("from_layer IN (1,2,3,4)", name=op.f("ck_memory_promotions_from_layer")),
        sa.CheckConstraint("to_layer IN (1,2,3,4)", name=op.f("ck_memory_promotions_to_layer")),
        sa.CheckConstraint(
            "state IN ('submitted','reviewing','approved','rejected','applied')",
            name=op.f("ck_memory_promotions_state"),
        ),
        sa.ForeignKeyConstraint(
            ["record_id"], ["memory_records.id"], name=op.f("fk_memory_promotions_record_id_memory_records")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_promotions")),
    )
    op.create_index("ix_memory_promotions_tenant_id", "memory_promotions", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_promotions_tenant_id", table_name="memory_promotions")
    op.drop_table("memory_promotions")
    op.drop_index("ix_memory_records_tenant_type_state", table_name="memory_records")
    op.drop_index("ix_memory_records_tenant_subject_created", table_name="memory_records")
    op.drop_index("ix_memory_records_tenant_decay", table_name="memory_records")
    op.drop_index("ix_memory_records_layer_subject_state", table_name="memory_records")
    op.drop_index("ix_memory_records_tenant_id", table_name="memory_records")
    op.drop_table("memory_records")

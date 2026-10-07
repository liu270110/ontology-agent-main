"""add kb_facts（OntRAG 七步流水线终点权威表：抽取事实候选/裁决落位）

2026-09-27 模块轴重构期间本文件曾在并行会话产出后遗失，现按真库 information_schema DDL
逐列原样重建（revision id 沿用原值，已升级库的 alembic_version 与之对齐）。
约束名沿用真库现状（含 autogen 双前缀 ck_kb_facts_ck_kb_facts_*），命名归一随后续批次统一处理。

Revision ID: a9f3c2e1d7b8
Revises: b7c3e9f0a1d2
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "a9f3c2e1d7b8"
down_revision: str | None = "b7c3e9f0a1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kb_facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("fact_type", sa.String(length=16), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("predicate", sa.Text(), nullable=True),
        sa.Column("object", sa.Text(), nullable=True),
        sa.Column("subject_type", sa.String(length=128), nullable=True),
        sa.Column("object_type", sa.String(length=128), nullable=True),
        sa.Column("canonical_name", sa.String(length=256), nullable=True),
        sa.Column("aliases", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'candidate'::character varying"), nullable=False
        ),
        sa.Column("evidence", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("violations", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("meta", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id"], ["document_chunks.id"], name="fk_kb_facts_chunk_id_document_chunks"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], name="fk_kb_facts_document_id_documents"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_kb_facts_tenant_id_tenants"),
        sa.PrimaryKeyConstraint("id", name="pk_kb_facts"),
        sa.CheckConstraint(
            "fact_type IN ('entity','relation','attribute','event')",
            name="ck_kb_facts_ck_kb_facts_fact_type",
        ),
        sa.CheckConstraint(
            "status IN ('candidate','rejected','authoritative')",
            name="ck_kb_facts_ck_kb_facts_status",
        ),
    )
    op.create_index("idx_kb_facts_doc", "kb_facts", ["tenant_id", "document_id", "status"], unique=False)
    op.create_index("idx_kb_facts_queue", "kb_facts", ["tenant_id", "status", "confidence"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_kb_facts_queue", table_name="kb_facts")
    op.drop_index("idx_kb_facts_doc", table_name="kb_facts")
    op.drop_table("kb_facts")

"""add documents.acl_tags（OntRAG §4.3：文档级标签 ACL，database/01 §3.12 预登记转正）

2026-09-27 kb OWNER 批次产物；模块轴集成期由主持方重挂迁移链（原 down_revision
a1b2c3d4e5f6 与 memory L2 撞双头，现串 b2d4f6a8c0e2 之后成单头）。

Revision ID: d3e4f5a6b7c8
Revises: b2d4f6a8c0e2
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "b2d4f6a8c0e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("acl_tags", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
    )
    # 未标注/空标注=继承租户全员；已标注文档须调用方标签面命中（deny-by-default，检索三路下推）
    # GIN 仅作用 acl_tags（jsonb 默认 jsonb_ops 支持 ?|/@> 标签过滤；租户/集合列由既有 btree 索引覆盖）
    op.create_index(
        "ix_documents_acl_tags",
        "documents",
        ["acl_tags"],
        postgresql_using="gin",
        postgresql_where=sa.text("jsonb_array_length(acl_tags) > 0"),
    )


def downgrade() -> None:
    op.drop_index("ix_documents_acl_tags", table_name="documents")
    op.drop_column("documents", "acl_tags")

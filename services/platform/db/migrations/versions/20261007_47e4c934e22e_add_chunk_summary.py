"""document_chunks 加 summary 列——G-14 pgvector L0 摘要零读直出（13 篇 §22 K16-a）

Revision ID: 47e4c934e22e
Revises: a1eddb95e6dd
Create Date: 2026-10-07

openviking@23 §3 蓝本（docs/Agent/13 §22 K16 立项）：L0 摘要随 chunk 嵌入同点写入、
检索同一条 SQL 带回（零读直出=消费摘要不回读原文库）。本迁移只做**存储面**：

- document_chunks 加列 summary TEXT NULL——可空不回填：存量行随 embed 步重嵌入时
  自然生成（K16-b 边界：只补缺失向量的增量口径，存量已嵌入行不被触碰），不做全量
  回填脚本；
- additive-only 单头（down_revision=a1eddb95e6dd，alembic heads 现场核）；
- downgrade 直接删列：摘要为确定性派生数据（内容前缀），可由 embed 步随时重建，
  无独立保留语义。

Contract: docs/Agent/13 §22（K16-a）；上游 docs/研究整理/12-开源Agent深度对标/23-openviking.md §3。
ORM parity: services/kb/data/orm.py DocumentChunk.summary。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "47e4c934e22e"
down_revision: str | None = "a1eddb95e6dd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("document_chunks", sa.Column("summary", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("document_chunks", "summary")

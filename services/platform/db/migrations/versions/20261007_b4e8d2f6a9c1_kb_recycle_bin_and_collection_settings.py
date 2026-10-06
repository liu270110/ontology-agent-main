"""kb 回收站软删列 + 库设置 JSONB（api/01 §5.4 追加行，B3-Q 转实）。

Contract: docs/api/01-REST-API契约.md §5.4（GET /kb/recycle-bin、POST /kb/documents/{id}/restore、
DELETE /kb/documents/{id}/purge、GET/PUT /kb/collections/{id}/settings；B3-Q 2026-09-29 前端转实
登记）+ frontend/src/mocks/kb-handlers.ts 回收站/库设置段（契约权威）。
ORM parity: services/kb/data/orm.py Document.deleted_at/deleted_reason、KbCollection.settings。

纯加列（additive-only，arch 06 纪律）：
1. documents.deleted_at TIMESTAMPTZ NULL——回收站软删时间戳（非空=在回收站，7 天保留期；
   expires_at 由 API 投影层按 deleted_at+7d 计算，不落列）；检索下线闸仍是 valid_to
   （三路检索 SQL 谓词不变）；历史墓碑行（valid_to 非空而 deleted_at 空）不回填、不进回收站；
2. documents.deleted_reason TEXT NULL——软删原因调用方口径（契约 v1 无入参，恒空留位）；
3. kb_collections.settings JSONB NOT NULL DEFAULT '{}'——库设置整包（chunk_size/chunk_overlap/
   extract_prompt_level/auto_extract）；存量行 server_default 兜底，与 chunk_defaults（chunking
   留位）分列不混用。

downgrade 逆序 drop；无数据迁移、无约束新增（本批不涉及 ck_<表>_<列> 命名链）。

Revision ID: b4e8d2f6a9c1
Revises: a1eddb95e6dd
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "b4e8d2f6a9c1"
down_revision: str | None = "a1eddb95e6dd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("documents", sa.Column("deleted_reason", sa.Text(), nullable=True))
    op.add_column(
        "kb_collections",
        sa.Column("settings", JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("kb_collections", "settings")
    op.drop_column("documents", "deleted_reason")
    op.drop_column("documents", "deleted_at")

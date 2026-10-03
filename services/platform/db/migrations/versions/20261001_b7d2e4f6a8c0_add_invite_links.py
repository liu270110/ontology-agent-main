"""invite_links（架构设计/32 §三）：链接邀请表（DB 只存 token 哈希）。

2026-10-01 邀请链接切片（契约=docs/架构设计/32-邀请链接与成员加入设计.md §二/§三；
登记=api/01 §5.8 invite-links 五端点改道 /invites*）。单表：

- invite_links - 团体邀请链接（M1 语义=有效期内不限人数的团体邀请；过期/撤销即失效）。
  安全口径：token_hash=sha256(token) hex（secrets.token_urlsafe(24) 明文仅在生成响应
  返回一次，DB 泄露即链接可用被 32 篇 §一裁定为安全底线）；role 存 Role.code；
  used_count M1 仅计数不限人数（max_uses 字段预留不启用，故本批不落列）。

降级 drop 本表（链上无其他表引用；users/tenants 为被引用方不受影响）。

Contract: docs/架构设计/32 §三；api/01 §5.8。
ORM parity: services/iam/data/orm.py Invite。

Revision ID: b7d2e4f6a8c0
Revises: a5b7c9d1e3f5
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "b7d2e4f6a8c0"
down_revision: str | None = "a5b7c9d1e3f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # tenant_id 无 FK（首 27 表同款口径：租户删除策略未定，不设硬引用）；created_by → users.id 有 FK
    op.create_table(
        "invite_links",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), nullable=False),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_invite_links_created_by_users")),
        sa.UniqueConstraint("token_hash", name=op.f("uk_invite_links_token_hash")),
    )
    op.create_index(op.f("ix_invite_links_tenant_id"), "invite_links", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_invite_links_tenant_id"), table_name="invite_links")
    op.drop_table("invite_links")

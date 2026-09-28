"""prompt library (H-1): prompt_templates + prompt_versions (versioned LLM asset).

2026-09-29 H-1 prompt engineering governance batch (api/01 SS5.10 pre-registered
F-08/X12; standards/01 SS5.1 "prompts are versioned assets"). Two tables:

1. prompt_templates - template head (owner/scope/slug/name/status); scope is the
   two-level personal|tenant visibility, status active|archived where archived =
   soft delete (DELETE semantics; versions retained for traceability);
2. prompt_versions - append-only version rows (uk template_id+version; content
   JSONB {system_prompt, template, few_shot[], variables[]} + checksum sha256[:16]
   for runtime pinned references "prompt:{id}@{version}").

Downgrade drops both tables (version chain is unreferenced outside them in v1).

Contract: docs/api/01-REST-API契约.md SS5.10; standards/01-编码规范.md SS5.
ORM parity: services/agent/data/orm.py PromptTemplate / PromptVersion.

Revision ID: b8e1f2a3c4d5
Revises: 7c7b41e2b272
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "b8e1f2a3c4d5"
down_revision: str | None = "7c7b41e2b272"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "prompt_templates",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("owner_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("slug", sa.String(128), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "scope IN ('personal','tenant')", name=op.f("ck_prompt_templates_ck_prompt_templates_scope")
        ),
        sa.CheckConstraint(
            "status IN ('active','archived')", name=op.f("ck_prompt_templates_ck_prompt_templates_status")
        ),
        sa.UniqueConstraint("tenant_id", "scope", "slug", name=op.f("uk_prompt_templates_tenant_scope_slug")),
    )
    op.create_index(
        op.f("ix_prompt_templates_tenant_id"), "prompt_templates", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_prompt_templates_tenant_scope_status"),
        "prompt_templates",
        ["tenant_id", "scope", "status"],
        unique=False,
    )
    op.create_table(
        "prompt_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("template_id", UUID(as_uuid=True), sa.ForeignKey("prompt_templates.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version >= 1", name=op.f("ck_prompt_versions_ck_prompt_versions_version")),
        sa.UniqueConstraint("template_id", "version", name=op.f("uk_prompt_versions_template_version")),
    )
    op.create_index(op.f("ix_prompt_versions_tenant_id"), "prompt_versions", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_prompt_versions_template_id"), "prompt_versions", ["template_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_prompt_versions_template_id"), table_name="prompt_versions")
    op.drop_index(op.f("ix_prompt_versions_tenant_id"), table_name="prompt_versions")
    op.drop_table("prompt_versions")
    op.drop_index(op.f("ix_prompt_templates_tenant_scope_status"), table_name="prompt_templates")
    op.drop_index(op.f("ix_prompt_templates_tenant_id"), table_name="prompt_templates")
    op.drop_table("prompt_templates")

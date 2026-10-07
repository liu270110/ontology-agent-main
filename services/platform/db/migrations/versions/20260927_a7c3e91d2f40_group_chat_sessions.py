"""群聊会话基座（27 篇 X15）：sessions type/routing 列 + session_members 表 + messages.agent_id。

Revision ID: a7c3e91d2f40
Revises: d3e4f5a6b7c8
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "a7c3e91d2f40"
down_revision: str | None = "d3e4f5a6b7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("type", sa.String(16), nullable=False, server_default="single"),
    )
    op.add_column(
        "sessions",
        sa.Column("routing", sa.String(16), nullable=False, server_default="round_robin"),
    )
    op.create_check_constraint(
        "ck_sessions_type", "sessions", "type IN ('single','group')"
    )
    op.create_check_constraint(
        "ck_sessions_routing",
        "sessions",
        "routing IN ('mention','round_robin','all','orchestrator')",
    )
    op.create_table(
        "session_members",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("sessions.id"), nullable=False, index=True),
        sa.Column("agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column("routing_role", sa.String(16), nullable=False, server_default="speaker"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("session_id", "display_name", name="uk_session_members_session_display"),
        sa.UniqueConstraint("session_id", "agent_id", name="uk_session_members_session_agent"),
        sa.CheckConstraint(
            "routing_role IN ('coordinator','speaker','observer')", name="ck_session_members_role"
        ),
    )
    op.add_column("messages", sa.Column("agent_id", UUID(as_uuid=True), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "agent_id")
    op.drop_table("session_members")
    op.drop_constraint("ck_sessions_routing", "sessions", type_="check")
    op.drop_constraint("ck_sessions_type", "sessions", type_="check")
    op.drop_column("sessions", "routing")
    op.drop_column("sessions", "type")

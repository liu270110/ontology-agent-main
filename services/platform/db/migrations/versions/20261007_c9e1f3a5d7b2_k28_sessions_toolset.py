"""sessions toolset 会话具名工具集列（K28-a，docs/Agent/13 §34）

Revision ID: c9e1f3a5d7b2
Revises: b8e4d2f6a9c1
Create Date: 2026-10-07

K28 批（F-7 会话面具名工具集；hermes 03 §3 具名工具集=会话表面门）：

``sessions.toolset``：VARCHAR(64) NULL——具名工具集名（会话工具表面门；NULL=平台现行
全集，含 MCP 桥动态面）。名单来源=business/capabilities/toolsets.py TOOLSETS（应用层
fail-closed 校验，未知名 API 层 422），DB 不设 CHECK——注册表演进不应耦合库约束
（迁移只增不改纪律下的纯加列；64=SessionCreateIn.toolset max_length 同口径）。

additive-only；存量行不回填（NULL 语义=不设门，与既有会话行为零变化）。
downgrade：删列。

ORM parity: services/agent/data/orm.py（Session.toolset 列）。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c9e1f3a5d7b2"
down_revision: str | None = "b8e4d2f6a9c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE sessions ADD COLUMN toolset VARCHAR(64) NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS toolset")

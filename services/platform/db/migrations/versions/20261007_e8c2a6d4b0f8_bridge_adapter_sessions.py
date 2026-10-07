"""Bridge adapter_sessions 会话映射表（docs/Agent/20 §2 G1 批；05 篇 §4.4 落库面）

Revision ID: e8c2a6d4b0f8
Revises: c9e1f3a5d7b2
Create Date: 2026-10-07

G1 ACP 适配器批（Bridge 三形态第一波）：

``adapter_sessions``：桥自持 platform_session ↔ (adapter, foreign_id) 映射表——
F4 ACP 本批消费（platform sessions.id ↔ ACP sessionId），F3 http-generic / F5 a2a
共用（docs/Agent/20 §2「F3/F5 共用一次建表」）。列：session_id（FK sessions.id）/
adapter VARCHAR(32)（形态键：acp|http-generic|cli-generic|a2a）/ foreign_id VARCHAR(256)
（对端工具会话标识）/ metadata JSONB（profile/最近停因等可追溯面，ORM 属性名 meta）。

uk_adapter_sessions_session_id_adapter：一会话一形态至多一行（05 §4.4「fork 产生新
tool_session_id 显式建模」——fork 分支=新平台会话新行，本表不承载一对多）；
重绑（对端进程重启旧 foreign 失效）走 upsert 覆盖，不换行。

additive-only 纯建表（迁移只增不改纪律）；无数据迁移、无存量回填。
downgrade：drop table。

⚠ 调链注记：down_revision 取当前链头 c9e1f3a5d7b2（K28 sessions toolset）；G2 姊妹批
（http_service/cli_jsonl）并行同 head——合入 develop 时由主会话按合入序调整链头/补 merge
迁移（agents 不处理跨批迁移链，与 20261007_b8e4d2f6a9c1 同款注记）。

ORM parity: services/agent/data/orm.py（AdapterSession）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "e8c2a6d4b0f8"
down_revision: str | None = "c9e1f3a5d7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "adapter_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("adapter", sa.String(32), nullable=False),
        sa.Column("foreign_id", sa.String(256), nullable=False),
        sa.Column("metadata", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("session_id", "adapter", name="uk_adapter_sessions_session_id_adapter"),
    )
    # 索引名=NAMING_CONVENTION（base.py）展开值，与 Base.metadata.create_all 同形（测试库 parity）
    op.create_index("ix_adapter_sessions_tenant_id", "adapter_sessions", ["tenant_id"])
    op.create_index("ix_adapter_sessions_session_id", "adapter_sessions", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_adapter_sessions_session_id", table_name="adapter_sessions")
    op.drop_index("ix_adapter_sessions_tenant_id", table_name="adapter_sessions")
    op.drop_table("adapter_sessions")

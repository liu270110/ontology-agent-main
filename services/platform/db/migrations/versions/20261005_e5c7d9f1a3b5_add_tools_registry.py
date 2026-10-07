"""add tools_registry (tools marketplace, docs/Agent/14 S1)

Revision ID: e5c7d9f1a3b5
Revises: b835a095ffe4
Create Date: 2026-10-05

工具集市登记表（docs/Agent/14-集市平台核心模块与ORSI原子能力设计（主仓本地）§2/§5）：
租户级 + created/updated + version 版本列 + deleted_at 软删列。
additive-only 单头；注意：M4.6 并行批各有在途迁移——down_revision 取本批基线 head
（b835a095ffe4），主会话合入时可能按合入序调链（S 批纪律：agents 不处理跨批迁移链）。
离线干跑：alembic upgrade head --sql（PG 活库不可用时）。
"""

from typing import Sequence, Union

from alembic import op


revision: str = "e5c7d9f1a3b5"
down_revision: Union[str, None] = "b835a095ffe4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DDL = [
    """
    CREATE TABLE tools_registry (                   -- 工具集市登记表（14 §2/§5；租户级）
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id),
        name VARCHAR(128) NOT NULL, action_iri VARCHAR(256) NOT NULL,
        source_channel VARCHAR(8) NOT NULL CHECK (source_channel IN ('L0','L1','L2','L3')),
        semantic_annotation JSONB NOT NULL DEFAULT '{}',   -- 无语义标注不上架（ExtensionMeta 纪律）
        version VARCHAR(32) NOT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','in_review','listed','deprecated','revoked')),
        health_hint VARCHAR(128), evidence_uri VARCHAR(512),
        deleted_at TIMESTAMPTZ,                     -- 软删列（14 §5；软删行对读面不可见）
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uk_tools_registry_tenant_name UNIQUE (tenant_id, name))
    """,
]


def upgrade() -> None:
    for stmt in _DDL:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tools_registry CASCADE")

"""add plugin market 4 tables (domain-6)

Revision ID: a1b2c3d4e5f6
Revises: b8d4e6f8a0c2
Create Date: 2026-09-27

Discipline (arch 06 / database/01): additive-only; single head.
DDL 权威 = database/01 §3.6 域⑥ 插件与工具（M5 随插件市场建齐）——迁移用原文 DDL 执行，
避免 ORM API 翻译漂移；resource_leases（Agent/03 §3 租约表，12 篇推迟 M5 激活）DDL 尚未
回填 database/01（在册待办=Agent/03 §83），不在册不落——随回填迁移一并补。
离线干跑：alembic upgrade head --sql（PG 活库不可用时）。
"""

from typing import Sequence, Union

from alembic import op


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "b8d4e6f8a0c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DDL = [
    """
    CREATE TABLE plugins (                          -- 平台级（"商品"）
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), slug VARCHAR(128) NOT NULL, name VARCHAR(128) NOT NULL,
        kind VARCHAR(16) NOT NULL CHECK (kind IN ('mcp_server','rest_api','skill','prompt')),
        publisher_id UUID REFERENCES users(id), latest_version VARCHAR(32), signature VARCHAR(512),
        status VARCHAR(16) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','delisted')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uk_plugins_slug UNIQUE (slug))
    """,
    """
    CREATE TABLE plugin_versions (                  -- 平台级；版本发布后不可覆盖（04 篇 plugin 不变式）
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), plugin_id UUID NOT NULL REFERENCES plugins(id),
        version VARCHAR(32) NOT NULL, server_json JSONB NOT NULL DEFAULT '{}', compat_mcp VARCHAR(64),
        artifact_key VARCHAR(512) NOT NULL, checksum CHAR(64) NOT NULL,
        scope_required TEXT[] NOT NULL DEFAULT '{}', scan_report JSONB,
        status VARCHAR(16) NOT NULL DEFAULT 'submitted' CHECK (status IN ('submitted','scan_passed','published','delisted')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uk_plugin_versions UNIQUE (plugin_id, version))
    """,
    """
    CREATE TABLE tools (                            -- 统一工具视图（"单件"）
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id),
        name VARCHAR(128) NOT NULL, kind VARCHAR(16) NOT NULL CHECK (kind IN ('builtin','mcp_remote','rest_adapter','plugin')),
        provider_ref JSONB,                         -- plugin_id 或外部 server 标识
        input_schema JSONB NOT NULL DEFAULT '{}', annotations JSONB,
        ontology_action_iri VARCHAR(256),
        scope_required TEXT[] NOT NULL DEFAULT '{}', enabled BOOLEAN NOT NULL DEFAULT false,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uk_tools_tenant_name UNIQUE (tenant_id, name))
    """,
    """
    CREATE TABLE tool_invocations (                 -- 只追加；审计 + 成本归因
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id),
        tool_id UUID NOT NULL REFERENCES tools(id), session_id UUID REFERENCES sessions(id), task_id UUID REFERENCES tasks(id),
        caller JSONB NOT NULL DEFAULT '{}',
        input_digest JSONB, output_digest JSONB,
        status VARCHAR(16) NOT NULL, latency_ms INT, error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())
    """,
    "CREATE INDEX idx_tool_invocations_tenant ON tool_invocations (tenant_id, created_at)",
    "CREATE INDEX idx_tool_invocations_tool ON tool_invocations (tool_id, created_at)",
]


def upgrade() -> None:
    for stmt in _DDL:
        op.execute(stmt)


def downgrade() -> None:
    for tbl in ("tool_invocations", "tools", "plugin_versions", "plugins"):
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")

"""mcp 管理面两表 mcp_servers + mcp_tools + mcp:read/write scope 种子（api/01 §5.7 预登记实装）

Revision ID: b2c4d6f8a1e3
Revises: a4b1cc126df7
Create Date: 2026-10-05

mcp 管理域 8 端点（POST /mcp/discover、GET/POST /mcp/servers、GET /mcp/servers/{id}、
POST /mcp/servers/{id}/refresh、GET /mcp/servers/{id}/tools、POST /mcp/tools/{tool_id}/
enable|disable、DELETE /mcp/servers/{id}）的存储落点；契约源=frontend mock
platform-handlers.ts §5.7（McpServer/McpTool 字段名）+ docs/api/01 §5.7 行与 ★ 预登记行
（mcp:read / mcp:write scope，2026-09-27/28）。DDL 与 services/mcp/data/orm.py 两 ORM 类
同文（database/01 域⑥表格补录说明=本头注：mcp_servers 登记行 + mcp_tools 工具缓存行，
MCP 篇 §9 建表欠账 M5 随本批提前收口，审计仍由 audit_logs 承接）：

- mcp_servers            外部 Server 登记行（明文凭据只落 token_secret=refresh 复探所必需，
                         targets.json 同水位，凭据托管引用随 M5 换装；读面唯一出参 token_masked）
- mcp_tools              工具缓存行（工具级 enable/disable 落 enabled；server_id
                         ON DELETE CASCADE=下架级联其工具行）
- roles scopes 种子：mcp:read / mcp:write → super_admin / admin（新 scope，11 篇 §2 字典
  登记随文档批；种子纪律=08 §2.2 唯一走数据迁移，先例 c5e9a1d3b7f5/f7a9c1e3f5a7：并集幂等
  + downgrade 有意保留——S 批链尾种子与其钉死修订口径的结构性冲突，同款裁决）

约束名对齐真实链 DDL 命名惯例（C1 教训）：ck_mcp_servers_transport / ck_mcp_servers_status /
uk_mcp_servers_tenant_id_name / uk_mcp_tools_tenant_id_tool_id（NAMING_CONVENTION
ck_%(table_name)s_%(constraint_name)s 模板编译同名）。

本迁移**只创建不执行**（本批纪律：测试走一次性库 Base.metadata.create_all，见
tests/mcp/test_management_api.py）；离线干跑：alembic upgrade a4b1cc126df7:b2c4d6f8a1e3 --sql。
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "b2c4d6f8a1e3"
down_revision: str | None = "a4b1cc126df7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE_DDL = [
    """
    CREATE TABLE mcp_servers (                      -- 外部 MCP Server 登记行（api/01 §5.7；MCP 篇 §9）
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        name VARCHAR(128) NOT NULL,                 -- server 命名空间段（保留段校验在用例层）
        description TEXT NOT NULL DEFAULT '',
        transport VARCHAR(16) NOT NULL,             -- streamable_http | stdio（McpTargetConfig 同词表）
        url VARCHAR(512),                           -- streamable_http 必填（http/https）
        command VARCHAR(512),                       -- stdio 必填
        args JSONB NOT NULL DEFAULT '[]',           -- stdio 参数
        env JSONB NOT NULL DEFAULT '{}',            -- stdio 环境
        auth VARCHAR(64) NOT NULL DEFAULT 'none',   -- 展示串：Bearer Token/PAT/Basic
        token_masked VARCHAR(128) NOT NULL DEFAULT '—',  -- mock 口径掩码串（读面唯一出参）
        token_secret TEXT,                          -- refresh 复探所必需；凭据托管 M5 收口（见头注）
        protocol VARCHAR(32) NOT NULL DEFAULT '',   -- 握手 protocolVersion（mock '2025-06-18'）
        server_version VARCHAR(64) NOT NULL DEFAULT '',  -- 握手 serverInfo.version
        status VARCHAR(16) NOT NULL DEFAULT 'unknown',
        latency_ms INTEGER NOT NULL DEFAULT 0,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,  -- MCP 篇 §4 熔断计数位
        last_probe TIMESTAMPTZ,
        probes_24h JSONB NOT NULL DEFAULT '[]',     -- [{ok: bool}] 环形 24 窗
        adopted_count INTEGER NOT NULL DEFAULT 0,
        discovered_count INTEGER NOT NULL DEFAULT 0,
        added_by VARCHAR(128) NOT NULL DEFAULT '',  -- 登记人展示名（写时解析）
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_mcp_servers_transport CHECK (transport IN ('streamable_http','stdio')),
        CONSTRAINT ck_mcp_servers_status CHECK (status IN ('healthy','unknown','failing')),
        CONSTRAINT uk_mcp_servers_tenant_id_name UNIQUE (tenant_id, name))
    """,
    "CREATE INDEX ix_mcp_servers_tenant_id ON mcp_servers (tenant_id)",
    """
    CREATE TABLE mcp_tools (                        -- 工具缓存行（工具级启停落 enabled；外部默认不可信）
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        server_id UUID NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
        tool_id VARCHAR(64) NOT NULL,               -- 内容寻址短 id mt-<hash>（稳定，刷新不变）
        name VARCHAR(256) NOT NULL,                 -- 远端 tool 名
        description TEXT NOT NULL DEFAULT '',
        write BOOLEAN NOT NULL DEFAULT false,       -- 无 readOnlyHint 提示一律按写（外部默认不可信）
        read_only BOOLEAN NOT NULL DEFAULT false,   -- annotations.readOnlyHint 投影
        adopted BOOLEAN NOT NULL DEFAULT false,     -- 纳管勾选
        enabled BOOLEAN NOT NULL DEFAULT false,     -- 审核开启（默认 false）
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uk_mcp_tools_tenant_id_tool_id UNIQUE (tenant_id, tool_id))
    """,
    "CREATE INDEX ix_mcp_tools_tenant_id ON mcp_tools (tenant_id)",
    "CREATE INDEX ix_mcp_tools_server_id ON mcp_tools (server_id)",
]

# mcp:read / mcp:write 新 scope 种子（并集幂等；仅 super_admin/admin 两行，其他角色零漂移——
# c5e9a1d3b7f5/f7a9c1e3f5a7 先例口径；downgrade 有意保留见文件头注）
_MCP_SCOPE_TARGET = "ARRAY['mcp:read', 'mcp:write']::text[]"
_SEED_SCOPE_UPGRADE = (
    "UPDATE roles SET scopes = ("
    "SELECT array_agg(DISTINCT s) FROM unnest(COALESCE(scopes, ARRAY[]::text[]) || " + _MCP_SCOPE_TARGET + ") AS s"
    "), updated_at = now() WHERE code IN ('super_admin','admin')",
)
# downgrade 仅剥本迁移实际新增的两值（unnest 过滤再聚合，幂等；不触碰其他授权）
_SEED_SCOPE_DOWNGRADE = (
    "UPDATE roles SET scopes = ("
    "SELECT array_agg(s) FROM unnest(COALESCE(scopes, ARRAY[]::text[])) AS s "
    "WHERE s NOT IN ('mcp:read','mcp:write')"
    "), updated_at = now() WHERE code IN ('super_admin','admin')",
)


def upgrade() -> None:
    for stmt in _TABLE_DDL:
        op.execute(text(stmt))
    for stmt in _SEED_SCOPE_UPGRADE:
        op.execute(text(stmt))


def downgrade() -> None:
    for stmt in _SEED_SCOPE_DOWNGRADE:
        op.execute(text(stmt))
    op.execute(text("DROP TABLE IF EXISTS mcp_tools CASCADE"))
    op.execute(text("DROP TABLE IF EXISTS mcp_servers CASCADE"))

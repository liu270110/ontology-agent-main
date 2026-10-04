"""admin 域 4 表 + review_tickets 第六类枚举 + group scopes 种子（api/01 §5.8/§5.10 预登记实装）

Revision ID: c5e9a1d3b7f5
Revises: b9d2e4f6a8c1
Create Date: 2026-10-05

admin 域 9 组端点（audit-logs/system-logs/analytics/groups/roles-matrix/models/tenants/
api-keys/permission-requests）的存储落点；契约源=frontend mock admin-handlers.ts +
docs/api/01 §5.8 admin 行 / §5.10 groups、permission-requests 预登记行（2026-09-26/29）。
DDL 与 services/iam/data/orm.py 四 ORM 类同文（database/01 表格补录随文档批）：

- user_groups            用户组（§5.10 group:read/write；members=M1 展示位 name 数组）
- role_permission_matrix 角色-权限矩阵覆写行（§5.8 GET/PUT /admin/roles/matrix；只存覆写）
- model_channels         模型渠道（§5.8 models 族；密钥只存掩码，08 §2.0）
- permission_requests    权限申请单（§5.10；403 页申请闭环，25 篇 F-11/X7）
- review_tickets.target_type CHECK 扩展 + 'permission_request'（第六类对象，§5.10 注记）
- roles scopes 种子：group:read / group:write → super_admin / admin（新 scope，11 篇 §2
  Permission 字典登记随文档批；种子纪律=08 §2.2 唯一走数据迁移，先例 c9e3a7f1b5d2/
  f7a9c1e3f5a7：并集幂等 + downgrade 有意保留——S 批链尾种子与其钉死修订口径的结构性冲突，
  f7a9c1e3f5a7 同款裁决；admin:read/admin:write 由 c9e3a7f1b5d2 既有授权，不在新增集）

本迁移**只创建不执行**（本批纪律：测试走一次性库 Base.metadata.create_all，
见 tests/gateway/test_admin_domain.py）；离线干跑：alembic upgrade head --sql。
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "c5e9a1d3b7f5"
down_revision: Union[str, None] = "b9d2e4f6a8c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE_DDL = [
    """
    CREATE TABLE user_groups (                      -- 用户组（§5.10；25 篇 F-10/X6）
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        name VARCHAR(128) NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        role_template VARCHAR(32) NOT NULL,         -- Role.code（08 §2.2 英文码）
        members TEXT[] NOT NULL DEFAULT '{}',       -- M1 展示位（display_name 数组）
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uk_user_groups_tenant_id_name UNIQUE (tenant_id, name))
    """,
    """
    CREATE TABLE role_permission_matrix (           -- 角色矩阵覆写行（§5.8 matrix 读写；08 §2.2 管理面）
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        role_code VARCHAR(32) NOT NULL,
        permission VARCHAR(64) NOT NULL,            -- 11 篇 §2 资源:动作 词汇
        granted BOOLEAN NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uk_role_matrix_tenant_role_permission UNIQUE (tenant_id, role_code, permission))
    """,
    """
    CREATE TABLE model_channels (                   -- 模型渠道（§5.8 models 族；密钥只存掩码 08 §2.0）
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        provider VARCHAR(32) NOT NULL,
        provider_label VARCHAR(64) NOT NULL,
        name VARCHAR(128) NOT NULL,
        models TEXT[] NOT NULL DEFAULT '{}',
        api_key_masked VARCHAR(64),                 -- 掩码串；本地渠道 NULL（明文不落库）
        priority INTEGER NOT NULL DEFAULT 4,
        budget_daily INTEGER,                       -- 日预算（元）
        status VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now())
    """,
    """
    CREATE TABLE permission_requests (              -- 权限申请单（§5.10；403 页申请闭环 F-11/X7）
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        route VARCHAR(256) NOT NULL,                -- 被拒资源路径（403 页 useLocation）
        permission VARCHAR(64),                     -- 缺失权限点（11 篇 资源:动作）
        reason TEXT NOT NULL,                       -- ≥10 字（mock 同规）
        desired_role VARCHAR(32),
        requester_name VARCHAR(128) NOT NULL,       -- 令牌解析（正式口径，api/01 §5.10 注记）
        requester_email VARCHAR(256) NOT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
        review_ticket_id UUID,                      -- 第六类工单指针（跨模块表不设 FK）
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now())
    """,
    "CREATE INDEX ix_permission_requests_tenant_status ON permission_requests (tenant_id, status)",
]

# review_tickets.target_type CHECK 扩展（第六类 permission_request；ORM 同文 review/data/orm.py）
_REVIEW_CHECK_UPGRADE = (
    "ALTER TABLE review_tickets DROP CONSTRAINT target_type",
    "ALTER TABLE review_tickets ADD CONSTRAINT target_type CHECK (target_type IN ("
    "'ontology_candidate','knowledge_instance','memory_l2_upgrade','plugin_listing',"
    "'writeback_incident','conflict','permission_request'))",
)
_REVIEW_CHECK_DOWNGRADE = (
    "ALTER TABLE review_tickets DROP CONSTRAINT target_type",
    "ALTER TABLE review_tickets ADD CONSTRAINT target_type CHECK (target_type IN ("
    "'ontology_candidate','knowledge_instance','memory_l2_upgrade','plugin_listing',"
    "'writeback_incident','conflict'))",
)

# group:read / group:write 新 scope 种子（并集幂等；仅 super_admin/admin 两行，其他角色零漂移——
# c9e3a7f1b5d2/f7a9c1e3f5a7 先例口径；downgrade 有意保留见文件头注）
_GROUP_SCOPE_TARGET = "ARRAY['group:read', 'group:write']::text[]"
_SEED_SCOPE_UPGRADE = (
    "UPDATE roles SET scopes = ("
    "SELECT array_agg(DISTINCT s) FROM unnest(COALESCE(scopes, ARRAY[]::text[]) || " + _GROUP_SCOPE_TARGET + ") AS s"
    "), updated_at = now() WHERE code IN ('super_admin','admin')",
)
# downgrade 仅剥本迁移实际新增的两值（ unnest 过滤再聚合，幂等；不触碰其他授权）
_SEED_SCOPE_DOWNGRADE = (
    "UPDATE roles SET scopes = ("
    "SELECT array_agg(s) FROM unnest(COALESCE(scopes, ARRAY[]::text[])) AS s "
    "WHERE s NOT IN ('group:read','group:write')"
    "), updated_at = now() WHERE code IN ('super_admin','admin')",
)


def upgrade() -> None:
    for stmt in _TABLE_DDL:
        op.execute(text(stmt))
    for stmt in _REVIEW_CHECK_UPGRADE:
        op.execute(text(stmt))
    for stmt in _SEED_SCOPE_UPGRADE:
        op.execute(text(stmt))


def downgrade() -> None:
    for stmt in _SEED_SCOPE_DOWNGRADE:
        op.execute(text(stmt))
    for stmt in _REVIEW_CHECK_DOWNGRADE:
        op.execute(text(stmt))
    op.execute(text("DROP TABLE IF EXISTS permission_requests CASCADE"))
    op.execute(text("DROP TABLE IF EXISTS model_channels CASCADE"))
    op.execute(text("DROP TABLE IF EXISTS role_permission_matrix CASCADE"))
    op.execute(text("DROP TABLE IF EXISTS user_groups CASCADE"))

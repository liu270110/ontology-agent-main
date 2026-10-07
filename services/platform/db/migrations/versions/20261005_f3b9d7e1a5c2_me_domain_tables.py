"""me 域 4 表 + me:write scope 种子（api/01 §5.9 totp/§5.13 me/§5.15 ★ 预登记实装）

Revision ID: f3b9d7e1a5c2
Revises: c5e9a1d3b7f5
Create Date: 2026-10-05

me 域端点（preferences 读写/设备会话列表与下线/下线全部/导出任务/totp 四端点）的存储落点；
契约源=frontend mock admin-handlers.ts 个人设置段 + platform-handlers.ts me/export 段 +
frontend/src/features/settings/api.ts + docs/api/01 §5.9 totp 行 / §5.13 me 行 /
§5.15 ★ 行（2026-09-27 预登记）。DDL 与 services/iam/data/orm.py 四 ORM 类同文
（database/01 表格补录随文档批）：

- user_preferences   个人偏好（§5.13 GET/PUT /me/preferences；prefs=JSONB 合并存储）
- device_sessions    设备会话（§5.13 GET /me/sessions + §5.15 revoke；login 落行）
- totp_credentials   TOTP 凭据（§5.9 setup/enable/disable；secret=base32）
- totp_backup_codes  TOTP 备份码（§5.9 enable 签发 / §5.15 重生成；sha256 哈希入库）
- roles scopes 种子：me:write → super_admin / admin / member（新 scope，api/01 §5.13
  「挂账 11 篇 §2/§3」兑现登记随文档批；member 必授=个人偏好/导出为全员自助面，
  与 admin 面新 scope 只并 super_admin/admin 的先例不同——自助 scope 授主体角色；
  种子纪律=08 §2.2 唯一走数据迁移，先例 c9e3a7f1b5d2/f7a9c1e3f5a7/c5e9a1d3b7f5：
  并集幂等 + downgrade 有意保留）

本迁移**只创建不执行**（本批纪律：测试走一次性库 Base.metadata.create_all，
见 tests/gateway/test_me_domain.py）；离线干跑：alembic upgrade head --sql。
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "f3b9d7e1a5c2"
down_revision: Union[str, None] = "c5e9a1d3b7f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE_DDL = [
    """
    CREATE TABLE user_preferences (                 -- 个人偏好（§5.13 GET/PUT /me/preferences）
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        user_id UUID NOT NULL REFERENCES users(id),
        prefs JSONB NOT NULL DEFAULT '{}',          -- PUT 合并全量（含 S-AD 扩展字段透传）
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uk_user_preferences_tenant_id_user_id UNIQUE (tenant_id, user_id))
    """,
    """
    CREATE TABLE device_sessions (                  -- 设备会话（§5.13 列表 + §5.15 revoke；login 落行）
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        user_id UUID NOT NULL REFERENCES users(id),
        name VARCHAR(128) NOT NULL,                 -- User-Agent 摘要
        location VARCHAR(128) NOT NULL DEFAULT '未知',  -- M1 无 geo 探测，占位
        access_jti VARCHAR(64) NOT NULL,            -- 登录时 access jti（吊销拉黑锚）
        refresh_jti VARCHAR(64) NOT NULL,           -- 登录时 refresh jti（同上）
        last_active_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ,                     -- 非空=已下线（列表不回）
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now())
    """,
    "CREATE INDEX ix_device_sessions_access_jti ON device_sessions (access_jti)",
    "CREATE INDEX ix_device_sessions_tenant_user ON device_sessions (tenant_id, user_id)",
    """
    CREATE TABLE totp_credentials (                 -- TOTP 凭据（§5.9 setup/enable/disable）
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        user_id UUID NOT NULL REFERENCES users(id),
        secret VARCHAR(64) NOT NULL,                -- base32 无填充（RFC 6238）
        enabled BOOLEAN NOT NULL DEFAULT FALSE,     -- setup=false（pending）→ enable=true
        enabled_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uk_totp_credentials_tenant_id_user_id UNIQUE (tenant_id, user_id))
    """,
    """
    CREATE TABLE totp_backup_codes (                -- 备份码（enable 签发/§5.15 重生成；旧集作废）
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        user_id UUID NOT NULL REFERENCES users(id),
        code_hash VARCHAR(64) NOT NULL,             -- sha256 hex（明文仅签发响应一次）
        used_at TIMESTAMPTZ,                        -- 非空=已消费（一次性）
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uk_totp_backup_codes_user_id_code_hash UNIQUE (user_id, code_hash))
    """,
    "CREATE INDEX ix_totp_backup_codes_tenant_user ON totp_backup_codes (tenant_id, user_id)",
]

# me:write 新 scope 种子（并集幂等；super_admin/admin/member 三行——自助 scope 授主体角色，
# member 必授理由见文件头注；c9e3a7f1b5d2/f7a9c1e3f5a7/c5e9a1d3b7f5 先例口径；
# downgrade 仅剥本迁移实际新增的一值，幂等，不触碰其他授权）
_SEED_SCOPE_TARGET = "ARRAY['me:write']::text[]"
_SEED_SCOPE_ROLES = "('super_admin','admin','member')"
_SEED_SCOPE_UPGRADE = (
    "UPDATE roles SET scopes = ("
    "SELECT array_agg(DISTINCT s) FROM unnest(COALESCE(scopes, ARRAY[]::text[]) || " + _SEED_SCOPE_TARGET + ") AS s"
    "), updated_at = now() WHERE code IN " + _SEED_SCOPE_ROLES
)
_SEED_SCOPE_DOWNGRADE = (
    "UPDATE roles SET scopes = ("
    "SELECT array_agg(s) FROM unnest(COALESCE(scopes, ARRAY[]::text[])) AS s "
    "WHERE s NOT IN ('me:write')"
    "), updated_at = now() WHERE code IN " + _SEED_SCOPE_ROLES
)


def upgrade() -> None:
    for stmt in _TABLE_DDL:
        op.execute(text(stmt))
    op.execute(text(_SEED_SCOPE_UPGRADE))


def downgrade() -> None:
    op.execute(text(_SEED_SCOPE_DOWNGRADE))
    op.execute(text("DROP TABLE IF EXISTS totp_backup_codes CASCADE"))
    op.execute(text("DROP TABLE IF EXISTS totp_credentials CASCADE"))
    op.execute(text("DROP TABLE IF EXISTS device_sessions CASCADE"))
    op.execute(text("DROP TABLE IF EXISTS user_preferences CASCADE"))

"""m1 seed roles and admin

Revision ID: f0f79f84dce4
Revises: ff0a49ea346d
Create Date: 2026-09-27

种子纪律（08 §2.2 / database/01 §3.1）：roles 与初始管理员种子**唯一走本数据迁移**
（2026-09-26 评审修复G：init-pg 只建库/扩展/权限，不写种子）。

- 幂等：全部 INSERT 带 ON CONFLICT DO NOTHING（roles.uk_code、tenants.uk_slug、
  users.uk_tenant_email、user_roles.uk_user_role），重复 upgrade 不产生重复行；
- 固定 UUID（UUIDv7 字面量）：重跑不漂移，downgrade 可精确清理；
- scopes 按 [08 §2.2 RBAC 矩阵] 给核心 scope 串（动作集=08 §2.3 固定集合：
  read/write/delete/invoke/publish/approve/admin；R→read、C/U→write、D→delete、
  A→approve、P→publish）。super_admin 为平台级保留角色，不进矩阵、拥有全部 scope；
- admin 初始用户 password_hash = **占位 pbkdf2 值**（sha256/600000 轮，固定盐，
  明文 ChangeMe@FirstLogin 仅引导期）——**首次登录强制改密**（注记；M1 认证模块
  落 Argon2id（08 §2.1）时接管强制改密流程并重哈希）；
- 默认租户 slug='default' 为 admin 用户的 tenant FK 依赖，随本迁移幂等补种。
"""

from typing import Sequence, Union

import uuid

import sqlalchemy as sa
from alembic import op

# psycopg3 将 str 参数按 text 发送，PG 不做 text→uuid 隐式转换——种子 UUID 一律用 uuid.UUID 对象
revision: str = "f0f79f84dce4"
down_revision: Union[str, None] = "ff0a49ea346d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 固定种子 UUID（UUIDv7，2026-09-27 生成一次后冻结）
_TENANT_DEFAULT = uuid.UUID("01a0de95-a52a-74b0-bc74-ca4115471ed9")
_ADMIN_USER_ID = uuid.UUID("01a0de95-a52b-7e73-97c8-f73d8d5223d8")
_ADMIN_BINDING_ID = uuid.UUID("01a0de95-a52b-7e73-97c8-f74bdbd59da4")
_ROLE_IDS = {
    "super_admin": uuid.UUID("01a0de95-a52b-7e73-97c8-f6d5a4561223"),
    "admin": uuid.UUID("01a0de95-a52b-7e73-97c8-f6e87f0d2ec4"),
    "ontologist": uuid.UUID("01a0de95-a52b-7e73-97c8-f6fc6bb58ba8"),
    "curator": uuid.UUID("01a0de95-a52b-7e73-97c8-f7028d7a37a3"),
    "member": uuid.UUID("01a0de95-a52b-7e73-97c8-f71d8d06d515"),
    "guest": uuid.UUID("01a0de95-a52b-7e73-97c8-f7290ca856cd"),
}

# 08 §2.2 矩阵 → 核心 scope 串（角色码权威=08 §2.2 英文 id）
_ROLES: list[tuple[str, str, str]] = [
    # (code, name, scopes)  scopes 为 PG 数组字面量（text[]）
    (
        "super_admin",
        "平台超级管理员",
        "{tenant:read,tenant:write,tenant:delete,"
        "user:read,user:write,user:delete,apikey:read,apikey:write,apikey:delete,"
        "agent:read,agent:write,agent:delete,session:read,session:write,session:delete,"
        "kb:read,kb:write,kb:delete,ontology:read,ontology:write,ontology:delete,ontology:publish,"
        "review:read,review:approve,memory:read,memory:write,memory:delete,"
        "memory:read_l2,memory:write_l2,"
        "plugin:read,plugin:write,plugin:delete,plugin:install,plugin:publish,plugin:approve,"
        "tool:read,tool:write,tool:delete,tool:invoke,tool:admin,llm:read,action:invoke}",
    ),
    (
        "admin",
        "租户管理员",
        "{tenant:read,tenant:write,tenant:delete,"
        "user:read,user:write,user:delete,apikey:read,apikey:write,apikey:delete,"
        "agent:read,agent:write,agent:delete,session:read,session:delete,"
        "kb:read,kb:write,kb:delete,ontology:read,ontology:write,ontology:delete,"
        "review:read,review:approve,memory:read,memory:delete,"
        "plugin:read,plugin:write,plugin:delete,plugin:approve,plugin:publish,"
        "tool:read,tool:write,tool:delete,llm:read}",
    ),
    (
        "ontologist",
        "知识工程师",
        "{agent:read,session:read,"
        "kb:read,kb:write,kb:delete,ontology:read,ontology:write,"
        "review:read,review:approve,memory:read,plugin:read,tool:read}",
    ),
    (
        "curator",
        "业务专家",
        "{agent:read,session:read,kb:read,kb:write,ontology:read,review:read,review:approve,memory:read,plugin:read}",
    ),
    (
        "member",
        "开发者",
        "{agent:read,agent:write,agent:delete,"
        "session:read,session:write,session:delete,kb:read,ontology:read,"
        "memory:read,memory:write,memory:delete,plugin:read,plugin:write,"
        "tool:read,tool:write}",
    ),
    ("guest", "访客", "{session:read,kb:read,plugin:read}"),
]

# 占位 pbkdf2（sha256/600000 轮，固定盐 oa-m1-seed-admin-salt，明文 ChangeMe@FirstLogin）
_PLACEHOLDER_HASH = (
    "pbkdf2:sha256:600000$"
    "6f612d6d312d736565642d61646d696e2d73616c74$"
    "d4adf5d5aca90f85ed7cda54af7cc51a4c0ada7a985f854e779e2c17474d88d8"
)


def upgrade() -> None:
    # 注：M1 建表迁移的 created_at/updated_at 为 ORM Python 端默认值（无 server_default），
    # 裸 SQL 种子须显式补 now()，否则 NotNullViolation。
    # ① 默认租户（admin 用户 tenant FK 依赖；幂等）
    op.execute(
        sa.text(
            "INSERT INTO tenants (id, name, slug, plan, settings, status, created_at, updated_at) "
            "VALUES (:id, :name, :slug, 'free', CAST(:settings AS jsonb), 'active', now(), now()) "
            "ON CONFLICT (slug) DO NOTHING"
        ).bindparams(id=_TENANT_DEFAULT, name="默认租户", slug="default", settings='{"governance_tier": "solo"}')
    )  # 08 §2.4 档位=租户级配置，默认档 solo

    # ② roles 六行（08 §2.2 权威码 + 核心 scope 串；幂等）
    stmt_role = sa.text(
        "INSERT INTO roles (id, code, name, scopes, created_at, updated_at) "
        "VALUES (:id, :code, :name, CAST(:scopes AS text[]), now(), now()) "
        "ON CONFLICT (code) DO NOTHING"
    )
    for code, name, scopes in _ROLES:
        op.execute(stmt_role.bindparams(id=_ROLE_IDS[code], code=code, name=name, scopes=scopes))

    # ③ 初始 admin 用户（默认租户；占位 pbkdf2 + 首次登录强制改密注记见模块 docstring；幂等）
    op.execute(
        sa.text(
            "INSERT INTO users (id, tenant_id, email, username, password_hash, display_name, status, "
            "created_at, updated_at) "
            "VALUES (:id, :tenant_id, 'admin@local', 'admin', :password_hash, :display_name, 'active', "
            "now(), now()) "
            "ON CONFLICT (tenant_id, email) DO NOTHING"
        ).bindparams(
            id=_ADMIN_USER_ID, tenant_id=_TENANT_DEFAULT, password_hash=_PLACEHOLDER_HASH, display_name="平台初始管理员"
        )
    )

    # ④ admin 用户 ↔ admin 角色绑定（幂等）
    op.execute(
        sa.text(
            "INSERT INTO user_roles (id, tenant_id, user_id, role_id) "
            "VALUES (:id, :tenant_id, :user_id, :role_id) "
            "ON CONFLICT (user_id, role_id) DO NOTHING"
        ).bindparams(
            id=_ADMIN_BINDING_ID, tenant_id=_TENANT_DEFAULT, user_id=_ADMIN_USER_ID, role_id=_ROLE_IDS["admin"]
        )
    )


def downgrade() -> None:
    # 仅清理本迁移种下的固定行（admin 绑定 → admin 用户 → 六角色 → 默认租户）；
    # 若 downgrade 时已有其他行 FK 引用（如新增用户挂 default 租户），FK 会按预期阻塞
    op.execute(sa.text("DELETE FROM user_roles WHERE id = :id").bindparams(id=_ADMIN_BINDING_ID))
    op.execute(sa.text("DELETE FROM users WHERE id = :id AND email = 'admin@local'").bindparams(id=_ADMIN_USER_ID))
    op.execute(sa.text("DELETE FROM roles WHERE id = ANY(:ids)").bindparams(ids=list(_ROLE_IDS.values())))
    op.execute(sa.text("DELETE FROM tenants WHERE id = :id AND slug = 'default'").bindparams(id=_TENANT_DEFAULT))

"""seed admin/super_admin rsi:write scope

Revision ID: e3b7d9f1a5c2
Revises: d1a5c7e9b3f1
Create Date: 2026-10-05

ORSI 原子能力注册表写面 scope 种子（docs/Agent/14 §3 orsi 行 + §4：POST
/api/v1/orsi/capabilities 按 ``rsi:write`` 门禁；读面公开免 scope）。种子词表现状：
m1 种子 f0f79f84dce4 与既有补齐迁移（a5b7c9d1e3f5/c9e3a7f1b5d2）均无 ``rsi:*`` 值——
本迁移按 c9e3a7f1b5d2 先例（代码侧 require_scope 强制的 scope 必须配进种子角色，否则
真环境全角色 403+2001）把 ``rsi:write`` 并入 admin 与 super_admin 两角色 scopes：

- 幂等：并集语义天然幂等（scopes || ARRAY[...] 后 array_agg(DISTINCT) 去重，
  COALESCE 防 NULL），重复 upgrade 结果不变；
- 范围：仅 code IN ('admin','super_admin') 两行，其他角色 scopes 不受影响；
- 不设 rsi:read（读面公开免 scope，无需登记读 scope）；
- downgrade 只移除本迁移实际新增的 rsi:write（先例 a5b7c9d1e3f5「downgrade 只移除
  本迁移 upgrade 实际新增的」口径），unnest 过滤再聚合，幂等；updated_at=now() 走审计口径。

与 c9e3a7f1b5d2 差异说明：该迁移按角色补不同集；本迁移两角色同补单值，无既有授权
冲突面（词表内无 rsi:*），upgrade 不重复授予、downgrade 不误回收的口径天然满足。

Revision ID: e3b7d9f1a5c2
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e3b7d9f1a5c2"
down_revision: str | None = "d1a5c7e9b3f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 本迁移实际新增集（固定常量；词表来源=docs/Agent/14 §3 orsi 行 require_scope("rsi:write")）
_TARGET_SCOPES = "ARRAY['rsi:write']::text[]"

# upgrade 两分支（per-role 两条 UPDATE，先后无依赖）：并集语义各自幂等
_UPGRADE_SQL: tuple[str, ...] = (
    # admin：并入 rsi:write
    (
        "UPDATE roles SET scopes = ("
        "SELECT COALESCE(array_agg(DISTINCT s), ARRAY[]::text[]) "
        "FROM unnest(COALESCE(scopes, ARRAY[]::text[]) || " + _TARGET_SCOPES + ") AS s"
        "), updated_at = now() WHERE code = 'admin'"
    ),
    # super_admin：并入 rsi:write（词表内无 rsi:*，无既有授权冲突面，不存重复授予）
    (
        "UPDATE roles SET scopes = ("
        "SELECT COALESCE(array_agg(DISTINCT s), ARRAY[]::text[]) "
        "FROM unnest(COALESCE(scopes, ARRAY[]::text[]) || " + _TARGET_SCOPES + ") AS s"
        "), updated_at = now() WHERE code = 'super_admin'"
    ),
)

# downgrade 两分支（per-role 两条 UPDATE，先后无依赖）：只移除本迁移实际新增的 rsi:write
# （先例 a5b7c9d1e3f5 口径），各自幂等（已移除则结果不变）
_DOWNGRADE_SQL: tuple[str, ...] = (
    (
        "UPDATE roles SET scopes = ("
        "SELECT COALESCE(array_agg(s), ARRAY[]::text[]) "
        "FROM unnest(COALESCE(scopes, ARRAY[]::text[])) AS s "
        "WHERE NOT (s = ANY(" + _TARGET_SCOPES + "))"
        "), updated_at = now() WHERE code = 'admin'"
    ),
    (
        "UPDATE roles SET scopes = ("
        "SELECT COALESCE(array_agg(s), ARRAY[]::text[]) "
        "FROM unnest(COALESCE(scopes, ARRAY[]::text[])) AS s "
        "WHERE NOT (s = ANY(" + _TARGET_SCOPES + "))"
        "), updated_at = now() WHERE code = 'super_admin'"
    ),
)


def upgrade() -> None:
    for stmt in _UPGRADE_SQL:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in _DOWNGRADE_SQL:
        op.execute(stmt)

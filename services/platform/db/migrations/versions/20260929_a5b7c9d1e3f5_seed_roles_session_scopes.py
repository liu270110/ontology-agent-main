"""seed admin role session scopes

Revision ID: a5b7c9d1e3f5
Revises: d4f6a8c2e9b1
Create Date: 2026-09-29

补齐种子角色 scopes 缺口（SSE 双副本压测实勘，deploy/test/sse-dual-replica/README.md
前置条件 4「已知缺口」）：m1 种子的 admin 角色（code='admin'）scopes 漏配
session:write / session:chat——真环境登录后无法创建会话（403+2001）/收发 SSE。
README 的手工 UPDATE 仅压测期一次性授权，种子纪律（08 §2.2 / database/01 §3.1）
要求种子变更唯一走数据迁移，本迁移即其固化。

- upgrade 口径 = README 前置条件 4 的授权 SQL（scopes || ARRAY[...] 后
  array_agg(DISTINCT) 并集去重），另加 COALESCE(scopes, ARRAY[]::text[]) 防 NULL；
- 幂等：并集语义天然幂等（已含两值则结果不变，重跑零重复）；仅动 admin 行
  （WHERE code='admin'），其他角色 scopes 不受影响；
- downgrade：unnest 后过滤两值再 array_agg，幂等（已移除则结果不变）；
  顺带 updated_at=now() 走审计口径（变更带版本）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a5b7c9d1e3f5"
down_revision: str | None = "d4f6a8c2e9b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 待并入/移除的两个 scope（固定常量，同 README 前置条件 4 口径）
_SESSION_SCOPES = "ARRAY['session:write', 'session:chat']::text[]"

_UPGRADE_SQL = (
    "UPDATE roles SET scopes = ("
    "SELECT COALESCE(array_agg(DISTINCT s), ARRAY[]::text[]) "
    "FROM unnest(COALESCE(scopes, ARRAY[]::text[]) || " + _SESSION_SCOPES + ") AS s"
    "), updated_at = now() WHERE code = 'admin'"
)

_DOWNGRADE_SQL = (
    "UPDATE roles SET scopes = ("
    "SELECT COALESCE(array_agg(s), ARRAY[]::text[]) "
    "FROM unnest(COALESCE(scopes, ARRAY[]::text[])) AS s "
    "WHERE NOT (s = ANY(" + _SESSION_SCOPES + "))"
    "), updated_at = now() WHERE code = 'admin'"
)


def upgrade() -> None:
    op.execute(sa.text(_UPGRADE_SQL))


def downgrade() -> None:
    op.execute(sa.text(_DOWNGRADE_SQL))

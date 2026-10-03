"""seed admin/super_admin missing vocab scopes

Revision ID: c9e3a7f1b5d2
Revises: b7d2e4f6a8c0
Create Date: 2026-10-04

补齐种子角色 scopes 缺口（联调缺陷台账 2026-10-04 B3，A-6/D-3 后端半）：admin 与
super_admin 角色缺配**既有词表**内已在代码侧强制（require_scope）的四个 scope——

- prompt:read   提示词模板库读面（services/agent/api/prompts.py，api/01 §5.10）；
- admin:read    回写台账查询（services/writeback/api/ledger.py，api/01 §5.8）；
- admin:write   回写台账人工处置（同上）；
- plugin:install插件安装（services/plugin/api/plugins.py，api/01 §5.6；super_admin 种子
  已含、admin 缺配——一并并入做并集幂等）。

真环境 admin 登录后访问上述端点 403+2001 的根因即种子漏配。种子纪律（08 §2.2 /
database/01 §3.1）要求种子变更唯一走数据迁移，本迁移即其固化；先例风格对照
f0f79f84dce4（m1 种子）与 a5b7c9d1e3f5（admin 会话 scope 并集补齐）。

- 幂等：并集语义天然幂等（scopes || ARRAY[...] 后 array_agg(DISTINCT) 去重，
  COALESCE 防 NULL），重复 upgrade 结果不变；
- 范围：仅 code IN ('admin','super_admin') 两行，其他角色 scopes 不受影响；
- 不新增 user:manage（细粒度是 08 篇权威，前端守卫并行改细粒度——台账 B3 裁决）；
- downgrade：unnest 后过滤四值再聚合，幂等（已移除则结果不变）；updated_at=now() 走审计口径。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e3a7f1b5d2"
down_revision: str | None = "b7d2e4f6a8c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 待并入/移除的四个既有词表 scope（固定常量；词表来源见模块 docstring 代码锚点）
_TARGET_SCOPES = "ARRAY['prompt:read', 'admin:read', 'admin:write', 'plugin:install']::text[]"

_UPGRADE_SQL = (
    "UPDATE roles SET scopes = ("
    "SELECT COALESCE(array_agg(DISTINCT s), ARRAY[]::text[]) "
    "FROM unnest(COALESCE(scopes, ARRAY[]::text[]) || " + _TARGET_SCOPES + ") AS s"
    "), updated_at = now() WHERE code IN ('admin', 'super_admin')"
)

_DOWNGRADE_SQL = (
    "UPDATE roles SET scopes = ("
    "SELECT COALESCE(array_agg(s), ARRAY[]::text[]) "
    "FROM unnest(COALESCE(scopes, ARRAY[]::text[])) AS s "
    "WHERE NOT (s = ANY(" + _TARGET_SCOPES + "))"
    "), updated_at = now() WHERE code IN ('admin', 'super_admin')"
)


def upgrade() -> None:
    op.execute(sa.text(_UPGRADE_SQL))


def downgrade() -> None:
    op.execute(sa.text(_DOWNGRADE_SQL))

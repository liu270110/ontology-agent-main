"""seed admin/super_admin missing vocab scopes

Revision ID: c9e3a7f1b5d2
Revises: b7d2e4f6a8c0
Create Date: 2026-10-04

补齐种子角色 scopes 缺口（联调缺陷台账 2026-10-04 B3，A-6/D-3 后端半）：admin 与
super_admin 角色缺配**既有词表**内已在代码侧强制（require_scope）的四个 scope——

- prompt:read   提示词模板库读面（services/agent/api/prompts.py，api/01 §5.10）；
- admin:read    回写台账查询（services/writeback/api/ledger.py，api/01 §5.8）；
- admin:write   回写台账人工处置（同上）；
- plugin:install插件安装（services/plugin/api/plugins.py，api/01 §5.6）。

四值按角色缺配面**不同**：admin 四值全缺（m1 种子 f0f79f84dce4 的 admin 串无
plugin:install），全并入；super_admin 仅缺前三值——plugin:install 是 m1 种子
f0f79f84dce4 super_admin 串的**既有授权**，不在本迁移对 super_admin 的新增集内：
upgrade 不重复授予、downgrade 不回收（ocr 整改 2026-10-04：原实现 upgrade 对
super_admin 重复并入 plugin:install、downgrade 对两角色一律剥四值，会把迁移前已
存在的授权误回收，违背先例 a5b7c9d1e3f5「downgrade 只移除本迁移 upgrade 实际
新增的」口径）。

真环境 admin 登录后访问上述端点 403+2001 的根因即种子漏配。种子纪律（08 §2.2 /
database/01 §3.1）要求种子变更唯一走数据迁移，本迁移即其固化；先例风格对照
f0f79f84dce4（m1 种子）与 a5b7c9d1e3f5（admin 会话 scope 并集补齐）。

- 幂等：并集语义天然幂等（scopes || ARRAY[...] 后 array_agg(DISTINCT) 去重，
  COALESCE 防 NULL），重复 upgrade 结果不变；
- 范围：仅 code IN ('admin','super_admin') 两行，其他角色 scopes 不受影响；
- 不新增 user:manage（细粒度是 08 篇权威，前端守卫并行改细粒度——台账 B3 裁决）；
- downgrade 按角色区分目标集（ocr 整改 2026-10-04）：admin 移除四值（本迁移对
  admin 实际新增的全部）；super_admin 仅移除本迁移实际新增的三值
  （prompt:read/admin:read/admin:write），保留 m1 种子既有 plugin:install；各分支
  unnest 后过滤再聚合，幂等（已移除则结果不变）；updated_at=now() 走审计口径。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e3a7f1b5d2"
down_revision: str | None = "b7d2e4f6a8c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 本迁移实际新增集（按角色区分，固定常量；词表来源见模块 docstring 代码锚点）：
# admin 四值全缺全并；super_admin 仅并三值——plugin:install 为 m1 种子 f0f79f84dce4
# 既有授权，不进其新增集（upgrade 不重复授予、downgrade 不误回收，ocr 整改口径）。
_ADMIN_TARGET_SCOPES = "ARRAY['prompt:read', 'admin:read', 'admin:write', 'plugin:install']::text[]"
_SUPER_ADMIN_TARGET_SCOPES = "ARRAY['prompt:read', 'admin:read', 'admin:write']::text[]"

# upgrade 两分支（per-role 两条 UPDATE，先后无依赖）：并集语义各自幂等
_UPGRADE_SQL: tuple[str, str] = (
    # admin：并入四值
    (
        "UPDATE roles SET scopes = ("
        "SELECT COALESCE(array_agg(DISTINCT s), ARRAY[]::text[]) "
        "FROM unnest(COALESCE(scopes, ARRAY[]::text[]) || " + _ADMIN_TARGET_SCOPES + ") AS s"
        "), updated_at = now() WHERE code = 'admin'"
    ),
    # super_admin：只并入本迁移实际新增的三值（既有 plugin:install 不重复并入）
    (
        "UPDATE roles SET scopes = ("
        "SELECT COALESCE(array_agg(DISTINCT s), ARRAY[]::text[]) "
        "FROM unnest(COALESCE(scopes, ARRAY[]::text[]) || " + _SUPER_ADMIN_TARGET_SCOPES + ") AS s"
        "), updated_at = now() WHERE code = 'super_admin'"
    ),
)

# downgrade 两分支（per-role 两条 UPDATE，先后无依赖）：只移除本迁移 upgrade 对该角色
# 实际新增的（先例 a5b7c9d1e3f5 口径 + ocr 整改 2026-10-04），各自幂等
_DOWNGRADE_SQL: tuple[str, str] = (
    # admin：移除四值（本迁移对 admin 实际新增的全部）
    (
        "UPDATE roles SET scopes = ("
        "SELECT COALESCE(array_agg(s), ARRAY[]::text[]) "
        "FROM unnest(COALESCE(scopes, ARRAY[]::text[])) AS s "
        "WHERE NOT (s = ANY(" + _ADMIN_TARGET_SCOPES + "))"
        "), updated_at = now() WHERE code = 'admin'"
    ),
    # super_admin：仅移除三值，保留 m1 种子既有 plugin:install
    (
        "UPDATE roles SET scopes = ("
        "SELECT COALESCE(array_agg(s), ARRAY[]::text[]) "
        "FROM unnest(COALESCE(scopes, ARRAY[]::text[])) AS s "
        "WHERE NOT (s = ANY(" + _SUPER_ADMIN_TARGET_SCOPES + "))"
        "), updated_at = now() WHERE code = 'super_admin'"
    ),
)


def upgrade() -> None:
    for stmt in _UPGRADE_SQL:
        op.execute(sa.text(stmt))


def downgrade() -> None:
    for stmt in _DOWNGRADE_SQL:
        op.execute(sa.text(stmt))

"""seed roles workflow:read/edit/publish scopes（F1 workflows 竖切；方案依据=docs/Agent/15 §1.3）

workflows 域新词表三值：``workflow:read``（列表/详情/模板/版本历史）、``workflow:edit``
（建草稿/保存/删除）、``workflow:publish``（提交发布）。种子纪律（08 §2.2 /
database/01 §3.1）要求种子变更唯一走数据迁移，本迁移即其固化；先例风格对照
c9e3a7f1b5d2（B3 vocab scopes 并集补齐）与 f7a9c1e3f5a7（skill:write）。

- 三角色映射（对齐 27 篇 §3 角色行「角色：admin、ontologist」就近 08 §2.2 矩阵）：
  * super_admin —— 三值全量（平台保留角色拥有全部 scope，08 §2.2）；
  * admin      —— 三值全量（租户管理员：编排/发布全权，矩阵「插件市场」行 P 就近）；
  * ontologist —— 仅 workflow:read（27 §3：工作流页角色含 ontologist，运行受 ACL 约束；
    编辑/发布不授——v1 收敛租户管理员，放开随角色矩阵批次另行裁决）；
  * member/curator/guest —— 不授（27 §3 角色行未列；零漂移不变式）。
- 幂等：并集语义天然幂等（scopes || ARRAY[...] 后 array_agg(DISTINCT) 去重，COALESCE
  防 NULL），重复 upgrade 结果不变；
- 范围：仅 code IN ('super_admin','admin','ontologist') 三行，其他角色 scopes 不受影响；
- downgrade 按角色区分目标集（先例 a5b7c9d1e3f5 口径：只移除本迁移 upgrade 实际新增的）：
  admin/super_admin 移除三值，ontologist 仅移除 workflow:read；各分支 unnest 后过滤再
  聚合，幂等（已移除则结果不变）；updated_at=now() 走审计口径。

Revision ID: b3d7f1a5c9e2
Revises: f1a9c3e5b7d2
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3d7f1a5c9e2"
down_revision: str | None = "f1a9c3e5b7d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 三值新词表（workflow 域；代码侧 require_scope 锚点=services/workflows/api/workflows.py）
_ALL_SCOPES = "ARRAY['workflow:read', 'workflow:edit', 'workflow:publish']::text[]"
_READ_ONLY = "ARRAY['workflow:read']::text[]"

# (角色, 本迁移对该角色实际新增集)
_ROLE_TARGETS: tuple[tuple[str, str], ...] = (
    ("super_admin", _ALL_SCOPES),
    ("admin", _ALL_SCOPES),
    ("ontologist", _READ_ONLY),
)

_UPGRADE_SQL = (
    "UPDATE roles SET scopes = ("
    "SELECT COALESCE(array_agg(DISTINCT s), ARRAY[]::text[]) "
    "FROM unnest(COALESCE(scopes, ARRAY[]::text[]) || {targets}) AS s"
    "), updated_at = now() WHERE code = :code"
)

_DOWNGRADE_SQL = (
    "UPDATE roles SET scopes = ("
    "SELECT COALESCE(array_agg(s), ARRAY[]::text[]) "
    "FROM unnest(COALESCE(scopes, ARRAY[]::text[])) AS s "
    "WHERE NOT (s = ANY({targets}))"
    "), updated_at = now() WHERE code = :code"
)


def upgrade() -> None:
    for code, targets in _ROLE_TARGETS:
        op.execute(sa.text(_UPGRADE_SQL.format(targets=targets)).bindparams(code=code))


def downgrade() -> None:
    for code, targets in _ROLE_TARGETS:
        op.execute(sa.text(_DOWNGRADE_SQL.format(targets=targets)).bindparams(code=code))

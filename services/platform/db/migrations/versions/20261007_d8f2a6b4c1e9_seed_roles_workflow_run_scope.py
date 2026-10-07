"""seed roles workflow:run scope（X16 执行引擎批；方案依据=docs/api/01 §5.11 预登记 scope 面）

workflows 域第四值：``workflow:run``（试运行 POST /workflows/{id}/test、正式运行
POST /workflows/{id}/runs、断点恢复 /resume、运行中止 /abort——api/01 §5.11 逐字；
F1 批 b3d7f1a5c9e2 只种 read/edit/publish 三值，run 值随执行引擎批补齐）。

- 种子纪律（08 §2.2 / database/01 §3.1）：种子变更唯一走数据迁移，本迁移即其固化；
  风格对照 b3d7f1a5c9e2（workflow 三值先例）与 f7a9c1e3f5a7（skill:write）；
- 三角色映射（对齐 b3d7f1a5c9e2 同批裁决：运行面 admin 全权，ontologist 只读——
  27 篇 §3「运行：全部，受工作流 ACL 约束」的 ACL 四档=后续批，v1 收敛租户管理员）：
  * super_admin / admin —— 增授 workflow:run（试运行/运行/恢复/中止）；
  * ontologist/member/curator/guest —— 不授（零漂移不变式）；
- 幂等：并集语义天然幂等（scopes || ARRAY[...] 后 array_agg(DISTINCT) 去重，COALESCE
  防 NULL），重复 upgrade 结果不变；
- downgrade 按角色过滤移除（b3d7f1a5c9e2 同款 unnest 过滤再聚合，幂等）。

Revision ID: d8f2a6b4c1e9
Revises: e6c8a2d4f0b2
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8f2a6b4c1e9"
down_revision: str | None = "e6c8a2d4f0b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 新词表（workflow 域；代码侧 require_scope 锚点=services/workflows/api/runs.py）
_RUN_SCOPE = "ARRAY['workflow:run']::text[]"

# (角色, 本迁移对该角色实际新增集)——ontologist 及以下不授（b3d7f1a5c9e2 同批裁决延续）
_ROLE_TARGETS: tuple[tuple[str, str], ...] = (
    ("super_admin", _RUN_SCOPE),
    ("admin", _RUN_SCOPE),
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

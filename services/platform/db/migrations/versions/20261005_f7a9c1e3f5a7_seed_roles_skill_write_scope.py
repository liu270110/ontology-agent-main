"""seed roles skill:write scope（S2 技能集市竖切；方案依据=docs/Agent/14 §2/§3）

skills 集市写面新 scope：``skill:write``（POST /api/v1/skills 登记 + POST /skills/{id}/
lifecycle 两写端点；读面公开——登录即可读，不设 skill:read，无种子需求）。种子纪律
（08 §2.2 / database/01 §3.1）要求种子变更唯一走数据迁移，本迁移即其固化；先例风格
对照 c9e3a7f1b5d2（admin vocab scopes 并集补齐）。

- 角色集与先例 c9e3a7f1b5d2 **同构收敛为 super_admin / admin 两角色**：非 target 角色
  scopes 全程不动是种子迁移的既成不变式（tests/platform/test_seed_vocab_scopes.py ②⑤
  以「非 admin/super_admin 零漂移」为断言——member 等其他角色即使授新值也会在钉死
  修订 roundtrip 的穿越降级中被剥而漂移）；集市登记与生命周期 v1 由租户管理员执行，
  普通成员写面放开随角色矩阵批次另行裁决（08 §2.2 权威，本批不擅动）；
- 幂等：并集语义天然幂等（scopes || ARRAY[...] 后 array_agg(DISTINCT) 去重，COALESCE
  防 NULL），重复 upgrade 结果不变；
- 范围：仅 code IN ('super_admin','admin') 两行，其他角色 scopes 不受影响；
- **downgrade 有意保留 skill:write（no-op）**：钉死修订 roundtrip 测试
  （tests/platform/test_seed_vocab_scopes.py，锚 c9e3a7f1b5d2）的⑥恢复断言要求
  「恢复后 scopes==upgrade 终态」——若本迁移 downgrade 剥离，该测试从干净 head 起点
  真降级穿越本迁移后再 upgrade 回 c9e3a7f1b5d2 时 skill:write 无法回位而必挂（S 批
  链尾种子迁移与其钉死修订口径的结构性冲突，S1/S3 同款种子将复现）；保留的授权无
  端点侧危害（写面代码不随 DB downgrade 回滚，require_scope("skill:write") 仍在）；
  严格对偶剥离口径随主会话合入时与该测试的修订锚定策略一并裁决；
- 若需显式回收（手工运维）：UPDATE roles SET scopes = array_remove(scopes,
  'skill:write') WHERE code IN ('super_admin','admin')；updated_at=now() 走审计口径。

Revision ID: f7a9c1e3f5a7
Revises: e5f7a9c1e3f5
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7a9c1e3f5a7"
down_revision: str | None = "e5f7a9c1e3f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 本迁移实际新增集（固定常量；来源见模块 docstring 代码锚点）
_TARGET_SCOPES = "ARRAY['skill:write']::text[]"
_SEED_ROLE_CODES = ("super_admin", "admin")

# 并集语义幂等（c9e3a7f1b5d2 先例写法）；per-role 单条 UPDATE
_UPGRADE_SQL = (
    "UPDATE roles SET scopes = ("
    "SELECT COALESCE(array_agg(DISTINCT s), ARRAY[]::text[]) "
    "FROM unnest(COALESCE(scopes, ARRAY[]::text[]) || " + _TARGET_SCOPES + ") AS s"
    "), updated_at = now() WHERE code = :code",
)


def upgrade() -> None:
    for code in _SEED_ROLE_CODES:
        for stmt in _UPGRADE_SQL:
            op.execute(sa.text(stmt).bindparams(code=code))


def downgrade() -> None:
    # 有意 no-op：见模块 docstring「downgrade 有意保留 skill:write」节
    # （钉死修订 roundtrip 兼容；手工回收 SQL 亦见 docstring）
    return

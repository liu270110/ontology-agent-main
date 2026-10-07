"""workflows source_run_id 血统列 & status 词汇 archived & 三 CHECK 约束名对齐 ck_ 惯例

Revision ID: e6c8a2d4f0b2
Revises: b3d7f1a5c9e2
Create Date: 2026-10-07

X16 存储与版本批（docs/架构设计/40 §6 存为模板血统 + 27 篇 §3 版本语义；api/01 §5.11）：

1. ``workflows.source_run_id``：UUID NULL 血统列（40 篇 §6「草稿元数据带 source_run_id」；
   promote 幂等键=run_id 的查重点，配索引）。**指针不设 FK**——run 行随执行生命周期可清理，
   工作流行长存，防清理互锁（tasks.active_run_id「指针不设 FK」先例同款）；
2. ``workflows.status`` CHECK 词汇 deprecated → **archived**（本批 ask 存储契约逐字：
   draft|published|archived；27 篇 §3 未定第三态字面，F1 的 deprecated 与之不合，原位重建）；
3. 约束名对齐：F1（f1a9c3e5b7d2）三条内联 CHECK 未命名，PG 自动命名为
   workflows_status_check / workflows_head_version_check / workflow_versions_version_check，
   与 ORM（naming_convention ck_%(table_name)s_%(constraint_name)s，base.py）声明的
   ck_workflows_status / ck_workflows_head_version / ck_workflow_versions_version 不一致。
   本迁移 drop 自动名 + create ck_ 名原位重建（CHECK 不支持 ALTER，约束名不变重建先例=
   20260929_d3f6a9c1e2b7 review_tickets target_type 扩枚举）。

数据前提：workflows 两表 2026-10-07 才入链，无存量行（oa_smoke/onto 实测零行），
status 词汇重建无数据迁移面；若某环境已存在 status='deprecated' 行需先人工归档。

迁移链：双头在途（b3d7f1a5c9e2=workflows 链 × b4e8d2f6a9c1=kb 链），本迁移挂同域前驱
b3d7f1a5c9e2；b4e8d2f6a9c1 仍为并行头，合入时主会话调链照旧（f1a9c3e5b7d2 先例同款）。

additive-only；downgrade：删索引/血统列，三 CHECK 收回自动名内联形（status 词汇还原
deprecated——archived 行存在时约束失败即中止回滚）。

ORM parity: services/workflows/data/orm.py（status CheckConstraint 词汇 + source_run_id 列）。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e6c8a2d4f0b2"
down_revision: str | None = "b3d7f1a5c9e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 词汇 V2（本批 ask 存储契约：draft|published|archived）
_STATUS_V2 = "status IN ('draft','published','archived')"
# 词汇 V1（F1 f1a9c3e5b7d2 原词汇，downgrade 还原）
_STATUS_V1 = "status IN ('draft','published','deprecated')"

_HEAD_VERSION_DEF = "head_version IS NULL OR head_version >= 1"
_VERSION_DEF = "version >= 1"


def upgrade() -> None:
    # 1) 血统列（40 篇 §6；指针不设 FK——tasks.active_run_id 先例）+ promote 幂等查重索引
    op.execute("ALTER TABLE workflows ADD COLUMN source_run_id UUID NULL")
    op.execute("CREATE INDEX ix_workflows_source_run_id ON workflows (source_run_id)")
    # 2) 三 CHECK：drop PG 自动名 → create ck_ 名（ORM naming_convention 对齐），status 换词汇。
    #    名一律 op.f() 固化字面（naming_convention 不再二次展开——review_tickets 扩枚举先例同款）
    op.drop_constraint(op.f("workflows_status_check"), "workflows", type_="check")
    op.create_check_constraint(op.f("ck_workflows_status"), "workflows", _STATUS_V2)
    op.drop_constraint(op.f("workflows_head_version_check"), "workflows", type_="check")
    op.create_check_constraint(op.f("ck_workflows_head_version"), "workflows", _HEAD_VERSION_DEF)
    op.drop_constraint(op.f("workflow_versions_version_check"), "workflow_versions", type_="check")
    op.create_check_constraint(op.f("ck_workflow_versions_version"), "workflow_versions", _VERSION_DEF)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_workflows_status"), "workflows", type_="check")
    op.create_check_constraint(op.f("workflows_status_check"), "workflows", _STATUS_V1)
    op.drop_constraint(op.f("ck_workflows_head_version"), "workflows", type_="check")
    op.create_check_constraint(op.f("workflows_head_version_check"), "workflows", _HEAD_VERSION_DEF)
    op.drop_constraint(op.f("ck_workflow_versions_version"), "workflow_versions", type_="check")
    op.create_check_constraint(op.f("workflow_versions_version_check"), "workflow_versions", _VERSION_DEF)
    op.execute("DROP INDEX IF EXISTS ix_workflows_source_run_id")
    op.execute("ALTER TABLE workflows DROP COLUMN IF EXISTS source_run_id")

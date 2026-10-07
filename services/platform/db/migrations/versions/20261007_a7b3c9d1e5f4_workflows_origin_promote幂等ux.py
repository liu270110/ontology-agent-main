"""workflows origin 草稿来源列 & source_run_id 幂等唯一索引（40 篇 §6 提升批）

Revision ID: a7b3c9d1e5f4
Revises: e6c8a2d4f0b2
Create Date: 2026-10-07

X16 提升与前端批（docs/架构设计/40 §6 存为工作流模板 + 设计宪法 3；api/01 §5.11
POST /workflows/runs/{run_id}/promote）：

1. ``workflows.origin``：VARCHAR(16) NOT NULL DEFAULT 'user'（草稿来源词汇
   user|llm_candidate，CHECK 对齐 ``ck_workflows_origin`` 真链命名惯例——
   ck_<表>_<列>，e6c8a2d4f0b2 同款 op.f() 固化）。llm_candidate=计划卡 LLM 候选
   （40 篇 §6 入口②），发布侧强制走审批队列（宪法 3 硬门禁，服务层读列分流）；
   建行写入、行存续期不变（source_run_id 血统列同款纪律）。存量行经 DEFAULT
   'user' 原子回填，无数据迁移面；
2. promote 幂等键=run_id（重复调用返回既有草稿）：e6c8a2d4f0b2 的
   ``ix_workflows_source_run_id`` 普通索引升级为**部分唯一索引**
   ``ux_workflows_source_run_id``（WHERE source_run_id IS NOT NULL）——画布普通
   新建（NULL 血统）不受约束，同 run 并发提升在库侧串行为单草稿，应用层
   find_by_source_run 查重为主、唯一索引兜底（防竞态双草稿）。

additive-only；downgrade：删 origin 列，唯一索引退回普通索引（命名还原）。

ORM parity: services/workflows/data/orm.py（origin 列 + ck_workflows_origin）。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a7b3c9d1e5f4"
down_revision: str | None = "e6c8a2d4f0b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORIGIN_VOCAB = "origin IN ('user','llm_candidate')"


def upgrade() -> None:
    # 1) origin 草稿来源列（40 篇 §6 入口②/宪法 3；存量行 DEFAULT 原子回填 user）
    op.execute("ALTER TABLE workflows ADD COLUMN origin VARCHAR(16) NOT NULL DEFAULT 'user'")
    op.create_check_constraint(op.f("ck_workflows_origin"), "workflows", _ORIGIN_VOCAB)
    # 2) promote 幂等键：普通索引 → 部分唯一索引（run 上的重复提升库侧收口为单草稿）
    op.execute("DROP INDEX IF EXISTS ix_workflows_source_run_id")
    op.execute(
        "CREATE UNIQUE INDEX ux_workflows_source_run_id ON workflows (source_run_id) "
        "WHERE source_run_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_workflows_source_run_id")
    op.execute("CREATE INDEX ix_workflows_source_run_id ON workflows (source_run_id)")
    op.drop_constraint(op.f("ck_workflows_origin"), "workflows", type_="check")
    op.execute("ALTER TABLE workflows DROP COLUMN IF EXISTS origin")

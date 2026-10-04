"""runs 子 Run 列 + uk_runs_one_active 收窄（40 篇 R1）

Revision ID: e8a1c4d6b2f9
Revises: c9e3a7f1b5d2
Create Date: 2026-10-04

对话内多 Agent 协作（docs/架构设计/40 §3.1/§8 R1）子 Run 持久化 DDL，契约权威：
database/01 §3.2 runs 表 DDL（2026-10-04 R1 增补）与 architecture/06 §2.3 runs 行。
同一表承载根 Run（parent_run_id NULL）与子 Run（内核 spawn_sub 派生）：

- runs 加列 parent_run_id UUID NULL（自引用 FK runs.id）、label VARCHAR(128)、
  goal TEXT、depth SMALLINT NOT NULL（0=根）；
- uk_runs_one_active 部分唯一索引 WHERE 追加 ``AND parent_run_id IS NULL``——收窄为
  「每任务至多一个活跃**根** Run」，并行子 Run 不受约束（活跃数受 kernel_tool_parallelism
  限额），否则并行派发即撞唯一索引；
- depth 存量行回填：add_column 带 server_default='0' 完成 NOT NULL 回填后**移除**
  server_default，对齐 ORM（Python 端 default=0；先例 5af72ad1d6c7 server_default 对齐
  与 m1 建表 runs.usage 无 server_default 同口径，DDL 文档 DEFAULT 语义由 ORM 默认承担）；
- downgrade 先换回旧索引再删列（新索引 WHERE 引用 parent_run_id，列须先在）；删
  parent_run_id 后原子 Run 行失去血统成普通行——降级属开发期操作，应先收敛子 Run
  终态（本迁移不清洗数据）。

配套（本迁移不涉及）：repo 聚合加载 WHERE parent_run_id IS NULL 隔离 + 子 Run 独立
写入口（services/agent/data/repo_impl/session_repo.py）；ORM parity 同文件 orm.py Run。

Contract: docs/database/01 §3.2；docs/architecture/06 §2.3；docs/架构设计/40 §8 R1。
ORM parity: services/agent/data/orm.py Run。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "e8a1c4d6b2f9"
down_revision: str | None = "c9e3a7f1b5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# uk_runs_one_active 新旧 WHERE（收窄前后；与 ORM Index postgresql_where 同文）
_WHERE_NARROWED = "status IN ('queued','running','waiting_tool') AND parent_run_id IS NULL"
_WHERE_LEGACY = "status IN ('queued','running','waiting_tool')"


def upgrade() -> None:
    op.add_column("runs", sa.Column("parent_run_id", UUID(as_uuid=True), nullable=True))
    op.add_column("runs", sa.Column("label", sa.String(length=128), nullable=True))
    op.add_column("runs", sa.Column("goal", sa.Text(), nullable=True))
    # NOT NULL 存量回填走 server_default，回填后移除（对齐 ORM Python 默认，见 docstring）
    op.add_column("runs", sa.Column("depth", sa.SmallInteger(), nullable=False, server_default="0"))
    op.create_foreign_key(op.f("fk_runs_parent_run_id_runs"), "runs", "runs", ["parent_run_id"], ["id"])
    op.drop_index("uk_runs_one_active", table_name="runs")
    op.create_index(
        "uk_runs_one_active",
        "runs",
        ["tenant_id", "task_id"],
        unique=True,
        postgresql_where=sa.text(_WHERE_NARROWED),
    )
    op.alter_column("runs", "depth", existing_type=sa.SmallInteger(), server_default=None)


def downgrade() -> None:
    op.alter_column("runs", "depth", existing_type=sa.SmallInteger(), server_default=sa.text("0"))
    op.drop_index("uk_runs_one_active", table_name="runs")
    op.create_index(
        "uk_runs_one_active",
        "runs",
        ["tenant_id", "task_id"],
        unique=True,
        postgresql_where=sa.text(_WHERE_LEGACY),
    )
    op.drop_constraint(op.f("fk_runs_parent_run_id_runs"), "runs", type_="foreignkey")
    op.drop_column("runs", "parent_run_id")
    op.drop_column("runs", "label")
    op.drop_column("runs", "goal")
    op.drop_column("runs", "depth")

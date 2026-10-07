"""session_feedback 会话级用户反馈表（docs/Agent/19 §5 数据飞轮·采集环，W9+B5 批）

Revision ID: e9d3b5c7a1f4
Revises: c9e1f3a5d7b2
Create Date: 2026-10-07

飞轮三环之一「采集环」的用户信号第一落点（19 §5 裁决：POST /sessions/{id}/feedback 落
session_feedback 表）：

- 粒度=（session_id, run_id, user_id）唯一（uk_session_feedback_session_run_user）——
  同 run 同用户重复反馈=幂等更新（UoW upsert on conflict do update，非重复行）；
- outcome ∈ completed/partial/failed（19 §5 三元采集：「有帮助/部分解决/没解决」），
  DB 设 CHECK（ck_session_feedback_outcome）——封闭枚举应库级兜底（agents.status 先例）；
- tags TEXT[]：反馈标签（v1 契约收下不消费，转化环 B6 harvest 筛选面预留）；
- correction_text TEXT NULL：可选纠错文本（≤120 字，契约层 SessionFeedbackIn 管控）；
- session_id/run_id/user_id 均无 FK（messages.agent_id / tasks.active_run_id 同款口径）：
  run 随会话级联硬删、会话删除级联不触碰本表（并行批次共享库迁移未合入期亦不炸既有
  级联路径）、用户运维删不受阻；归属断言在端点层（get_session_owned + run∈session）。
  会话删除后反馈行留存为审计留痕（宪法 5 全程可追溯），孤儿行由转化环（B6 harvest）
  读面 JOIN 过滤。

additive-only 纯新建表，链上无其他表引用；downgrade=drop 本表。
**迁移链注意（orsi_capabilities 先例同文）**：本迁移 down_revision 取本批基线 head
（c9e1f3a5d7b2），**合入时由主会话按合入序调整链头**（agents 不处理跨批迁移链）。

Contract: docs/Agent/19-通用Agent前后端对标与评测飞轮设计 §5（主仓本地）。
ORM parity: services/agent/data/orm.py SessionFeedback。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision: str = "e9d3b5c7a1f4"
down_revision: str | None = "c9e1f3a5d7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_feedback",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("tags", ARRAY(sa.Text()), nullable=False),
        sa.Column("correction_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # 约束名=显式全名（ORM services/agent/data/orm.py 逐字一致；命名约定仅作用于未命名约束）
        sa.CheckConstraint("outcome IN ('completed','partial','failed')", name="ck_session_feedback_outcome"),
        sa.UniqueConstraint("session_id", "run_id", "user_id", name="uk_session_feedback_session_run_user"),
    )
    op.create_index(op.f("ix_session_feedback_tenant_id"), "session_feedback", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_session_feedback_session_id"), "session_feedback", ["session_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_session_feedback_session_id"), table_name="session_feedback")
    op.drop_index(op.f("ix_session_feedback_tenant_id"), table_name="session_feedback")
    op.drop_table("session_feedback")

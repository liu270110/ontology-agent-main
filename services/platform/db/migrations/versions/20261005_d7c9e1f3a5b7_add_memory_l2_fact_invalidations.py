"""add memory_l2_fact_invalidations（失效即归档影子表，K2-a / hindsight 范式）。

2026-10-05 K2 批（契约=docs/memory/多层记忆设计.md §11.1，DDL 顺序=06 契约→本迁移）。
现行失效为墓碑软删（status=invalidated + valid_to 双时间线），但无 reason/影子表/restore
——本表补全失效归档链：

- memory_l2_fact_invalidations - L2 事实失效影子行（失效即归档，失效时事实文本快照）。
  语义红线：本表是**归档追溯面**，非复活通道——主表 INVALIDATED 终态不动（P3-3 防复活），
  restore 仅回填 restored_at（影子层可见性恢复）；fact_id/user_id 不设 FK（归档行须独立于
  主表与用户行生命周期存续，invite_links 同款租户列无 FK 口径）；reason 必填（无 reason
  拒绝失效，领域层 L2Fact.invalidate 同款红线）。
  写入纪律：影子行与主表失效 save_state 同事务（repo_impl.archive_invalidated + save_state
  由调用方同一 SessionDep/后台事务提交），杜绝「主表已失效、影子缺失」断链。

降级 drop 本表（链上无表引用本表；memory_l2_facts 为被引用方不受影响）。

Contract: docs/memory/多层记忆设计.md §11.1；docs/Agent/13-开源对标优化吸收方案.md §3 K2-a。
ORM parity: services/memory/data/orm.py MemoryL2FactInvalidation。

Revision ID: d7c9e1f3a5b7
Revises: b835a095ffe4
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "d7c9e1f3a5b7"
down_revision: str | None = "b835a095ffe4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_l2_fact_invalidations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("fact_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),  # 失效时事实文本快照（主表后续 UPDATE 不影响追溯）
        sa.Column("reason", sa.Text(), nullable=False),  # 必填（领域层同款红线：无 reason 拒绝失效）
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),  # null=失效生效中
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "restored_at IS NULL OR restored_at >= invalidated_at",
            name=op.f("ck_memory_l2_fact_invalidations_restore_after_invalidate"),
        ),
    )
    op.create_index(op.f("ix_memory_l2_fact_invalidations_tenant_id"), "memory_l2_fact_invalidations", ["tenant_id"])
    op.create_index(
        op.f("ix_memory_l2_fact_invalidations_fact_id"), "memory_l2_fact_invalidations", ["fact_id"]
    )  # §11.1 契约：fact_id 索引（按事实回放失效史）
    op.create_index(
        "ix_memory_l2_fact_inval_tenant_user_active",
        "memory_l2_fact_invalidations",
        ["tenant_id", "user_id", "fact_id", "restored_at"],
    )  # list_invalidated（用户归档分页）/ restore 定位最新生效影子行


def downgrade() -> None:
    op.drop_index("ix_memory_l2_fact_inval_tenant_user_active", table_name="memory_l2_fact_invalidations")
    op.drop_index(op.f("ix_memory_l2_fact_invalidations_fact_id"), table_name="memory_l2_fact_invalidations")
    op.drop_index(op.f("ix_memory_l2_fact_invalidations_tenant_id"), table_name="memory_l2_fact_invalidations")
    op.drop_table("memory_l2_fact_invalidations")

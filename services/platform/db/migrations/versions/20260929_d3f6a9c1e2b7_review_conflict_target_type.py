"""review_tickets target_type 枚举扩展 conflict（KB-G1a 冲突分诊 T2 工单）。

OntRAG §8.1「对齐现行 review_workflow 状态机」：冲突工单复用 review_tickets 单据机制，
target_type 增设 'conflict' 枚举值（工单 payload 并排两条事实 + 各自原文出处 + 评分明细）。
CHECK 约束不支持 ALTER，按 drop + create 原位重建（约束名 ck_review_tickets_target_type
不变，与 ORM naming convention 渲染名一致）。

Downgrade 收窄回五值枚举（conflict 单先由调用方清理，约束失败即中止回滚）。

Contract: docs/database/01 §3.5（本批同步）；OntRAG §8.1
ORM parity: services/review/data/orm.py ReviewTicket.target_type。

Revision ID: d3f6a9c1e2b7
Revises: b8e1f2a3c4d5
Create Date: 2026-09-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d3f6a9c1e2b7"
down_revision: str | None = "b8e1f2a3c4d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TARGET_TYPES_V2 = (
    "target_type IN ('ontology_candidate','knowledge_instance','memory_l2_upgrade',"
    "'plugin_listing','writeback_incident','conflict')"
)
_TARGET_TYPES_V1 = (
    "target_type IN ('ontology_candidate','knowledge_instance','memory_l2_upgrade',"
    "'plugin_listing','writeback_incident')"
)
# 物理终名（m1 建表迁移 op.f("ck_review_tickets_target_type") 产物）；op.f() 标记「已是终名」，
# 防 alembic 对显式名再套一层 naming_convention 渲染出双前缀错名（agent_degraded_state 同款）。
_CK_NAME = op.f("ck_review_tickets_target_type")


def upgrade() -> None:
    op.drop_constraint(_CK_NAME, "review_tickets", type_="check")
    op.create_check_constraint(_CK_NAME, "review_tickets", _TARGET_TYPES_V2)


def downgrade() -> None:
    op.drop_constraint(_CK_NAME, "review_tickets", type_="check")
    op.create_check_constraint(_CK_NAME, "review_tickets", _TARGET_TYPES_V1)

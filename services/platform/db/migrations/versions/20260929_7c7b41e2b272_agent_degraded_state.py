"""agent degraded state (H-0c ③): status check widened + adapter_failure_count column.

2026-09-29 H-0c stability batch (review rows 17/20, docs/architecture/04 §3 agent adapter
row: "degraded（探活失败 N 次入 degraded，新会话拒绑、存量 Run 跑完）"). Two additive
changes on agents:

1. status CHECK widened to include 'degraded' (enabled<->degraded bidirectional; disabled
   terminal semantics unchanged — enforced by the aggregate transition table, not SQL);
2. adapter_failure_count INTEGER NOT NULL DEFAULT 0 — consecutive health-check failure
   counter (failure +1, success resets to zero; >= threshold -> degrade()).

Downgrade resets degraded rows to enabled before restoring the narrow CHECK (a degraded
row would violate it otherwise); the counter column is dropped.

Contract: docs/Agent/Agent服务设计.md §2 (agent aggregate) + 04 §10 degraded ruling.
ORM parity: services/agent/data/orm.py Agent (ck_agents_status, adapter_failure_count).

Revision ID: 7c7b41e2b272
Revises: c3d5e7f9a1b3
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c7b41e2b272"
down_revision: str | None = "c3d5e7f9a1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 物理名沿 naming convention 展开后的终名（convention ck_%(table_name)s_%(constraint_name)s
# × ORM CheckConstraint(name="ck_agents_status")，与 m1 建表迁移 op.f() 产物一致）；
# op.f() 标记「已是终名」，防 alembic 对显式名再套一层 convention
_CK_NAME = "ck_agents_ck_agents_status"


def upgrade() -> None:
    op.drop_constraint(op.f(_CK_NAME), "agents", type_="check")
    op.create_check_constraint(op.f(_CK_NAME), "agents", "status IN ('enabled','disabled','degraded')")
    op.add_column(
        "agents",
        sa.Column("adapter_failure_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("agents", "adapter_failure_count")
    # degraded 行回退为 enabled（窄约束不认 degraded；探活计数列已删，回 enabled 即健康态）
    op.execute("UPDATE agents SET status = 'enabled' WHERE status = 'degraded'")
    op.drop_constraint(op.f(_CK_NAME), "agents", type_="check")
    op.create_check_constraint(op.f(_CK_NAME), "agents", "status IN ('enabled','disabled')")

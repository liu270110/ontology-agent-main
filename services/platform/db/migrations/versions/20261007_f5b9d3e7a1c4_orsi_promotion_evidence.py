"""orsi_capabilities.promotion_evidence（17 篇 §3.3 晋升证据挂接点；C 批 evalsuite-c）

ORSI 原子能力注册表补一列：``promotion_evidence`` JSONB 可空——晋升（promote）审查
工单的必填证据挂接点（URI+version_diff 摘要五键闭集：eval_tag/scenario_hash/
metrics_digest/baseline_digest/version_diff_uri，键集权威=services/rsi/domain/orsi.py
``PROMOTION_EVIDENCE_KEYS``，领域构造期校验）。

红线不动：promoted 迁移位 v1 恒拒——本列仅承载证据（与 source_channel 同类元数据），
不激活任何状态迁移；M5+ 审核工作流接线后由 promote() 消费。

JSONB 可空（既有登记行不回填；eval 来源注册由 benchmarks/orsi_link.py v2 --register 挂）。

迁移链注意：当前链上多 head 并行（多批次 worktree 各取基线 head 先例）——本迁移
down_revision 取 a7b3c9d1e5f4，合入时由主会话按合入序调整链头（agents 不处理跨批迁移链）。

Contract: docs/Agent/17 §3.3；docs/Agent/14 §4/§5。
ORM parity: services/rsi/data/orm.py OrsiCapabilityORM.promotion_evidence。

Revision ID: f5b9d3e7a1c4
Revises: a7b3c9d1e5f4
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "f5b9d3e7a1c4"
down_revision: str | None = "a7b3c9d1e5f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orsi_capabilities", sa.Column("promotion_evidence", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("orsi_capabilities", "promotion_evidence")

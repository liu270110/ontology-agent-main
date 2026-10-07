"""kb_rule_candidates 放宽 document_id 可空 + 加 source 来源列——K19 审批携带策略修正回流（13 篇 §25）

Revision ID: e7c9d1f3a5b2
Revises: c7e9f2a4b6d8
Create Date: 2026-10-07

B-3 审批携带策略修正回流（codex@01 §7 批准产出规则而非仅放行；R21-2 立项裁决）：
规则候选本就有两来源（文档抽取/审批回流），document_id 语义上应可空——系统占位锚会
污染文档列表且语义失真，故采「放宽 nullable + source 语义列」而非占位锚：

- document_id 放宽 nullable=True（FK 保留：审批回流该键为 NULL，抽取路径不变）；
- 加 source VARCHAR(16) NOT NULL DEFAULT 'extraction'——存量行零回填即语义不变，
  审批回流行写 'approval'；CHECK 枚举库级收口（kind/status 同款纪律）；
- additive-only 单头（down_revision=c7e9f2a4b6d8，alembic heads 现场核 2026-10-07）；
- downgrade 逆序：删 CHECK → 删列 → document_id 收回 NOT NULL（存量若有 source='approval'
  行其 document_id 必为 NULL，收紧会失败——回滚前置条件=先清审批回流候选，候选永不物理
  删除纪律下按运维处置，此处如实暴露而非静默清数）。

Contract: docs/Agent/13 §25（K19-c）；上游 docs/研究整理/12-开源Agent深度对标/01-codex.md §7。
ORM parity: services/kb/data/rule_orm.py KbRuleCandidate.document_id/source。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "e7c9d1f3a5b2"
down_revision: str | None = "c7e9f2a4b6d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "kb_rule_candidates",
        "document_id",
        existing_type=UUID(as_uuid=True),
        nullable=True,
    )
    op.add_column(
        "kb_rule_candidates",
        sa.Column(
            "source",
            sa.String(length=16),
            server_default=sa.text("'extraction'::character varying"),
            nullable=False,
        ),
    )
    # op.f()：名字已是终名（NAMING_CONVENTION ck 模板会二次加前缀，review_conflict_target_type 同款）
    op.create_check_constraint(
        op.f("ck_kb_rule_candidates_source"),
        "kb_rule_candidates",
        "source IN ('extraction','approval')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_kb_rule_candidates_source"), "kb_rule_candidates", type_="check")
    op.drop_column("kb_rule_candidates", "source")
    op.alter_column(
        "kb_rule_candidates",
        "document_id",
        existing_type=UUID(as_uuid=True),
        nullable=False,
    )

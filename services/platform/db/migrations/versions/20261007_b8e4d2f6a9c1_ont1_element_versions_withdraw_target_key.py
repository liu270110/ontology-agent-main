"""ONT-1 本体核心增补：元素定义快照表 + 撤除标记/候选防重提列 + changeset 目标防重提。

2026-10-07 ONT-1 批（契约=docs/architecture/06 篇 §ONT-1，DDL=docs/database/01 篇 §ONT-1，
逐列对照实现；顺序纪律=06 契约 → database/01 DDL → 本迁移）：

- ontology_element_versions —— 元素定义快照（ONT-1.1，append-only）：每元素至多一个活跃快照
  （部分唯一 uk_ont_elem_versions_active WHERE superseded_at IS NULL）；作废=定向 UPDATE
  superseded_at/superseded_by，行永不 DELETE（应用层守卫随 ONT-1 实现批落测试）。
- classes/properties/axioms/rules 增列 —— 撤除=标记（ONT-1.3 状态机外化）：withdrawn_at/
  withdrawn_reason，撤除永不物理删行；axioms/rules 另增 declined_reason/evidence_count
  （source='llm_candidate' 防重提层三：同形候选再提需证据量翻倍，DEFAULT 1 与 DDL 一致）。
- ontology_changesets.target_key —— 防重提层一（ONT-1.5）：sha256(canonical_json(sorted 目标
  IRI 列表))，纯新增类为 NULL；部分唯一 uk_changesets_one_target（仅 draft/in_review 生效，
  同目标不双开）；既有 uk_changesets_one_active（本体级单活跃）保持不动。

降级：本仓 upgrade-only 惯例（merge 头 a1eddb95e6dd 同款）——downgrade 留空不实现。

Contract: docs/architecture/06-数据层设计.md §ONT-1.1/§ONT-1.3/§ONT-1.5；
          docs/database/01-数据库详细设计.md §ONT-1（DDL 权威）。
ORM parity: services/ontology/data/orm.py（OntologyElementVersion / OntologyChangeset.target_key /
          OntoClass/OntoProperty/Axiom/Rule 撤除与候选列）。

Revision ID: b8e4d2f6a9c1
Revises: a1eddb95e6dd
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "b8e4d2f6a9c1"
down_revision: str | None = "2a9cb3fa48c6"  # 合入时由主会话按合入序接到舰队链尾（原 a1eddb95e6dd，replay 后重指）
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) 新表：元素定义快照（append-only，database/01 §ONT-1 DDL 逐列对照）
    op.create_table(
        "ontology_element_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("ontology_id", UUID(as_uuid=True), sa.ForeignKey("ontologies.id"), nullable=False),
        sa.Column("element_type", sa.String(length=16), nullable=False),
        sa.Column("element_key", sa.String(length=256), nullable=False),
        sa.Column("definition", JSONB(), nullable=False),
        sa.Column("definition_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "superseded_by", UUID(as_uuid=True), sa.ForeignKey("ontology_element_versions.id"), nullable=True
        ),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "element_type IN ('class','property','axiom','rule')",
            name="ck_ont_elem_versions_element_type",
        ),
    )
    op.create_index(
        "uk_ont_elem_versions_active",
        "ontology_element_versions",
        ["ontology_id", "element_type", "element_key"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.create_index("ix_ont_elem_versions_tenant", "ontology_element_versions", ["tenant_id", "ontology_id"])

    # 2) 读模型四表：撤除=标记 + LLM 候选防重提列（ONT-1.3）
    op.add_column("classes", sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("classes", sa.Column("withdrawn_reason", sa.String(length=256), nullable=True))
    op.add_column("properties", sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("properties", sa.Column("withdrawn_reason", sa.String(length=256), nullable=True))
    op.add_column("axioms", sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("axioms", sa.Column("withdrawn_reason", sa.String(length=256), nullable=True))
    op.add_column("axioms", sa.Column("declined_reason", sa.String(length=256), nullable=True))
    op.add_column(
        "axioms", sa.Column("evidence_count", sa.Integer(), nullable=False, server_default=sa.text("1"))
    )
    op.add_column("rules", sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("rules", sa.Column("withdrawn_reason", sa.String(length=256), nullable=True))
    op.add_column("rules", sa.Column("declined_reason", sa.String(length=256), nullable=True))
    op.add_column(
        "rules", sa.Column("evidence_count", sa.Integer(), nullable=False, server_default=sa.text("1"))
    )

    # 3) changeset 目标级防重提（ONT-1.5；uk_changesets_one_active 保持不动）
    op.add_column("ontology_changesets", sa.Column("target_key", sa.CHAR(length=64), nullable=True))
    op.create_index(
        "uk_changesets_one_target",
        "ontology_changesets",
        ["tenant_id", "ontology_id", "target_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('draft','in_review') AND target_key IS NOT NULL"),
    )


def downgrade() -> None:
    # upgrade-only（本仓惯例，a1eddb95e6dd 同款）：ONT-1 为增量机制，不做降级路径。
    pass

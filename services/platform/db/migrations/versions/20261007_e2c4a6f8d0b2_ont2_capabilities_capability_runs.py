"""ONT-2 能力本体 schema：capabilities/capability_runs 两表 + 元素快照类型扩型。

2026-10-07 ONT-2 批（契约=docs/architecture/06 篇 §ONT-2.1~2.3，DDL=docs/database/01 篇
§ONT-2，逐列对照实现；顺序纪律=06 契约 → database/01 DDL → 本迁移）：

- capabilities —— 能力读模型（ONT-2.2，复用 ONT-1 版本化）：挂靠 cap TBox 版本
  （ontology_id/version_id FK），UNIQUE(version_id, iri)；requires/produces/grants/
  serves_task=JSONB（白名单结构由 pydantic 校验）；execution=执行语义四元 JSONB NOT NULL；
  current_definition_version_id FK→ontology_element_versions（ONT-1 快照机制复用）；
  撤除=标记（withdrawn_at/reason），部分索引 ix_capabilities_tenant_iri 仅覆盖在役行。
- capability_runs —— 运行台账（ONT-2.3，0034 runs + 0050 五态修正）：capability_iri 值
  引用不 FK（台账比对象活得久）+ capability_id FK ON DELETE SET NULL（对象删除台账存活）；
  status 五态封闭集（pending/despatched/succeeded/partial/failed）、channel 四通道封闭集
  （kernel/mcp/api/skill）；ix_capability_runs_lookup（tenant,iri,created_at DESC）+
  ix_capability_runs_open 部分索引（在途单）。本批只记台账不接调用点（E3 待专题）。
- ontology_element_versions.element_type CHECK 扩型 —— ONT-2.2 契约「current_definition_
  version_id … ONT-1 机制复用」的直接后果：能力定义快照需第五元素类型 'capability'
  （requires/produces/constrained_by/execution/binds_action 算定义字段，白名单单点
  =ontology_read_model.DEFINITION_FIELDS，随本批同步扩型）。原四类值域保持不动。

降级：本仓 upgrade-only 惯例（ONT-1 迁移 b8e4d2f6a9c1 同款）——downgrade 留空不实现。
可重入：两表建表/建索引均带存在性守卫（迁移② 种子链 mid-run COMMIT 落库后若失败/崩溃，
重跑再次进入本迁移须干净跳过——表级 has_table 守卫，理由见 upgrade() 注记）。

Contract: docs/architecture/06-数据层设计.md §ONT-2.1~§ONT-2.3；
          docs/database/01-数据库详细设计.md §ONT-2（DDL 权威）。
ORM parity: services/ontology/data/orm.py（Capability / CapabilityRun）；快照类型扩型=
          services/ontology/domain/model/ontology_read_model.py DEFINITION_FIELDS['capability']。

Revision ID: e2c4a6f8d0b2
Revises: b8e4d2f6a9c1
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "e2c4a6f8d0b2"
down_revision: str | None = "c9e1f3a5d7b2"  # 合入序接舰队 K28 链尾
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 0) 元素快照类型扩型（ONT-2.2 快照机制复用的前置：'capability' 入封闭集）。
    # 约束名注记：NAMING_CONVENTION（base.py）对显式名再套 ck_<table>_ 前缀——迁移与
    # create_all 两路同名（实测共享开发库=ck_ontology_element_versions_ck_ont_elem_versions_
    # element_type）；DROP 走条件块兼容两种历史候选，新约束沿用 ONT-1 同名（重建后同名）。
    op.execute(
        """
DO $$
DECLARE
    prefixed CONSTANT text := 'ck_ontology_element_versions_ck_ont_elem_versions_element_type';
    legacy   CONSTANT text := 'ck_ont_elem_versions_element_type';
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = prefixed) THEN
        EXECUTE format('ALTER TABLE ontology_element_versions DROP CONSTRAINT %I', prefixed);
    ELSIF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = legacy) THEN
        EXECUTE format('ALTER TABLE ontology_element_versions DROP CONSTRAINT %I', legacy);
    END IF;
END $$;
"""
    )
    op.create_check_constraint(
        "ck_ont_elem_versions_element_type",
        "ontology_element_versions",
        "element_type IN ('class','property','axiom','rule','capability')",
    )

    # 1) capabilities（database/01 §ONT-2 DDL 逐列对照）。可重入守卫：迁移② 在种子链前
    #    exec_driver_sql("COMMIT")——此后 ① 的 DDL 已持久而版本戳仍停在 b8e4d2f6a9c1（env.py
    #    单事务包全程、版本戳终态一次性提交），若种子失败/崩溃，重跑会再次进入本迁移；
    #    CREATE TABLE 非幂等，存在性守卫让重放干净跳过（PG 事务性 DDL：COMMIT 前失败整体
    #    回滚、无半建状态，表级守卫即充分；与上方 DO 块的条件 DROP 同款纪律）。
    if not sa.inspect(op.get_bind()).has_table("capabilities"):
        op.create_table(
            "capabilities",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
            sa.Column("ontology_id", UUID(as_uuid=True), sa.ForeignKey("ontologies.id"), nullable=False),
            sa.Column("version_id", UUID(as_uuid=True), sa.ForeignKey("ontology_versions.id"), nullable=False),
            sa.Column("iri", sa.String(length=256), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("label", sa.String(length=256), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("requires", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("produces", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("grants", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("constrained_by", sa.String(length=256), nullable=True),
            sa.Column("execution", JSONB(), nullable=False),
            sa.Column("serves_task", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("binds_action", sa.String(length=256), nullable=True),
            sa.Column(
                "current_definition_version_id",
                UUID(as_uuid=True),
                sa.ForeignKey("ontology_element_versions.id"),
                nullable=True,
            ),
            sa.Column("source", sa.String(length=16), nullable=False, server_default=sa.text("'manual'")),
            sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("withdrawn_reason", sa.String(length=256), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.CheckConstraint("kind IN ('atomic','composite')", name="ck_capabilities_kind"),
            sa.CheckConstraint("source IN ('manual','seed','llm_candidate')", name="ck_capabilities_source"),
            sa.UniqueConstraint("version_id", "iri", name="uk_capabilities_version_id_iri"),
        )
        op.create_index(
            "ix_capabilities_tenant_iri",
            "capabilities",
            ["tenant_id", "iri"],
            unique=False,
            postgresql_where=sa.text("withdrawn_at IS NULL"),
        )

    # 2) capability_runs（database/01 §ONT-2 DDL 逐列对照；0050 五态；可重入守卫同上）
    if not sa.inspect(op.get_bind()).has_table("capability_runs"):
        op.create_table(
            "capability_runs",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
            sa.Column("capability_iri", sa.String(length=256), nullable=False),
            sa.Column(
                "capability_id",
                UUID(as_uuid=True),
                sa.ForeignKey("capabilities.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("action_iri", sa.String(length=256), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("channel", sa.String(length=16), nullable=False),
            sa.Column("requested_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("session_id", UUID(as_uuid=True), nullable=True),
            sa.Column("trace_id", sa.String(length=64), nullable=True),
            sa.Column("input_digest", JSONB(), nullable=True),
            sa.Column("result_digest", JSONB(), nullable=True),
            sa.Column("target_ref_type", sa.String(length=32), nullable=True),
            sa.Column("target_ref_id", UUID(as_uuid=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint(
                "status IN ('pending','despatched','succeeded','partial','failed')",
                name="ck_capability_runs_status",
            ),
            sa.CheckConstraint("channel IN ('kernel','mcp','api','skill')", name="ck_capability_runs_channel"),
        )
        op.create_index(
            "ix_capability_runs_lookup",
            "capability_runs",
            ["tenant_id", "capability_iri", sa.text("created_at DESC")],
            unique=False,
        )
        op.create_index(
            "ix_capability_runs_open",
            "capability_runs",
            ["tenant_id"],
            unique=False,
            postgresql_where=sa.text("status IN ('pending','despatched')"),
        )


def downgrade() -> None:
    # upgrade-only（本仓惯例，b8e4d2f6a9c1 同款）：能力本体为增量机制，不做降级路径。
    pass

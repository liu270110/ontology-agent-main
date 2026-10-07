"""add kb connector/governance/rule/usage 5 tables（kb 六 ORM 族的改表流程第 3 步收口）

五组 ORM 已先行合入（先例：connector_orm.py docstring「ORM 按设计先行落地、DDL 契约与
Alembic 迁移回填待办移交 DB owner」——本迁移即该回填待办的收口，改表流程第 3 步：
06 契约 → database/01 DDL → Alembic 迁移）：

- kb_connector_cursors   连接器游标（多源接入设计 §3.2，(tenant, source, stream) 一行 upsert）
- kb_connector_events    连接器事件登记（只追加追溯锚；uk 五元组幂等去重）
- kb_fact_relations      失效分边三语义（GraphRAG 设计 §8.2：superseded_by/outranked_by/invalidated_by）
- kb_conflicts           冲突工单（§8.1 四型四处置 v1 全人工裁决）
- kb_rule_candidates     规则候选草案（时序流程型抽取；宪法底线 3：risk_flag 库级恒真）
- kb_usage_counters      知识活性计数（§6.1 反馈环，只记计数不记内容级日志）

列/约束/索引逐字转录自 ORM（services/kb/data/{connector,governance,rule,usage}_orm.py），
经 Base.metadata CreateTable 编译实证（本迁移约束名=编译产物落库名）：

- 约束命名两形态（落库名 = Base.metadata create_all 编译产物，已实证一致）：
  ①uq/ix/pk/fk 模板不含 %(constraint_name)s，迁移里显式写的落库名直用不改；
  ②ck 模板含 %(constraint_name)s，op.create_table 执行时**总是**再套一层——故迁移内
  CHECK 写 ORM __table_args__ 原名（connector_events 三短名 source_type/event_type/
  occurred_at_trust → ck_kb_connector_events_<短名>；其余三表显式全名 → 双前缀
  ck_kb_fact_relations_ck_kb_fact_relations_relation 等，a9f3c2e1d7b8 kb_facts 迁移
  同款现象，命名归一随后续批次统一处理）。首版曾在迁移里预套落库全名，被 op 层
  二次展开成三前缀并触发 PG 63 字节截断+hash，集成测试 information_schema 断言
  抓出后修正为本写法；
- FK 命名走 convention fk_<table>_<column>_<referred>：kb_fact_relations.from/to→kb_facts、
  kb_conflicts.fact_a/fact_b→kb_facts、resolved_by→users.id、
  kb_rule_candidates.document_id→documents、chunk_id→document_chunks；
- 五表 tenant_id 无 FK（TenantMixin 原样：invite_links b7d2e4f6a8c0 同款口径——租户
  删除策略未定，不设硬引用）；kb_usage_counters.kb_collection_id/chunk_id 无 FK
  （usage_orm docstring：派生观测数据绝不阻塞知识生命周期）；
- server_default 按 kb_facts a9f3c2e1d7b8 先例把 ORM Python default 落成库端默认
  （default=dict→'{}'::jsonb、default=list→'[]'::jsonb、枚举串→'::<char varying'、
  计数 0→'0'、risk_flag→true、时间戳→now()）；id 的 Python 端 uuid7 默认不落库端
  （base.py：默认值仅作用于 ORM 侧插入，先例两迁移同款）。

- 范围：只增不改——五表全部新建，不触碰既有表/数据（无种子）；
- downgrade 按创建逆序 drop 六表（kb_usage_counters → kb_rule_candidates →
  kb_conflicts → kb_fact_relations → kb_connector_events → kb_connector_cursors），
  先 drop 索引再 drop 表（先例 a9f3c2e1d7b8 口径）；五表间无互相 FK，链上无其他表
  引用五表，逆序 drop 无悬挂引用。

Contract: docs/OntRAG/多源接入与连接器设计.md §3/§6.1；docs/OntRAG/知识库GraphRAG设计.md §8.1/§8.2。
ORM parity: services/kb/data/connector_orm.py、governance_orm.py、rule_orm.py、usage_orm.py。

Revision ID: b3d5f7a9c1e3
Revises: c9e3a7f1b5d2
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "b3d5f7a9c1e3"
down_revision: str | None = "c9e3a7f1b5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ① kb_connector_cursors（connector_orm.KbConnectorCursor）
    op.create_table(
        "kb_connector_cursors",
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("stream_id", sa.String(length=256), server_default=sa.text("'*'::character varying"), nullable=False),
        sa.Column("cursor", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("connector_version", sa.String(length=32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_kb_connector_cursors"),
        sa.UniqueConstraint(
            "tenant_id",
            "source_id",
            "stream_id",
            name="uk_kb_connector_cursors_tenant_id_source_id_stream_id",
        ),
    )
    op.create_index(op.f("ix_kb_connector_cursors_tenant_id"), "kb_connector_cursors", ["tenant_id"], unique=False)

    # ② kb_connector_events（connector_orm.KbConnectorEvent；只追加，uk 五元组幂等去重）
    op.create_table(
        "kb_connector_events",
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("source_system", sa.String(length=64), nullable=False),
        sa.Column("stream_id", sa.String(length=256), nullable=False),
        sa.Column("external_id", sa.String(length=256), nullable=False),
        sa.Column("source_sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("payload_ref", sa.Text(), nullable=True),
        sa.Column("payload_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("schema_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("connector_version", sa.String(length=32), nullable=False),
        sa.Column("native_metadata", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("acl_tags", JSONB(), nullable=True),
        sa.Column("cursor", JSONB(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurred_at_trust", sa.String(length=16), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('document','transactional','data_platform','message','external','machine','manual')",
            name="source_type",
        ),
        sa.CheckConstraint(
            "event_type IN ('added','modified','deleted','schema_changed')",
            name="event_type",
        ),
        sa.CheckConstraint(
            "occurred_at_trust IN ('source','approximate','fallback')",
            name="occurred_at_trust",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kb_connector_events"),
        sa.UniqueConstraint(
            "tenant_id",
            "source_id",
            "stream_id",
            "external_id",
            "source_sequence",
            name="uk_kb_connector_events_dedup",
        ),
    )
    op.create_index(op.f("ix_kb_connector_events_tenant_id"), "kb_connector_events", ["tenant_id"], unique=False)
    op.create_index(
        "ix_kb_connector_events_trace",
        "kb_connector_events",
        ["tenant_id", "source_id", "stream_id", "external_id"],
        unique=False,
    )
    op.create_index(
        "ix_kb_connector_events_collected", "kb_connector_events", ["tenant_id", "collected_at"], unique=False
    )

    # ③ kb_fact_relations（governance_orm.KbFactRelation；§8.2 三语义不复用，uk 幂等）
    op.create_table(
        "kb_fact_relations",
        sa.Column("from_fact_id", UUID(as_uuid=True), nullable=False),
        sa.Column("to_fact_id", UUID(as_uuid=True), nullable=False),
        sa.Column("relation", sa.String(length=32), nullable=False),
        sa.Column("evidence", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["from_fact_id"], ["kb_facts.id"], name="fk_kb_fact_relations_from_fact_id_kb_facts"
        ),
        sa.ForeignKeyConstraint(["to_fact_id"], ["kb_facts.id"], name="fk_kb_fact_relations_to_fact_id_kb_facts"),
        sa.PrimaryKeyConstraint("id", name="pk_kb_fact_relations"),
        sa.CheckConstraint(
            "relation IN ('superseded_by','outranked_by','invalidated_by')",
            name="ck_kb_fact_relations_relation",
        ),
        sa.UniqueConstraint("from_fact_id", "to_fact_id", "relation", name="uk_kb_fact_relations_from_to_relation"),
    )
    op.create_index(op.f("ix_kb_fact_relations_tenant_id"), "kb_fact_relations", ["tenant_id"], unique=False)
    op.create_index("ix_kb_fact_relations_to", "kb_fact_relations", ["to_fact_id", "relation"], unique=False)

    # ④ kb_conflicts（governance_orm.KbConflict；§8.1 v1 全人工裁决，uk 工单幂等）
    op.create_table(
        "kb_conflicts",
        sa.Column("conflict_type", sa.String(length=2), nullable=False),
        sa.Column("fact_a_id", UUID(as_uuid=True), nullable=False),
        sa.Column("fact_b_id", UUID(as_uuid=True), nullable=False),
        sa.Column("score_a", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("score_b", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column(
            "resolution", sa.String(length=16), server_default=sa.text("'pending'::character varying"), nullable=False
        ),
        sa.Column("resolved_by", UUID(as_uuid=True), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["fact_a_id"], ["kb_facts.id"], name="fk_kb_conflicts_fact_a_id_kb_facts"),
        sa.ForeignKeyConstraint(["fact_b_id"], ["kb_facts.id"], name="fk_kb_conflicts_fact_b_id_kb_facts"),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"], name="fk_kb_conflicts_resolved_by_users"),
        sa.PrimaryKeyConstraint("id", name="pk_kb_conflicts"),
        sa.CheckConstraint(
            "conflict_type IN ('T2','T3')",
            name="ck_kb_conflicts_conflict_type",
        ),
        sa.CheckConstraint(
            "resolution IN ('winner_a','winner_b','t3_coexist','pending')",
            name="ck_kb_conflicts_resolution",
        ),
        sa.UniqueConstraint("fact_a_id", "fact_b_id", name="uk_kb_conflicts_fact_a_id_fact_b_id"),
    )
    op.create_index(op.f("ix_kb_conflicts_tenant_id"), "kb_conflicts", ["tenant_id"], unique=False)
    op.create_index("ix_kb_conflicts_queue", "kb_conflicts", ["tenant_id", "resolution"], unique=False)

    # ⑤ kb_rule_candidates（rule_orm.KbRuleCandidate；底线 3：risk_flag 库级恒真 CHECK）
    op.create_table(
        "kb_rule_candidates",
        sa.Column("document_id", UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", UUID(as_uuid=True), nullable=True),
        sa.Column("rule_id", sa.String(length=64), nullable=False),
        sa.Column("rule_key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("consequence", sa.Text(), nullable=False),
        sa.Column("target_class", sa.String(length=256), nullable=False),
        sa.Column("evidence", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("draft_shacl", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("risk_flag", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("violations", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'candidate'::character varying"), nullable=False
        ),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column("meta", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], name="fk_kb_rule_candidates_document_id_documents"),
        sa.ForeignKeyConstraint(
            ["chunk_id"], ["document_chunks.id"], name="fk_kb_rule_candidates_chunk_id_document_chunks"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kb_rule_candidates"),
        sa.CheckConstraint(
            "kind IN ('invariant','precondition','exclusion','state_transition')",
            name="ck_kb_rule_candidates_kind",
        ),
        sa.CheckConstraint(
            "status IN ('candidate','approved','rejected')",
            name="ck_kb_rule_candidates_status",
        ),
        sa.CheckConstraint("risk_flag", name="ck_kb_rule_candidates_risk_flag_true"),
        sa.UniqueConstraint("tenant_id", "rule_key", name="uk_kb_rule_candidates_tenant_id_rule_key"),
    )
    op.create_index(op.f("ix_kb_rule_candidates_tenant_id"), "kb_rule_candidates", ["tenant_id"], unique=False)
    op.create_index(
        "idx_kb_rule_candidates_doc", "kb_rule_candidates", ["tenant_id", "document_id", "status"], unique=False
    )

    # ⑥ kb_usage_counters（usage_orm.KbUsageCounter；维度列无 FK，upsert 原子递增）
    op.create_table(
        "kb_usage_counters",
        sa.Column("kb_collection_id", UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", UUID(as_uuid=True), nullable=False),
        sa.Column("search_hits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("action_refs", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_searched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_kb_usage_counters"),
        sa.UniqueConstraint("tenant_id", "kb_collection_id", "chunk_id", name="uk_kb_usage_counters_dims"),
    )
    op.create_index(op.f("ix_kb_usage_counters_tenant_id"), "kb_usage_counters", ["tenant_id"], unique=False)
    op.create_index(
        "ix_kb_usage_counters_hits",
        "kb_usage_counters",
        ["tenant_id", "kb_collection_id", "search_hits"],
        unique=False,
    )


def downgrade() -> None:
    # 创建的严格逆序；先索引后表（先例 a9f3c2e1d7b8 口径）。五表间无互相 FK、
    # 链上无其他表引用五表，drop 无悬挂引用。
    # ⑥ kb_usage_counters
    op.drop_index(op.f("ix_kb_usage_counters_hits"), table_name="kb_usage_counters")
    op.drop_index(op.f("ix_kb_usage_counters_tenant_id"), table_name="kb_usage_counters")
    op.drop_table("kb_usage_counters")
    # ⑤ kb_rule_candidates
    op.drop_index("idx_kb_rule_candidates_doc", table_name="kb_rule_candidates")
    op.drop_index(op.f("ix_kb_rule_candidates_tenant_id"), table_name="kb_rule_candidates")
    op.drop_table("kb_rule_candidates")
    # ④ kb_conflicts
    op.drop_index("ix_kb_conflicts_queue", table_name="kb_conflicts")
    op.drop_index(op.f("ix_kb_conflicts_tenant_id"), table_name="kb_conflicts")
    op.drop_table("kb_conflicts")
    # ③ kb_fact_relations
    op.drop_index("ix_kb_fact_relations_to", table_name="kb_fact_relations")
    op.drop_index(op.f("ix_kb_fact_relations_tenant_id"), table_name="kb_fact_relations")
    op.drop_table("kb_fact_relations")
    # ② kb_connector_events
    op.drop_index("ix_kb_connector_events_collected", table_name="kb_connector_events")
    op.drop_index("ix_kb_connector_events_trace", table_name="kb_connector_events")
    op.drop_index(op.f("ix_kb_connector_events_tenant_id"), table_name="kb_connector_events")
    op.drop_table("kb_connector_events")
    # ① kb_connector_cursors
    op.drop_index(op.f("ix_kb_connector_cursors_tenant_id"), table_name="kb_connector_cursors")
    op.drop_table("kb_connector_cursors")

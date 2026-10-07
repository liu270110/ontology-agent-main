"""ontology 模块 ORM：ontologies/ontology_versions + changesets + 读模型四表 + 元素定义快照 + 能力读模型（10 表）。

DDL 权威：database/01 §3.3~§3.4 + §ONT-1 + §ONT-2；2026-09-27 模块轴重构自 kb_ontology_audit/m2_semantic_review 拆分。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BOOLEAN,
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin, TimestampMixin


class Ontology(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "ontologies"
    iri_base: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    scheme_tier: Mapped[str] = mapped_column(String(16), default="light_graph", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    format: Mapped[str] = mapped_column(String(16), default="turtle", nullable=False)
    owner_business: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))  # 双负责人（ontology §6.3）
    owner_engineer: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # FK 迁移中 DEFERRABLE 后置
    __table_args__ = (
        UniqueConstraint("tenant_id", "iri_base", name="uk_ontologies_tenant_id_iri_base"),
        CheckConstraint("scheme_tier IN ('glossary','light_graph','heavy')", name="ck_ontologies_scheme_tier"),
        CheckConstraint("status IN ('draft','published','deprecated')", name="ck_ontologies_status"),
    )


class OntologyVersion(Base, PkMixin):  # 版本不可变；制品在 MinIO
    __tablename__ = "ontology_versions"
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    ontology_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ontologies.id"), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_key: Mapped[str] = mapped_column(String(512), nullable=False)
    checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    triple_count: Mapped[int | None] = mapped_column(Integer)
    changelog: Mapped[str | None] = mapped_column(Text)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ontology_versions.id"))
    published_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("ontology_id", "version", name="uk_ontology_versions_ontology_id_version"),
        UniqueConstraint("ontology_id", "version_no", name="uk_ontology_versions_ontology_id_version_no"),
    )


class OntologyChangeset(Base, PkMixin, TenantMixin, TimestampMixin):
    """状态机=ontology §6.1 五态；工单是流程壳（03 §5）。"""

    __tablename__ = "ontology_changesets"
    ontology_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ontologies.id"), nullable=False)
    from_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ontology_versions.id"))
    to_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ontology_versions.id"))
    title: Mapped[str | None] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)
    diff: Mapped[dict | None] = mapped_column(JSONB)  # 三元组摘要
    impact_report: Mapped[dict | None] = mapped_column(JSONB)  # 核心变动影响（ontology §6.3）
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    # ONT-1.5 防重提层一：sha256(canonical_json(sorted 目标 IRI))；纯新增类为 NULL
    target_key: Mapped[str | None] = mapped_column(CHAR(64))
    applicant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reviewer_business: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))  # 双签
    reviewer_engineer: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    review_comment: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','in_review','published','rejected','rolled_back')", name="ck_ontology_changesets_status"
        ),
        Index("ix_changesets_ontology_status", "ontology_id", "status"),
        Index(
            "uk_changesets_one_active",
            "tenant_id",
            "ontology_id",
            unique=True,
            postgresql_where=text("status IN ('draft','in_review')"),
        ),  # 同本体单活跃
        Index(
            "uk_changesets_one_target",
            "tenant_id",
            "ontology_id",
            "target_key",
            unique=True,
            postgresql_where=text("status IN ('draft','in_review') AND target_key IS NOT NULL"),
        ),  # ONT-1.5 防重提层一：同目标活跃变更单不双开（终态行不限，uk_changesets_one_active 保持不动）
    )


class _ReadModelMixin(TenantMixin):
    """读模型公共列：发布版本挂靠 + 来源 changeset。"""

    ontology_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ontologies.id"), nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ontology_versions.id"), nullable=False)
    changeset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ontology_changesets.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class _WithdrawMixin:
    """撤除=标记（ONT-1.3 状态机外化）：撤除永不物理删行，只打双标记；NULL=在役。"""

    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_reason: Mapped[str | None] = mapped_column(String(256))


class _CandidateMixin:
    """LLM 候选防重提层三（ONT-1.3）：declined 裁决留痕 + 证据量（再提同形候选需翻倍）。"""

    declined_reason: Mapped[str | None] = mapped_column(String(256))
    evidence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)  # DDL DEFAULT 1


class OntoClass(_ReadModelMixin, _WithdrawMixin, Base, PkMixin):
    __tablename__ = "classes"
    iri: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str | None] = mapped_column(String(256))
    definition: Mapped[str | None] = mapped_column(Text)
    subclass_of: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    equivalent_class: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    is_behavior: Mapped[bool] = mapped_column(BOOLEAN, default=False, nullable=False)  # OB2 行动类
    state_attribute: Mapped[dict | None] = mapped_column(JSONB)  # 状态流转属性
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)  # 国标附录 A 8 项
    __table_args__ = (
        UniqueConstraint("version_id", "iri", name="uk_classes_version_id_iri"),
        Index("ix_classes_tenant_ontology", "tenant_id", "ontology_id"),
    )


class OntoProperty(_ReadModelMixin, _WithdrawMixin, Base, PkMixin):
    __tablename__ = "properties"
    iri: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # datatype|object
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str | None] = mapped_column(String(256))
    definition: Mapped[str | None] = mapped_column(Text)
    domain_iri: Mapped[str | None] = mapped_column(String(256))
    range_iri: Mapped[str | None] = mapped_column(String(256))
    type_of_terms: Mapped[str | None] = mapped_column(String(64))  # ob2:termType
    functional: Mapped[bool] = mapped_column(BOOLEAN, default=False, nullable=False)
    constraints: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # min/max/in/pattern…
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)  # 附录 A 10 项
    __table_args__ = (
        CheckConstraint("kind IN ('datatype','object')", name="ck_properties_kind"),
        UniqueConstraint("version_id", "iri", name="uk_properties_version_id_iri"),
    )


class Axiom(_ReadModelMixin, _WithdrawMixin, _CandidateMixin, Base, PkMixin):
    __tablename__ = "axioms"
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # subClassOf/disjointWith/…
    subject_iri: Mapped[str] = mapped_column(String(256), nullable=False)
    object_iri: Mapped[str | None] = mapped_column(String(256))
    expression: Mapped[str] = mapped_column(Text, nullable=False)  # OWL 2 函数式语法
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    review_state: Mapped[str | None] = mapped_column(String(16))
    __table_args__ = (
        CheckConstraint("source IN ('manual','llm_candidate')", name="ck_axioms_source"),
        Index("ix_axioms_version_kind", "version_id", "kind"),
    )


class Rule(_ReadModelMixin, _WithdrawMixin, _CandidateMixin, Base, PkMixin):
    """三路由（owl_axiom|shacl|engine），同一规则只允许一个路由（ontology §2.3）。"""

    __tablename__ = "rules"
    route: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)  # R007 式编号
    description: Mapped[str | None] = mapped_column(Text)
    event_class_iri: Mapped[str | None] = mapped_column(String(256))  # ECA 事件类（engine 路由）
    condition: Mapped[str | None] = mapped_column(Text)  # SPARQL ASK / shape 引用
    action_ref: Mapped[str | None] = mapped_column(String(256))  # 行动类 IRI
    severity: Mapped[str] = mapped_column(String(8), default="error", nullable=False)
    enabled: Mapped[bool] = mapped_column(BOOLEAN, default=True, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    review_state: Mapped[str | None] = mapped_column(String(16))
    __table_args__ = (
        CheckConstraint("route IN ('owl_axiom','shacl','engine')", name="ck_rules_route"),
        CheckConstraint("severity IN ('error','warn')", name="ck_rules_severity"),
        UniqueConstraint("version_id", "name", name="uk_rules_version_id_name"),
    )


class OntologyElementVersion(Base, PkMixin):
    """元素定义快照（ONT-1.1，append-only：只 INSERT + 定向作废 UPDATE，永不 DELETE）。

    - 每元素至多一个活跃快照：部分唯一 uk_ont_elem_versions_active（WHERE superseded_at IS NULL）；
    - 作废=UPDATE superseded_at/superseded_by 指向新快照（append-only 指行不删，允许这一次定向 UPDATE）；
    - element_key 口径（DDL）：class/property/axiom=iri；rule=name；
    - 判等：definition_hash = sha256(canonical_json(definition))——同 hash 跳过不产生新快照。
    """

    __tablename__ = "ontology_element_versions"
    # 索引面=组合 ix（DDL 不设单列索引），故不走 TenantMixin（其 tenant_id 自带单列索引）
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    ontology_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ontologies.id"), nullable=False)
    element_type: Mapped[str] = mapped_column(String(16), nullable=False)
    element_key: Mapped[str] = mapped_column(String(256), nullable=False)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)  # 定义字段白名单投影（ONT-1.2）
    definition_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # NULL=活跃
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ontology_element_versions.id"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint(
            # ONT-2 扩型：'capability'（迁移①同步；快照机制复用见类 docstring 与 §ONT-2.2）
            "element_type IN ('class','property','axiom','rule','capability')",
            name="ck_ont_elem_versions_element_type",
        ),
        Index(
            "uk_ont_elem_versions_active",
            "ontology_id",
            "element_type",
            "element_key",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
        Index("ix_ont_elem_versions_tenant", "tenant_id", "ontology_id"),
    )


class Capability(_WithdrawMixin, Base, PkMixin):
    """能力读模型行（ONT-2.2，database/01 §ONT-2 DDL 逐列对照）。

    - 挂靠：ontology_id/version_id=cap TBox 版本（种子迁移内建「平台能力本体」项目，nil 租户）；
      不用 _ReadModelMixin（其 changeset_id 列为四读模型表专有，不在 §ONT-2 契约列面内）；
    - iri UNIQUE(version_id, iri)；kind/source CHECK 与 DDL 同口径；
    - requires/produces/grants/serves_task：JSONB 白名单结构由 pydantic 校验
      （business.capability_seed.CapabilitySeedRow）；execution=执行语义四元 JSONB NOT NULL；
    - current_definition_version_id：ONT-1 快照机制复用（element_type='capability'，
      requires/produces/constrained_by/execution/binds_action 算定义字段）；
    - 撤除=标记（ONT-1.3 同款），部分索引 ix_capabilities_tenant_iri 仅覆盖在役行。
    """

    __tablename__ = "capabilities"
    # tenant_id 不设单列索引（DDL §ONT-2 只有组合 ix_capabilities_tenant_iri 且前缀即
    # tenant_id；index=True 会令 create_all 产出迁移没有的 ix_capabilities_tenant_id——
    # 两路建库 schema 分叉，ontology_element_versions 同款纪律）
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    ontology_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ontologies.id"), nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ontology_versions.id"), nullable=False)
    iri: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # atomic|composite
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str | None] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)
    requires: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    produces: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    grants: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    constrained_by: Mapped[str | None] = mapped_column(String(256))
    execution: Mapped[dict] = mapped_column(JSONB, nullable=False)  # 执行语义四元（形状见模块 docstring 顶部契约）
    serves_task: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    binds_action: Mapped[str | None] = mapped_column(String(256))
    current_definition_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ontology_element_versions.id")
    )
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint("kind IN ('atomic','composite')", name="ck_capabilities_kind"),
        CheckConstraint("source IN ('manual','seed','llm_candidate')", name="ck_capabilities_source"),
        UniqueConstraint("version_id", "iri", name="uk_capabilities_version_id_iri"),
        Index(
            "ix_capabilities_tenant_iri",
            "tenant_id",
            "iri",
            unique=False,
            postgresql_where=text("withdrawn_at IS NULL"),
        ),
    )


class CapabilityRun(Base, PkMixin):
    """能力运行台账（ONT-2.3，database/01 §ONT-2 DDL 逐列对照；0034 runs + 0050 五态修正）。

    - capability_iri 值引用不 FK（台账比对象活得久）；capability_id FK ON DELETE SET NULL
      （对象删除台账存活）；本批只记台账不接调用点（派发去抖 E3 待专题，06 篇 §ONT-2.3）；
    - 状态推进由 repo（CapabilityRunRepository）守卫：pending→despatched→{succeeded,partial,failed}。
    """

    __tablename__ = "capability_runs"
    # tenant_id 不设单列索引（DDL §ONT-2 组合 ix_capability_runs_lookup 前缀即 tenant_id，
    # 部分索引 ix_capability_runs_open 亦含 tenant_id 列；理由同 capabilities.tenant_id）
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    capability_iri: Mapped[str] = mapped_column(String(256), nullable=False)
    capability_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("capabilities.id", ondelete="SET NULL"))
    action_iri: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    input_digest: Mapped[dict | None] = mapped_column(JSONB)
    result_digest: Mapped[dict | None] = mapped_column(JSONB)
    target_ref_type: Mapped[str | None] = mapped_column(String(32))
    target_ref_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','despatched','succeeded','partial','failed')", name="ck_capability_runs_status"
        ),
        CheckConstraint("channel IN ('kernel','mcp','api','skill')", name="ck_capability_runs_channel"),
        Index(
            "ix_capability_runs_lookup",
            "tenant_id",
            "capability_iri",
            text("created_at DESC"),
        ),
        Index(
            "ix_capability_runs_open",
            "tenant_id",
            unique=False,
            postgresql_where=text("status IN ('pending','despatched')"),
        ),
    )

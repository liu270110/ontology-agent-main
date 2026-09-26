"""M2 批次 ORM：本体域余量（changesets+读模型四表）+ 知识库余量（chunks/pipeline）+ 审核 + 评估。

DDL 权威：database/01 §3.3~§3.5/§3.9；读模型=发布版本的结构化索引（编辑期权威在 changeset 内存图）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BOOLEAN,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, PkMixin, TenantMixin, TimestampMixin


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
    )


class _ReadModelMixin(TenantMixin):
    """读模型公共列：发布版本挂靠 + 来源 changeset。"""

    ontology_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ontologies.id"), nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ontology_versions.id"), nullable=False)
    changeset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ontology_changesets.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class OntoClass(_ReadModelMixin, Base, PkMixin):
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


class OntoProperty(_ReadModelMixin, Base, PkMixin):
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


class Axiom(_ReadModelMixin, Base, PkMixin):
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


class Rule(_ReadModelMixin, Base, PkMixin):
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


class DocumentChunk(Base, PkMixin, TenantMixin):  # 只追加；向量在 Milvus kb_chunks
    __tablename__ = "document_chunks"
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer)
    page_no: Mapped[int | None] = mapped_column(Integer)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 双时间线
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 失效=封口不删除
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("document_id", "seq", name="uk_document_chunks_document_id_seq"),)


class KbPipelineStep(Base, PkMixin, TenantMixin):
    """断点续跑 checkpoint（03 §4）；step 七枚举（管线审计修复）。"""

    __tablename__ = "kb_pipeline_step"
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), nullable=False)
    step: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    checkpoint_uri: Mapped[str | None] = mapped_column(String(512))  # MinIO key
    attempt: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)  # ≤3
    worker_lease: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # worker 租约心跳
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 过期置 failed 可重跑
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    __table_args__ = (
        CheckConstraint(
            "step IN ('preprocess','env_setup','extract','align','validate','archive','review')", name="step"
        ),
        CheckConstraint("status IN ('pending','running','done','failed')", name="ck_kb_pipeline_step_status"),
    )


class ReviewTicket(Base, PkMixin, TenantMixin, TimestampMixin):
    """候选非成品门禁，多场景统一入口（08 §4）；六态=03 §5 权威。"""

    __tablename__ = "review_tickets"
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # 多态引用，不设 FK
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    submitter_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    decision_note: Mapped[str | None] = mapped_column(Text)
    sla_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('ontology_candidate','knowledge_instance','memory_l2_upgrade',"
            "'plugin_listing','writeback_incident')",
            name="target_type",
        ),
        CheckConstraint(
            "status IN ('draft','pending_review','approved','rejected','published','cancelled')", name="status"
        ),
        Index("idx_review_queue", "tenant_id", "status", "created_at"),
        Index(
            "uk_review_one_open",
            "tenant_id",
            "target_type",
            "target_id",
            unique=True,
            postgresql_where=text("status IN ('draft','pending_review')"),
        ),  # 同对象唯一 open
    )


class EvaluationRun(Base, PkMixin, TenantMixin):
    __tablename__ = "evaluation_runs"  # 评估结果契约 v1 冻结（08 §7.3）
    benchmark_type: Mapped[str] = mapped_column(String(32), nullable=False)
    benchmark_version: Mapped[str] = mapped_column(String(32), nullable=False)  # 评估集版本
    trigger_ref: Mapped[dict | None] = mapped_column(JSONB)  # PR/commit/参数快照
    baseline_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evaluation_runs.id"))
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # 主指标+分层
    delta: Mapped[dict | None] = mapped_column(JSONB)  # 对基线（-2% 阻断，08 §7.2）
    passed: Mapped[bool] = mapped_column(BOOLEAN, default=True, nullable=False)
    started_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint(
            "benchmark_type IN ('retrieval_qa','agent_task','extraction_precision')", name="benchmark_type"
        ),
        Index("idx_eval_runs", "tenant_id", "benchmark_type", "created_at"),
    )


class EvaluationResult(Base, PkMixin, TenantMixin):  # 只追加
    __tablename__ = "evaluation_results"
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evaluation_runs.id"), nullable=False)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint("verdict IN ('pass','fail','skip')", name="ck_evaluation_results_verdict"),
        UniqueConstraint("run_id", "case_id", name="uk_evaluation_results_run_id_case_id"),
    )

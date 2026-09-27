"""ontology 模块 ORM：ontologies/ontology_versions + changesets + 读模型四表（7 表）。

DDL 权威：database/01 §3.3~§3.4；2026-09-27 模块轴重构自 kb_ontology_audit/m2_semantic_review 拆分。
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

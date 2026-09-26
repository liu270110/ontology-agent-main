"""域③④⑨ M1 部分：kb_collections/documents + ontologies/ontology_versions + audit_logs（5 表）。

DDL 权威：database/01 §3.3/§3.4/§3.9；M2 增补（chunks/pipeline/changesets/读模型四表）另文件。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
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
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, PkMixin, TenantMixin, TimestampMixin


class KbCollection(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "kb_collections"
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    ontology_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ontologies.id"))  # 本体引导（后建 FK 见迁移）
    embedding_model: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_defaults: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uk_kb_collections_tenant_id_name"),
        CheckConstraint("status IN ('active','archived')", name="ck_kb_collections_status"),
    )


class Document(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "documents"
    kb_collection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_collections.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), default="upload", nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    minio_key: Mapped[str] = mapped_column(String(512), nullable=False)  # raw-docs/{tenant}/{kb}/{doc}/
    checksum_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 双时间线（OntRAG §8.2）
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 失效=封口不删除
    status: Mapped[str] = mapped_column(String(16), default="uploaded", nullable=False)  # 八态=04/03 §4
    __table_args__ = (
        CheckConstraint("source_type IN ('upload','api')", name="ck_documents_source_type"),
        CheckConstraint(
            "status IN ('uploaded','preprocessed','extracting','aligning','validating',"
            "'pending_review','indexed','failed')",
            name="status",
        ),
        UniqueConstraint(
            "tenant_id",
            "kb_collection_id",
            "checksum_sha256",
            name="uk_documents_tenant_id_kb_collection_id_checksum_sha256",
        ),
        Index("ix_documents_status", "status"),
        Index(
            "ix_documents_current", "tenant_id", "kb_collection_id", postgresql_where=text("valid_to IS NULL")
        ),  # bi-temporal 当前有效视图
    )


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


class AuditLog(Base, PkMixin, TenantMixin):  # 只追加；无 update/delete（08 §3）
    __tablename__ = "audit_logs"
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # 如 ontology.publish / action.invoke
    resource_type: Mapped[str | None] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    params_digest: Mapped[dict | None] = mapped_column(JSONB)  # 脱敏摘要（08 §3）
    result: Mapped[str] = mapped_column(String(16), default="success", nullable=False)
    ip: Mapped[str | None] = mapped_column(INET)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint("actor_type IN ('user','api_key','agent','system')", name="ck_audit_logs_actor_type"),
        Index("ix_audit_tenant_time", "tenant_id", "created_at"),
    )

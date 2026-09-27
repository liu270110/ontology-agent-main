"""kb 模块 ORM：collections/documents/chunks/pipeline_step/evaluation（6 表）。

DDL 权威：database/01 §3.3/§3.9；2026-09-27 模块轴重构拆分。向量列见 DocumentChunk docstring。
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
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin, TimestampMixin


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
        # checksum 唯一性 = 部分唯一索引（WHERE valid_to IS NULL，迁移 c4f6a8b0d2e4 同名改建；
        # database/01 documents 段契约 2026-09-28 B6 批）：墓碑行（valid_to 封口）不占唯一性——
        # 软删后同内容可重传为全新文档（墓碑行保留审计）
        Index(
            "uk_documents_tenant_id_kb_collection_id_checksum_sha256",
            "tenant_id",
            "kb_collection_id",
            "checksum_sha256",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
        ),
        Index("ix_documents_status", "status"),
        Index(
            "ix_documents_current", "tenant_id", "kb_collection_id", postgresql_where=text("valid_to IS NULL")
        ),  # bi-temporal 当前有效视图
    )


class DocumentChunk(Base, PkMixin, TenantMixin):  # 只追加；向量在 pgvector embedding 列（raw-SQL 读写）
    """向量列说明：`document_chunks.embedding vector(1024)` 由迁移 add_kb_vector_embedding
    按扩展可用性条件创建（缺失即跳过、检索降级 BM25-only），故本 ORM 不映射该列——
    读写统一走 services/semantic/knowledge/embed.py 的 raw SQL，保证无 pgvector 环境全链路可用。
    """

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
    __table_args__ = (
        UniqueConstraint("document_id", "seq", name="uk_document_chunks_document_id_seq"),
        # BM25 GIN 表达式索引（迁移 add_kb_vector_embedding 同款表达式；入 ORM 供 autogenerate 对齐）
        Index(
            "ix_document_chunks_fts",
            text("to_tsvector('simple', content)"),
            postgresql_using="gin",
        ),
    )


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
            # 七步 + M2-lite 工程步（迁移 add_kb_vector_embedding 枚举扩展；database/01 回填待办）
            "step IN ('preprocess','chunk','embed','bm25_index',"
            "'env_setup','extract','align','validate','archive','review')",
            name="step",
        ),
        CheckConstraint("status IN ('pending','running','done','failed')", name="ck_kb_pipeline_step_status"),
        # 2026-09-26 缺口核查修复：幂等不变式第五件——同文档单步单行（断点续跑不重插）
        UniqueConstraint("document_id", "step", name="uk_kb_pipeline_step_doc_step"),
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


class KbFact(Base, PkMixin):  # 七步终点权威表（OntRAG §2）；status=candidate → 人工终审 → authoritative
    """抽取事实：subject/predicate/object 三元组 + 证据信封；命名沿用真库现状（含双前缀）。"""

    __tablename__ = "kb_facts"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), nullable=False)
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("document_chunks.id"))
    fact_type: Mapped[str] = mapped_column(String(16), nullable=False)  # entity|relation|attribute|event
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    predicate: Mapped[str | None] = mapped_column(Text)
    object: Mapped[str | None] = mapped_column(Text)
    subject_type: Mapped[str | None] = mapped_column(String(128))
    object_type: Mapped[str | None] = mapped_column(String(128))
    canonical_name: Mapped[str | None] = mapped_column(String(256))
    aliases: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)  # 门禁参数非真值（standards §5.3）
    status: Mapped[str] = mapped_column(String(16), default="candidate", nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # source_ref 四元组
    violations: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)  # SHACL 结论回写
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint(
            "fact_type IN ('entity','relation','attribute','event')",
            name="ck_kb_facts_ck_kb_facts_fact_type",
        ),
        CheckConstraint(
            "status IN ('candidate','rejected','authoritative')",
            name="ck_kb_facts_ck_kb_facts_status",
        ),
        Index("idx_kb_facts_doc", "tenant_id", "document_id", "status"),
        Index("idx_kb_facts_queue", "tenant_id", "status", "confidence"),
    )

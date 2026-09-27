"""L2 网关 DTO：知识库（kb）域（02 篇 §6 铁律：extra="forbid"、snake_case、只数据无行为）。

检索响应 = OntRAG §5 knowledge.search 契约 REST 子集（2026-09-27 任务 2.3 补全）：
- hits（兼容保留）= citations 投影（content↔quote、document_id↔doc_id）；
- citations 全字段（chunk_id/doc_id/doc_name/minio_key/quote/span/score，quote=chunk 原文截片）；
- evidence.graph_paths（lite=类 IRI 链）+ community_reports（lite 恒空——完整档二期）；
- answers 抽取式摘要（每句挂 citation 引用）+ confidence（归一化 RRF 分）+ usage；
- degraded_reasons 细分降级因（vector_unavailable / mode_downgraded:global|drift）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CollectionCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    embedding_model: str = Field(default="bge-m3", max_length=64)


class CollectionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    name: str
    description: str | None
    embedding_model: str
    status: str
    created_at: datetime


class DocumentCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collection_id: uuid.UUID
    title: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1)  # M2 JSON 直传；MinIO 对象存储随 M3
    mime_type: str | None = Field(default="text/markdown", max_length=128)


class DocumentOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    collection_id: uuid.UUID
    title: str
    status: str
    size_bytes: int | None
    checksum_sha256: str
    created_at: datetime
    created: bool = True  # 幂等命中既有文档时为 False（§8.0 精确重复拒收并幂等返回）


class PipelineStartOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: uuid.UUID
    accepted: bool


class PipelineStepOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: str
    status: str
    attempt: int
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None


class PipelineProgressOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: uuid.UUID
    document_status: str
    degraded: bool
    steps: list[PipelineStepOut]


class KbSearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2048)
    top_k: int = Field(default=8, ge=1, le=50)
    mode: Literal["auto", "local", "global", "drift"] = "auto"  # 登记册默认 auto；lite 下 auto≡local
    kb_id: uuid.UUID | None = None  # 登记册/OntRAG §5 权威命名（原 collection_id 停用，extra=forbid 拒旧名）
    with_evidence: bool = True  # False=跳过图路、evidence 置空（§5 可选参数）
    entity_type_filter: list[str] | None = Field(default=None, max_length=20)  # 本体类 IRI（§5 签名）
    ontology_version: str | None = None  # 缺省=当前发布版（§5）
    max_hops: int = Field(default=2, ge=0, le=3)  # local/drift 图扩展跳数（§4.3 护栏 lite 上限 3）
    as_of: datetime | None = None  # bi-temporal 时点检索（参数权威=OntRAG §8.2）
    include_superseded: bool = False  # 被取代知识一并返回（带取代标注；lite=chunk/document 代际）


class KbHitOut(BaseModel):
    """兼容保留字段 = citations 投影（content↔quote、document_id↔doc_id）。"""

    model_config = ConfigDict(extra="forbid")
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    content: str
    score: float
    doc_name: str | None = None
    minio_key: str | None = None
    span: list[int] | None = None
    channels: list[str] = Field(default_factory=list)


class KbCitationOut(BaseModel):
    """OntRAG §5 citations 全字段（quote=chunk 原文截片；span 指向原文 [start, end)）。"""

    model_config = ConfigDict(extra="forbid")
    chunk_id: uuid.UUID
    doc_id: uuid.UUID
    doc_name: str | None = None
    minio_key: str | None = None
    quote: str
    span: list[int] | None = None
    score: float


class KbGraphNodeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    iri: str
    name: str | None = None
    type: str | None = None  # 直接父类 IRI（lite）


class KbGraphRelOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str  # subclass_of | same_class
    weight: float  # 信息值（层次 1.0 / 同类逐跳衰减），非排序分


class KbGraphPathOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[KbGraphNodeOut]
    rels: list[KbGraphRelOut]
    chunk_ids: list[uuid.UUID]  # 路径覆盖 chunk（锚定与扩展端点，供引用追溯）


class KbEvidenceOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    graph_paths: list[KbGraphPathOut] = Field(default_factory=list)
    community_reports: list[dict] = Field(default_factory=list)  # lite 恒空（完整档二期）


class KbAnswerSentenceOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    citations: list[uuid.UUID] = Field(default_factory=list)  # 来源 chunk_id（对齐 citations[].chunk_id）


class KbAnswerOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    confidence: float  # 归一化 RRF 分（top 分 / 当前通道集理论满分）
    sentences: list[KbAnswerSentenceOut] = Field(default_factory=list)
    citations: list[uuid.UUID] = Field(default_factory=list)


class KbUsageOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    latency_ms: int
    llm_calls: int = 0  # lite 抽取式摘要不引 LLM（§5 usage 形态）


class KbSearchOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    mode: str  # 请求 mode（回显）
    mode_used: str  # 实际执行模式（lite 恒 local；global/drift 降级）
    degraded: bool  # true=发生降级（嵌入路不可用 / global-drift 模式降级），调用方须标注
    degraded_reasons: list[str] = Field(default_factory=list)  # vector_unavailable / mode_downgraded:*
    channels: list[str]
    latency_ms: int
    hits: list[KbHitOut]  # 兼容保留（=citations 投影）
    citations: list[KbCitationOut] = Field(default_factory=list)
    evidence: KbEvidenceOut = Field(default_factory=KbEvidenceOut)
    answers: list[KbAnswerOut] = Field(default_factory=list)
    usage: KbUsageOut = Field(default_factory=KbUsageOut)

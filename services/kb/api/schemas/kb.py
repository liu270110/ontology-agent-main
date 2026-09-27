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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


# ---------------------------------------------------------------- 文档列表（R51 联调补齐 GET /kb/documents）

KbDocUiStatus = Literal["pending", "extracting", "indexed", "failed"]  # 前端 KbDocStatus 四态
KbDocType = Literal["PDF", "Word", "Excel", "CSV", "图片"]  # 前端 FileTypeBadge 五类

# 后端 documents.status 八态（database/01 DDL）→ 前端四态收敛：
# 抽取中=preprocessed/extracting/aligning/validating；等待=pending_review（候选待人工终审，
# 非入库非失败，重抽可用）；uploaded=待抽取。pending_review 不映射 extracting——避免前端
# 1.5s 轮询永不收敛（StatusBadge 仅对 extracting 行轮询）。
_UI_STATUS_OF: dict[str, KbDocUiStatus] = {
    "uploaded": "pending",
    "preprocessed": "extracting",
    "extracting": "extracting",
    "aligning": "extracting",
    "validating": "extracting",
    "pending_review": "pending",
    "indexed": "indexed",
    "failed": "failed",
}

# 前端状态筛（?status=）→ 后端八态集合（正反同表维护，词汇表收敛无注入面）
DOCUMENT_UI_STATUS_FILTER: dict[str, tuple[str, ...]] = {
    "pending": ("uploaded", "pending_review"),
    "extracting": ("preprocessed", "extracting", "aligning", "validating"),
    "indexed": ("indexed",),
    "failed": ("failed",),
}

# ?status= 接受面：前端四态别名 + 后端八态原值（原值走 (value,) 单值过滤）
DocumentStatusQuery = Literal[
    KbDocUiStatus,
    "uploaded",
    "preprocessed",
    "aligning",
    "validating",
    "pending_review",
]

# 前端类型筛（?type=）→ (title 后缀 LIKE 模式, mime ILIKE 模式)（?type= 可选过滤）
DOCUMENT_TYPE_FILTER: dict[KbDocType, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "PDF": (("%.pdf",), ("application/pdf%",)),
    "Word": (("%.doc", "%.docx"), ("%msword%", "%wordprocessingml%")),
    "Excel": (("%.xls", "%.xlsx"), ("%ms-excel%", "%spreadsheetml%")),
    "CSV": (("%.csv",), ("text/csv%",)),
    "图片": (("%.png", "%.jpg", "%.jpeg", "%.gif", "%.webp", "%.bmp", "%.svg"), ("image/%",)),
}


def ui_status_of(document_status: str) -> KbDocUiStatus:
    """后端八态 → 前端四态（未登记状态兜底 pending，不炸列表）。"""
    return _UI_STATUS_OF.get(document_status, "pending")


def doc_type_of(title: str, mime_type: str | None) -> KbDocType:
    """标题扩展名优先、mime 兜底的文档类型投影（mock 同款兜底=图片）。"""
    ext = title.rsplit(".", 1)[-1].upper() if "." in title else ""
    if ext == "PDF" or "pdf" in (mime_type or "").lower():
        return "PDF"
    if ext in ("DOC", "DOCX") or any(k in (mime_type or "").lower() for k in ("msword", "wordprocessingml")):
        return "Word"
    if ext in ("XLS", "XLSX") or any(k in (mime_type or "").lower() for k in ("ms-excel", "spreadsheetml")):
        return "Excel"
    if ext == "CSV" or "csv" in (mime_type or "").lower():
        return "CSV"
    return "图片"


class DocumentPipelineProgress(BaseModel):
    """流水线进度（R51 对账 DTO：step=已完成步数，total=全流水线步数）。"""

    model_config = ConfigDict(extra="forbid")

    step: int = 0
    total: int = 0


class DocumentListItem(BaseModel):
    """文档管理页行 DTO（live 对账口径：前端 KbDocument 全字段 + pipeline/tier/created_at）。"""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str  # = documents.title
    doc_type: KbDocType
    size: int = 0  # 字节（对账 DTO 命名）
    size_bytes: int | None = None  # 同义字段（前端 KbDocument.size_bytes）
    chunk_count: int = 0
    status: KbDocUiStatus
    progress: int = 0  # 0-100（done 步 / 流水线总步）
    pipeline: DocumentPipelineProgress = Field(default_factory=DocumentPipelineProgress)
    tier: str | None = None  # 文档分层后端未建模，恒 None（对账 DTO 占位）
    job_id: str | None = None  # lite 流水线进程内执行，无独立任务号
    error: str | None = None  # failed 步错误摘录
    created_at: datetime
    updated_at: datetime
    indexed_today: bool = False  # 当日入库（前端规模统计带）


class DocumentListData(BaseModel):
    """列表 data 面（信封解包后形态：{items,total,next_cursor} + offset/limit）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[DocumentListItem] = Field(default_factory=list)
    total: int = 0
    next_cursor: str | None = None  # 前端 listDocuments DTO 契约字段（offset/limit 分页恒 None）
    offset: int = 0
    limit: int = 100


class DocumentListEnvelope(BaseModel):
    """列表成功信封（live 对账口径：前端 client apiFetchEnvelope 强信封解包——同 R50 注）。"""

    model_config = ConfigDict(extra="forbid")

    code: int = 0
    message: str = "ok"
    data: DocumentListData


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
    # 源系统标识软路由（多源接入设计 §5.2：同词异义按源系统加权排序；None=维持相关度序。
    # 匹配文档 meta.source_system 的命中加分排前、不匹配者降序不剔除——软路由不硬过滤，
    # 硬过滤与分组返回 schema 随 v1.5 语境术语表落地）
    source_context: str | None = Field(default=None, max_length=64)


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


# ---------------------------------------------------------------- 终审工作台（api/01 §5.4 ★ 三端点）
#
# 候选实例消费面：B2 抽取深化已把 evidence.quote/span 与 violations 写进 kb_facts，本组 DTO 是
# 它们到达前端终审工作台的正式通道（列表项全量透出 = 工作台裁决依据）。

# 与 kb_facts CheckConstraint 同词汇表（database/01 §3.3 DDL 权威；ORM=services/kb/data/orm.py）
FactTypeFilter = Literal["entity", "relation", "attribute", "event"]
ReviewStatusFilter = Literal["candidate", "rejected", "authoritative"]

# edit_accept 可修订字段（api/01 §5.4「修订后入审」；subject_type 不在内——术语对齐结论由
# align 步归一，人工改类走重新对齐而非工作台直改）
_EDITABLE_FIELDS = ("subject", "predicate", "object", "object_type", "canonical_name")


class CandidateEditIn(BaseModel):
    """edit_accept 编辑载荷：None=不覆盖既有值（「可空字段不覆盖」——缺省字段保持原样）。"""

    model_config = ConfigDict(extra="forbid")

    subject: str | None = Field(default=None, min_length=1, max_length=256)
    predicate: str | None = Field(default=None, min_length=1, max_length=256)
    object: str | None = Field(default=None, min_length=1, max_length=1024)
    object_type: str | None = Field(default=None, min_length=1, max_length=128)
    canonical_name: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> CandidateEditIn:
        if all(getattr(self, name) is None for name in _EDITABLE_FIELDS):
            raise ValueError("编辑载荷至少提供一个待修订字段（api/01 §5.4 edit_accept）")
        return self

    def provided(self) -> dict[str, str]:
        """显式提供的字段（排除 None）；应用侧据此「可空字段不覆盖」。"""
        return {name: getattr(self, name) for name in _EDITABLE_FIELDS if getattr(self, name) is not None}


class CandidateDecisionIn(BaseModel):
    """单条终审决策请求（api/01 §5.4 ★ POST /kb/review/candidates/{cid}/decision）。

    action=accept|reject|edit_accept；edit_accept 必附编辑载荷（契约原文「修订后入审」），
    accept/reject 拒带编辑载荷（防呆：编辑语义只属 edit_accept）。DTO 校验失败 → 3001 统一错误体。
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["accept", "reject", "edit_accept"]
    edit: CandidateEditIn | None = None

    @model_validator(mode="after")
    def _edit_payload_only_for_edit_accept(self) -> CandidateDecisionIn:
        if self.action == "edit_accept" and self.edit is None:
            raise ValueError("edit_accept 必附编辑载荷（api/01 §5.4：修订后入审）")
        if self.action != "edit_accept" and self.edit is not None:
            raise ValueError("仅 edit_accept 接受编辑载荷")
        return self


class BatchCandidateDecisionIn(CandidateDecisionIn):
    """批量决策条目（candidate_id 定位候选；action/edit 语义同单条）。"""

    candidate_id: uuid.UUID


class BatchDecisionIn(BaseModel):
    """批量终审决策请求体（api/01 §5.4 ★ batch-decision；上限 200 条/批在端点侧断言 → 3001）。"""

    model_config = ConfigDict(extra="forbid")

    decisions: list[BatchCandidateDecisionIn] = Field(min_length=1)


class ReviewCandidateEvidenceOut(BaseModel):
    """证据信封（B2 双层结构全量透出）：quote=LLM 自报引语、span=引语在 chunk 内 [start, end)、
    source_ref=document/doc_version/chunk_id/span 四元组（终审逐字比对与出处回指依据）。"""

    model_config = ConfigDict(extra="forbid")

    source_ref: dict[str, Any] = Field(default_factory=dict)
    quote: str | None = None
    span: list[int] | None = None


class ReviewCandidateOut(BaseModel):
    """候选实例列表项（工作台裁决依据全量透出：evidence.quote 与 violations 必在列）。"""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    fact_type: str
    subject: str
    subject_type: str | None
    predicate: str | None
    object: str | None
    object_type: str | None
    canonical_name: str | None
    aliases: list[str] = Field(default_factory=list)
    confidence: float  # 门禁参数非真值（standards §5.3）
    status: str  # candidate | rejected | authoritative（authoritative=人工终审后唯一生效态）
    evidence: ReviewCandidateEvidenceOut
    violations: list[dict[str, Any]] = Field(default_factory=list)  # 规则违例 + SHACL 结论（validate 回写）
    align: dict[str, Any] | None = None  # meta["align"] 术语对齐决策（tier/status/reason 可追溯）
    created_at: datetime


class ReviewCandidatePageMetaOut(BaseModel):
    """分页 meta（api/01 §3.1 meta 惯例；offset 分页）。"""

    model_config = ConfigDict(extra="forbid")

    offset: int
    limit: int
    total: int


class ReviewCandidatePageOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ReviewCandidateOut]
    meta: ReviewCandidatePageMetaOut


class CandidateDecisionOut(BaseModel):
    """单条决策结果（202 受理；status=决策后事实状态）。"""

    model_config = ConfigDict(extra="forbid")

    candidate_id: uuid.UUID
    action: str
    status: str  # authoritative（accept）/ rejected（reject）/ candidate（edit_accept 修订后入审）
    trail_recorded: bool  # 决策留痕是否落到候选 open 单（无单=false——容忍票据缺失，主断言在状态翻转）


class BatchCandidateDecisionItemOut(BaseModel):
    """批量逐条结果（部分成功语义：单条失败不整批回滚）。"""

    model_config = ConfigDict(extra="forbid")

    candidate_id: uuid.UUID
    ok: bool
    status: str | None = None  # ok=true 时为决策后事实状态
    error: str | None = None  # ok=false 时为「错误码 + 消息」（404/409/4701 等）


class BatchCandidateDecisionMetaOut(BaseModel):
    """批量结果汇总（响应 meta；edited=edit_accept 成功数，accept/reject 成功各计 accepted/rejected）。"""

    model_config = ConfigDict(extra="forbid")

    accepted: int
    rejected: int
    edited: int
    failed: int


class BatchCandidateDecisionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[BatchCandidateDecisionItemOut]
    meta: BatchCandidateDecisionMetaOut

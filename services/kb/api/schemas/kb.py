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

from services.platform.schemas import EmptyMeta, PageMeta


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


class CollectionListOut(BaseModel):
    """集合列表（api/01 §3.1 信封：{data, meta:{page,page_size,total}}，B1 批统一——
    原 {code,message,data} 旧信封废止）。"""

    model_config = ConfigDict(extra="forbid")

    data: list[CollectionOut] = Field(default_factory=list)
    meta: PageMeta


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
# 前端 FileTypeBadge 六类；「文本」=docs/Agent/09 §2.1 工程问题 4（text/* 收敛，不再误判图片）
KbDocType = Literal["PDF", "Word", "Excel", "CSV", "文本", "图片"]

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
    "文本": (("%.md", "%.markdown", "%.txt", "%.json"), ("text/%", "application/json%")),
    "图片": (("%.png", "%.jpg", "%.jpeg", "%.gif", "%.webp", "%.bmp", "%.svg"), ("image/%",)),
}


def ui_status_of(document_status: str) -> KbDocUiStatus:
    """后端八态 → 前端四态（未登记状态兜底 pending，不炸列表）。"""
    return _UI_STATUS_OF.get(document_status, "pending")


def doc_type_of(title: str, mime_type: str | None) -> KbDocType:
    """标题扩展名优先、mime 兜底的文档类型投影（docs/Agent/09 §2.1 工程问题 4：text/* 收敛
    「文本」，markdown/plain/json 不再误判「图片」；CSV 先于 text/* 通配比对；未登记类型
    兜底=图片，mock 同款）。"""
    ext = title.rsplit(".", 1)[-1].upper() if "." in title else ""
    if ext == "PDF" or "pdf" in (mime_type or "").lower():
        return "PDF"
    if ext in ("DOC", "DOCX") or any(k in (mime_type or "").lower() for k in ("msword", "wordprocessingml")):
        return "Word"
    if ext in ("XLS", "XLSX") or any(k in (mime_type or "").lower() for k in ("ms-excel", "spreadsheetml")):
        return "Excel"
    if ext == "CSV" or "csv" in (mime_type or "").lower():
        return "CSV"
    if ext in ("MD", "MARKDOWN", "TXT", "TEXT", "JSON") or (mime_type or "").lower().startswith(
        ("text/", "application/json")
    ):
        return "文本"
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
    degraded: list[str] = Field(default_factory=list)  # 软降级步清单（如实透出 meta["degraded"]，如 ["embed"]）
    created_at: datetime
    updated_at: datetime
    indexed_today: bool = False  # 当日入库（前端规模统计带）


class DocumentListOut(BaseModel):
    """文档列表（api/01 §3.1 信封：{data, meta:{page,page_size,total}}，B1 批统一——
    原 {code,message,data:{items,total,next_cursor,offset,limit}} 旧信封废止，A-5 证据端点）。"""

    model_config = ConfigDict(extra="forbid")

    data: list[DocumentListItem] = Field(default_factory=list)
    meta: PageMeta


# ---------------------------------------------------------------- 单文档详情/删除（R17-a/R17-b live 对账增量）


class DocumentDetailData(DocumentListItem):
    """单文档详情 data 面（R17-a）：复用列表行全字段（chunk_count/error 在列）+ 定位字段。"""

    model_config = ConfigDict(extra="forbid")

    collection_id: uuid.UUID  # = documents.kb_collection_id（详情页回跳/流水线再触发定位用）
    mime_type: str | None = None


class DocumentDetailEnvelope(BaseModel):
    """单文档详情（api/01 §3.1 非列表包裹 {data, meta}，meta=空对象；旧 {code,message,data} 信封废止）。"""

    model_config = ConfigDict(extra="forbid")

    data: DocumentDetailData
    meta: EmptyMeta = Field(default_factory=EmptyMeta)


class DocumentDeleteCascade(BaseModel):
    """下线分片计数（墓碑口径：chunks/kb_facts/checkpoint 物理保留，仅随文档 valid_to 封口
    下线检索——计数为被下线入检索的当前有效分片数）。"""

    model_config = ConfigDict(extra="forbid")

    chunks: int = 0


class DocumentDeleteData(BaseModel):
    """删除结果 data 面（墓碑式软删幂等：文档不存在/已墓碑亦 200 deleted=false，不 404）。"""

    model_config = ConfigDict(extra="forbid")

    deleted: bool
    cascade: DocumentDeleteCascade = Field(default_factory=DocumentDeleteCascade)


class DocumentDeleteEnvelope(BaseModel):
    """删除成功信封（R17-b 恒 200 口径保留，包裹形态改契约 {data, meta}——deleted 区分
    命中/幂等未命中；旧 {code,message,data} 信封废止）。"""

    model_config = ConfigDict(extra="forbid")

    data: DocumentDeleteData
    meta: EmptyMeta = Field(default_factory=EmptyMeta)


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
    # agentic 检索（AgenticRAG优化方案 §8.1 冻结契约）：v1 默认 false=零行为变化红线；
    # true 走服务端 A0 代跑管线（判别→检索→评级→术语归一改写纠错，纯规则档零 LLM）
    agentic: bool = False
    max_rounds: int = Field(default=2, ge=1, le=2)  # 纠错轮上限（§2.3 红线 ≤2，PoC 后标定）


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
    type: str  # subclass_of | same_class | 关系谓词名（图三查权威关系边）
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


# ---------------------------------------------------------------- 分片列表（api/01 §5.4 ★ chunks 行，FR-KB-03）


class KbChunkOut(BaseModel):
    """分片预览项（FR-KB-03）：content=原文预览截断（api 层 ≤500 字符）；span=命中高亮偏移
    （chunk.meta.span，指向原文 [start, end)）；has_embedding=布尔（向量本体不回传）。"""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    document_id: uuid.UUID
    seq: int
    content: str
    token_count: int | None = None
    page_no: int | None = None
    span: list[int] | None = None
    has_embedding: bool = False  # pgvector 列缺失 / 未向量化恒 False（降级契约 BM25-only）
    created_at: datetime


class KbChunkPageOut(BaseModel):
    """分片列表（api/01 §3.1 信封：{data, meta:{page,page_size,total}}，B1 批统一）。"""

    model_config = ConfigDict(extra="forbid")
    data: list[KbChunkOut] = Field(default_factory=list)
    meta: PageMeta


# ---------------------------------------------------------------- 图三查（api/01 §5.4 ★ GET /kb/graph/*）


class KbGraphQueryOut(BaseModel):
    """图三查统一响应（lite=类级图，retrieval/graph.py 纯函数）：节点/边列表。

    空结果非失败（OntRAG §4）：未知类 IRI / 无路径 → 200 空 nodes/rels，404 仅用于资源不存在。
    """

    model_config = ConfigDict(extra="forbid")

    nodes: list[KbGraphNodeOut] = Field(default_factory=list)
    rels: list[KbGraphRelOut] = Field(default_factory=list)


# ---------------------------------------------------------------- agentic 检索 trace（§8.1 冻结契约）


class KbAgenticRoundOut(BaseModel):
    """单轮时间线条目（前端 AgenticTracePanel 步进数据源；枚举与 §8.1 逐字一致）。"""

    model_config = ConfigDict(extra="forbid")

    seq: int  # 1 起步轮号
    action: Literal["search", "rewrite_search"]
    query: str  # 本轮实际检索词
    rewrite_basis: str | None = None  # 改写依据（term_alias:<标签>）；search 轮恒 None
    grade: Literal["pass", "fail"]
    grade_reason: Literal["pass", "hit_count_zero", "score_below_threshold", "span_missing"]


class KbAgenticTraceOut(BaseModel):
    """agentic 循环 trace（响应 agentic 块；agentic=false 请求该块恒 None，存量消费方零影响）。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["rule"] = "rule"  # v1 固定 rule（LLM 档关闭，阈值 PoC 后开 hybrid）
    decision: Literal["retrieval_required", "retrieval_skipped"]
    decision_reason: Literal["deterministic_task", "smalltalk_pattern", "default_retrieve"]
    rounds: list[KbAgenticRoundOut] = Field(default_factory=list)  # 0~2 轮时间线
    degraded: Literal["agentic_exhausted"] | None = None  # 前端必须显著提示
    explain_trace_id: str  # "kb-agentic:" + uuid（knowledge.explain 回放键）


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
    agentic: KbAgenticTraceOut | None = None  # §8.1：agentic=false 请求恒 None（零行为变化）


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


class ReviewCandidatePageOut(BaseModel):
    """终审候选列表（api/01 §3.1 信封：{data, meta:{page,page_size,total}}，B1 批统一）。"""

    model_config = ConfigDict(extra="forbid")
    data: list[ReviewCandidateOut]
    meta: PageMeta


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


# ---------------------------------------------------------------- needs_review 聚合复核（§7.1 / §8.1 承接判定）
#
# 主文档 §11 待办「needs_review 聚合复核的交互设计（按 subject/主题分组复核）」：单部标准
# 换版可产生数千条 needs_review 候选，逐条复核不可运行——按 subject 分组聚合展示，复核人
# 按组全量裁决；逐行留痕（open 单 payload["decisions"] + 行内审计，见 business/review_queue）。


class ReviewQueueSampleOut(BaseModel):
    """组内样本引用（≤3 条，最旧优先）：回指候选事实与 chunk/document 出处。"""

    model_config = ConfigDict(extra="forbid")

    fact_id: uuid.UUID
    document_id: uuid.UUID
    chunk_id: uuid.UUID | None = None


class ReviewQueueGroupOut(BaseModel):
    """一个 subject/主题聚合组（subject_type 为组属性；最旧组优先由服务层排序保证）。"""

    model_config = ConfigDict(extra="forbid")

    subject: str
    subject_type: str | None = None
    count: int
    oldest_created_at: datetime
    samples: list[ReviewQueueSampleOut] = Field(default_factory=list)


class ReviewQueueSummaryOut(BaseModel):
    """聚合复核队列响应（groups 空列表=空队列合法态；total=队列候选总条数）。"""

    model_config = ConfigDict(extra="forbid")

    groups: list[ReviewQueueGroupOut] = Field(default_factory=list)
    total: int = 0


class ReviewQueueBatchDecideIn(BaseModel):
    """全组批量裁决请求（OntRAG §7 批量审核交互）：decision 映射 kb_facts.status。"""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=1024)
    decision: Literal["authoritative", "rejected"]  # 越枚举 → FastAPI 422（3001 统一错误体）
    comment: str | None = Field(default=None, max_length=512)  # 裁决意见（逐行留痕携带）


class ReviewQueueBatchDecideOut(BaseModel):
    """全组裁决结果：decided=裁决行数；trail_recorded=其中落到 open 单留痕的行数
    （行内审计恒写不计入——无单行也有痕，见 business/review_queue 复用结论）。"""

    model_config = ConfigDict(extra="forbid")

    subject: str
    decision: str
    decided: int
    trail_recorded: int


# ---------------------------------------------------------------- 冲突工单只读面（§8.1 T2 裁决；A1 接线 2026-10-04）
#
# 工单行（review_tickets，target_type=conflict）经 run_validate 尾调分诊生成（T2 真矛盾），
# 终审分流（candidate decision accept/reject → conflict_triage.apply_decision）执行裁决；
# 本组 DTO 只读透出（列表/详情），裁决动作面=既有终审决策端点（§5.4 review:approve）。


ConflictResolutionFilter = Literal[
    "pending",  # 未裁决（工单 open：draft/pending_review，信封无 conflict_decision）
    "winner_a",  # 点选 fact_a（候选新方）胜出
    "winner_b",  # 点选 fact_b（既有权威方）胜出
    "t3_coexist",  # 人工判定限定共存
]  # conflict_triage.DECISION_OPTIONS 同源（§8.1 v1 裁决选项；缺省=全部）


class ConflictFactSideOut(BaseModel):
    """冲突并排单侧事实（§8.1「两条事实+各自原文出处」；digest=建单快照 + status=实时行态）。"""

    model_config = ConfigDict(extra="forbid")

    fact_id: uuid.UUID
    fact_type: str | None = None
    subject: str | None = None
    predicate: str | None = None
    object: str | None = None
    status: str | None = None  # kb_facts 实时行态（authoritative/rejected/candidate；行缺=None）
    confidence: float | None = None
    scope: dict[str, Any] = Field(default_factory=dict)  # meta.scope（T3 人工填 scope 落点）
    source_ref: dict[str, Any] = Field(default_factory=dict)  # evidence.source_ref 四元组
    quote: str | None = None  # evidence 逐字引语（出处）
    span: list[int] | None = None  # evidence 原文定位


class ConflictTicketOut(BaseModel):
    """冲突工单列表项（api/01 §3.1 信封 data 项；resolution 缺省 pending=未裁决）。"""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    status: str  # review_tickets 六态（draft/pending_review/approved/published/rejected/cancelled）
    resolution: str = "pending"  # 信封 conflict_decision.resolution；无则 pending（open 单）
    target_id: uuid.UUID  # 冲突候选方事实 id（fact_a）
    conflict_type: str | None = None  # 信封 payload.conflict_type（T2）
    created_at: datetime


class ConflictPageOut(BaseModel):
    """冲突工单列表（api/01 §3.1 信封：{data, meta:{page,page_size,total}}）。"""

    model_config = ConfigDict(extra="forbid")
    data: list[ConflictTicketOut]
    meta: PageMeta


class ConflictDetailOut(BaseModel):
    """冲突工单详情（并排双方事实与出处 + 裁决留痕；终审工作台 T2 裁决数据面）。"""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    status: str
    resolution: str = "pending"
    target_id: uuid.UUID
    conflict_type: str | None = None
    decision_options: list[str] = Field(default_factory=list)  # 信封 decision_options（裁决选项面）
    fact_a: ConflictFactSideOut  # 候选新方（target_id 同侧）
    fact_b: ConflictFactSideOut  # 既有权威方
    decision: dict[str, Any] | None = None  # conflict_decision 留痕补丁（resolution/resolved_by/comment/decided_at）
    created_at: datetime

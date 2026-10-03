"""L2 知识库（kb）路由：文档上传 → 流水线 → knowledge.search lite（M2.5）。

端点（api/01 登记册 kb 行；OntRAG §5 检索契约 REST 子集）：
    POST /kb/collections                      建库
    GET  /kb/documents                        文档列表（R51 联调补齐：信封 + 前端
                                              KbDocument DTO；status/type/q 可选过滤 +
                                              offset/limit 分页，limit 缺省 50 上限 200）
    GET  /kb/documents/{id}                   文档详情（R17-a live 对账补齐：列表 DTO 全字段
                                              + chunk 计数/error；404=文档域 404* 同款错误体）
    DELETE /kb/documents/{id}                 删除文档（B6 墓碑式软删：documents.valid_to 封口
                                              下线检索，chunks/kb_facts/审计物理保留；幂等
                                              恒 200，已删态与不存在对调用方等价不 404）
    POST /kb/documents                        JSON 内容直传（MinIO 随 M3；checksum 幂等；
                                              文本类 mime 白名单 + NUL 拒收 → 415 业务错误）
    POST /kb/documents/{id}/pipeline/start    后台流水线（202 受理；M2 lite 四步 / M2 full
                                              七步中段 extract/align/validate 已插回）
    POST /kb/documents/{id}/pipeline/retry    失败文档流水线重试（B6：202 受理 + 后台断点续跑，
                                              非 failed 态 409）
    GET  /kb/documents/{id}/chunks            分片列表（B6：seq 升序 + offset/limit 分页 +
                                              meta.total；content 预览截断/has_embedding 布尔）
    GET  /kb/graph/search                     图检索（B6：q/class_name 匹配类 + 层次/关系扩展）
    GET  /kb/graph/neighborhood               邻域查询（B6：类 IRI depth≤2 一跳近邻 + 关系边）
    GET  /kb/graph/path                       路径查询（B6：类层次图内 BFS 最短路）
    POST /kb/search                           knowledge.search lite：bm25+向量+图三路 RRF
                                              （图=LazyGraphRAG lite 查询时扩展，retrieval/graph.py）
    GET  /kb/documents/{id}/review/candidates 终审候选列表（api/01 §5.4 ★，quote/violations 透出）
    POST /kb/review/candidates/{cid}/decision     单条终审决策 accept|reject|edit_accept（202）
    POST /kb/documents/{id}/review/batch-decision 批量终审决策（≤200 条/批，逐条独立执行，202）
    GET  /kb/review-queue                     needs_review 聚合复核队列（§7.1 批次纪律：
                                              按 subject 分组聚合——计数/最旧 created_at/样本 ≤3）
    POST /kb/review-queue/batch-decide        按 subject 全组批量裁决（authoritative/rejected，
                                              逐行留痕；主文档 §11 待办聚合复核交互面）

scope：kb:write（写路径）/ kb:read（检索与进度），deny-by-default（08 §2.5）；
终审工作台三端点持 review:read / review:approve（契约 §5.4 行；候选非成品门禁的裁决面）。
降级契约：嵌入模型不可用 / pgvector 列缺失 → BM25-only 且 degraded=true；
mode=global/drift 无社区摘要索引 → 降级 local 且 degraded=true（drift/完整档二期）；
流水线 embed 步软降级（document.meta["degraded"]=["embed"]，可重跑补向量）。
检索响应 = OntRAG §5 契约 REST 子集全量（citations/evidence.graph_paths/answers/usage）。
ACL 标签面接线（OntRAG §4.3，2026-09-27 任务 1）：X-Acl-Tags 请求头（逗号分隔）→
AclPushdown（开关 OA_KB_ACL_FILTER_ENABLED + 标签面双条件激活）→ 三路同源谓词下推；
无头 = no-op（调用方未接入标签面，兼容红线）。
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError

from services.kb.api.schemas.kb import (
    DOCUMENT_TYPE_FILTER,
    DOCUMENT_UI_STATUS_FILTER,
    BatchCandidateDecisionItemOut,
    BatchCandidateDecisionMetaOut,
    BatchCandidateDecisionOut,
    BatchDecisionIn,
    CandidateDecisionIn,
    CandidateDecisionOut,
    CollectionCreateIn,
    CollectionOut,
    DocumentCreateIn,
    DocumentDeleteCascade,
    DocumentDeleteData,
    DocumentDeleteEnvelope,
    DocumentDetailData,
    DocumentDetailEnvelope,
    DocumentListData,
    DocumentListEnvelope,
    DocumentListItem,
    DocumentOut,
    DocumentPipelineProgress,
    DocumentStatusQuery,
    FactTypeFilter,
    KbAgenticTraceOut,
    KbAnswerOut,
    KbAnswerSentenceOut,
    KbChunkOut,
    KbChunkPageMetaOut,
    KbChunkPageOut,
    KbCitationOut,
    KbDocType,
    KbEvidenceOut,
    KbGraphNodeOut,
    KbGraphPathOut,
    KbGraphQueryOut,
    KbGraphRelOut,
    KbHitOut,
    KbSearchIn,
    KbSearchOut,
    KbUsageOut,
    PipelineProgressOut,
    PipelineStartOut,
    PipelineStepOut,
    ReviewCandidateEvidenceOut,
    ReviewCandidateOut,
    ReviewCandidatePageMetaOut,
    ReviewCandidatePageOut,
    ReviewQueueBatchDecideIn,
    ReviewQueueBatchDecideOut,
    ReviewQueueGroupOut,
    ReviewQueueSampleOut,
    ReviewQueueSummaryOut,
    ReviewStatusFilter,
    doc_type_of,
    ui_status_of,
)
from services.kb.business.agentic import run_agentic_search
from services.kb.business.kb_pipeline import (
    M2_FULL_STEPS,
    PipelineError,
    assert_document_transition,
    run_pipeline,
)
from services.kb.business.review_queue import ReviewQueueService
from services.kb.business.search_service import rerank_hits_by_source_context
from services.kb.data.orm import Document, DocumentChunk, KbCollection, KbFact, KbPipelineStep
from services.kb.retrieval.embed import AclPushdown, OllamaEmbedder, bm25_search, vector_ready, vector_search
from services.kb.retrieval.graph import (
    ClassHierarchy,
    GraphQueryResult,
    authoritative_relations,
    build_class_hierarchy,
    expand_graph,
    graph_neighborhood,
    graph_search,
    graph_shortest_path,
)
from services.kb.retrieval.retrieve import (
    ExtractiveAnswer,
    GraphExpansion,
    GraphPath,
    SearchHit,
    hybrid_search,
    resolve_mode,
)
from services.ontology.business.hierarchy_service import get_class_hierarchy

# 跨模块显式服务调用（standards/01 §2.1 规则 3：business 为许可面，调用处注释模块文档）：
# 类层次读模型（database/01 §3.4）经 ontology 公开服务获取（kb 禁入 ontology.data）；
# 消费场景 = LazyGraphRAG lite 类闭包扩展（docs/OntRAG §4.0）。禁放 ontology.api——api 链触达
# ontology.data 会击穿「ontology.data 模块私有」契约（import-linter 强制）。
from services.platform.deps import Principal, SessionDep, get_session_factory, require_scope
from services.platform.errors import ErrorCode, GatewayError
from services.platform.ports.model_port import ModelPort

if TYPE_CHECKING:  # 仅类型注解（运行时零 import——app.py 同款纪律）
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/kb", tags=["knowledge-base"])

KbReadDep = Annotated[Principal, Depends(require_scope("kb:read"))]
KbWriteDep = Annotated[Principal, Depends(require_scope("kb:write"))]
# 终审工作台三端点（api/01 §5.4 ★ 行）：裁决面 scope 走 review 段，与 admin 审批面同权
ReviewReadDep = Annotated[Principal, Depends(require_scope("review:read"))]
ReviewApproveDep = Annotated[Principal, Depends(require_scope("review:approve"))]

logger = logging.getLogger("services.gateway.kb")

_HIERARCHY_TTL_SECONDS = 300.0  # 类层次进程内缓存 TTL（任务口径：lite 档不追发布事件失效；示例值）


def _acl_tags_from_request(request: Request) -> list[str] | None:
    """调用方 acl 标签面提取（OntRAG §4.3；X-Acl-Tags 请求头，逗号分隔）。

    - 无头 → None：调用方未接入标签面，开关开启也不激活谓词（no-op，兼容红线）；
    - 有头空值 → []：显式空标签面，deny-by-default（仅未标注文档可见）；
    - 有头 → 去空白后逐段取标签。
    标签面属调用方授权上下文，只从请求头（通道侧信道）采集，禁从 body/query 参数采集
    （不可信输入不作授权依据，capability_provider 红线同源）。
    """
    raw = request.headers.get("x-acl-tags")
    if raw is None:
        return None
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


def _embedder(state: object) -> OllamaEmbedder:
    """进程内复用的嵌入客户端（挂 app.state；base_url/协议=config.ollama_base_url/embed_protocol，
    组合根装配点：OA_EMBED_PROTOCOL=tei 切换 TEI 协议，docs/Agent/09 §2.1 工程问题 2）。"""
    cached = getattr(state, "_kb_embedder", None)
    if cached is None:
        cached = OllamaEmbedder(
            state.settings.ollama_base_url,  # type: ignore[attr-defined]
            protocol=state.settings.embed_protocol,  # type: ignore[attr-defined]
        )
        state._kb_embedder = cached  # type: ignore[attr-defined]
    return cached


def get_model_port(request: Request) -> ModelPort | None:
    """M2.5：取组合根（lifespan）装配于 app.state.model_port 的模型端口。

    无 LLM 配置时为 None——流水线 extract 步将以 5002 LLM_UNAVAILABLE 失败（可重跑），
    不阻塞路由与启动（kb_pipeline 模块头「不做 mock」口径）。
    """
    return getattr(request.app.state, "model_port", None)


ModelPortDep = Annotated[ModelPort | None, Depends(get_model_port)]


async def _load_document(session: AsyncSession, tenant_id: uuid.UUID, document_id: uuid.UUID) -> Document:
    """当前有效文档载入（墓碑口径：valid_to IS NULL——已封口文档对详情/分片/候选/流水线/
    重试等读写面一律 404 不可见；DELETE 的幂等判定自行装载含墓碑行，不经此处）。"""
    doc = (
        await session.execute(
            select(Document).where(
                Document.id == document_id,
                Document.tenant_id == tenant_id,
                Document.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise GatewayError(404, "文档不存在", status_code=404)
    return doc


@router.post("/collections", status_code=status.HTTP_201_CREATED, summary="创建知识库集合")
async def create_collection(body: CollectionCreateIn, principal: KbWriteDep, session: SessionDep) -> CollectionOut:
    collection = KbCollection(
        tenant_id=principal.tenant_id,
        name=body.name,
        description=body.description,
        embedding_model=body.embedding_model,
    )
    session.add(collection)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise GatewayError(409, "同名知识库已存在", status_code=409) from exc
    await session.refresh(collection)
    return CollectionOut(
        id=collection.id,
        name=collection.name,
        description=collection.description,
        embedding_model=collection.embedding_model,
        status=collection.status,
        created_at=collection.created_at,
    )


# 摄取入口两层防御（docs/Agent/09 §2.1 工程问题 3「二进制健壮性」）：M2 唯一通道是 JSON
# content:str 直传，二进制（PDF/Office/图片）经 UTF-8 有损解码混入 NUL/不可译字符会在 PG
# JSONB 落库时抛 UntranslatableCharacter（裸 500）；前置业务 415 拒收（错误码沿用契约已登记
# 的 3004 UNSUPPORTED_MEDIA_TYPE「Content-Type 不支持」，HTTP 状态 415 Unsupported Media Type）。
_TEXT_MIME_PREFIXES = ("text/",)  # text/markdown、text/x-markdown、text/plain、text/csv 等
_TEXT_MIME_EXACT = frozenset({"application/json"})  # 结构化文本直传


def _reject_binary_payload(mime_type: str | None, content: str) -> None:
    """mime 白名单 + NUL 清洗前置（415 业务错误体，二进制垃圾禁入存储层；preprocess 之前执行）。

    - mime 参数段剥离（「text/markdown; charset=utf-8」→ text/markdown）后小写比对；
      mime 缺省视为 text/markdown（DTO 默认值同口径）；
    - content 含 \\x00 直接拒收（疑似二进制；其余控制字符不动，保留既有 \\r\\n 规整链路）。
    """
    mime = (mime_type or "text/markdown").split(";", 1)[0].strip().lower()
    if not (mime.startswith(_TEXT_MIME_PREFIXES) or mime in _TEXT_MIME_EXACT):
        raise GatewayError(
            ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            f"不支持的文档类型 {mime}：本通道仅接受文本类（text/* 与 application/json），"
            "二进制文件摄取通道随 M3 MinIO 预签名上传落地",
            status_code=415,
        )
    if "\x00" in content:
        raise GatewayError(
            ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            "文档内容含 NUL 控制字符（疑似二进制字节），请以文本内容重新上传",
            status_code=415,
        )


@router.post("/documents", summary="上传文档（M2 JSON 内容直传；checksum 幂等；文本类 mime 白名单 + NUL 拒收）")
async def create_document(body: DocumentCreateIn, principal: KbWriteDep, session: SessionDep) -> DocumentOut:
    """§8.0 同源检测第①级：精确重复拒收并幂等返回既有文档（created=false）。

    入口两层防御（415 业务错误，防二进制垃圾裸 500）：①mime 白名单（text/* 与
    application/json）；②content 含 \\x00 拒收——均在 checksum/落库之前执行。
    """
    _reject_binary_payload(body.mime_type, body.content)
    collection = (
        await session.execute(
            select(KbCollection).where(
                KbCollection.id == body.collection_id, KbCollection.tenant_id == principal.tenant_id
            )
        )
    ).scalar_one_or_none()
    if collection is None:
        raise GatewayError(404, "知识库不存在", status_code=404)

    checksum = hashlib.sha256(body.content.encode("utf-8")).hexdigest()
    live_checksum = (  # 唯一性=部分唯一索引（valid_to IS NULL）：墓碑文档不占唯一性，同内容重传=全新插入
        Document.tenant_id == principal.tenant_id,
        Document.kb_collection_id == body.collection_id,
        Document.checksum_sha256 == checksum,
        Document.valid_to.is_(None),
    )
    existing = (await session.execute(select(Document).where(*live_checksum))).scalar_one_or_none()
    if existing is not None:
        return _document_out(existing, created=False)  # 幂等命中既有文档

    doc_id = uuid.uuid4()  # 显式生成以拼 minio_key（orm/base 默认值仅作用于未赋值主键）
    doc = Document(
        id=doc_id,
        tenant_id=principal.tenant_id,
        kb_collection_id=body.collection_id,
        title=body.title,
        mime_type=body.mime_type,
        size_bytes=len(body.content.encode("utf-8")),
        minio_key=f"raw-docs/{principal.tenant_id}/{body.collection_id}/{doc_id}/source.md",
        checksum_sha256=checksum,
        meta={"content": body.content},  # M2 直传内容暂存 meta；MinIO 迁移随 M3
        status="uploaded",
    )
    session.add(doc)
    try:
        await session.commit()
    except IntegrityError as exc:  # 并发重复上传兜底（uk checksum，部分唯一索引同键）
        await session.rollback()
        raced = (await session.execute(select(Document).where(*live_checksum))).scalar_one_or_none()
        if raced is None:
            raise GatewayError(409, "文档写入冲突", status_code=409) from exc
        return _document_out(raced, created=False)
    await session.refresh(doc)
    return _document_out(doc, created=True)


def _document_out(doc: Document, *, created: bool) -> DocumentOut:
    return DocumentOut(
        id=doc.id,
        collection_id=doc.kb_collection_id,
        title=doc.title,
        status=doc.status,
        size_bytes=doc.size_bytes,
        checksum_sha256=doc.checksum_sha256,
        created_at=doc.created_at,
        created=created,
    )


@router.get("/documents", summary="文档列表（管理页；status/type/q 可选过滤 + offset/limit 分页）")
async def list_documents(
    principal: KbReadDep,
    session: SessionDep,
    status_filter: Annotated[DocumentStatusQuery | None, Query(alias="status")] = None,
    type_filter: Annotated[KbDocType | None, Query(alias="type")] = None,
    q: Annotated[str | None, Query(max_length=128)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> DocumentListEnvelope:
    """当前有效文档分页（bi-temporal：valid_to IS NULL，最新优先），R51 联调补齐。

    live 对账契约（2026-09-28）：① 前端 client apiFetchEnvelope 强信封解包 → 返回
    {code,message,data:{items,total,next_cursor}}；② items=前端 KbDocument 全字段
    （name/doc_type/size_bytes/chunk_count/status 四态/progress/job_id/error/updated_at/
    indexed_today）+ pipeline{step,total}/size/created_at/tier；③ ?status=（前端四态别名
    或后端八态原值）与 ?type=（五类文档类型）均为可选过滤；④ ?q= 名称模糊（title ILIKE，
    LIKE 通配符转义防注入）+ ?limit=/?offset= 分页（缺省 50，上限 200）。空列表合法。
    """
    statuses: tuple[str, ...] | None = None
    if status_filter is not None:
        statuses = DOCUMENT_UI_STATUS_FILTER.get(status_filter, (status_filter,))

    conditions = [Document.tenant_id == principal.tenant_id, Document.valid_to.is_(None)]
    if statuses is not None:
        conditions.append(Document.status.in_(statuses))
    if type_filter is not None:
        title_patterns, mime_patterns = DOCUMENT_TYPE_FILTER[type_filter]
        conditions.append(
            or_(
                or_(*(Document.title.ilike(p) for p in title_patterns)),
                or_(*(Document.mime_type.ilike(p) for p in mime_patterns)),
            )
        )
    needle = (q or "").strip()
    if needle:  # ?q= 名称模糊（R17 增量）：通配符字面化后整词包裹 %（防 LIKE 注入）
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(Document.title.ilike(f"%{escaped}%", escape="\\"))

    total = (await session.execute(select(func.count()).select_from(Document).where(*conditions))).scalar_one()
    docs = (
        (
            await session.execute(
                select(Document).where(*conditions).order_by(Document.created_at.desc()).offset(offset).limit(limit)
            )
        )
        .scalars()
        .all()
    )

    chunk_counts = await _chunk_counts_of(session, [doc.id for doc in docs])
    step_stats = await _step_stats_of(session, principal.tenant_id, [doc.id for doc in docs])
    items = [_document_item_of(doc, chunk_counts, step_stats) for doc in docs]
    return DocumentListEnvelope(data=DocumentListData(items=items, total=total, offset=offset, limit=limit))


async def _chunk_counts_of(session: AsyncSession, doc_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
    """分片计数投影（列表/详情共用一条 SQL；detail 传单元素列表）。"""
    if not doc_ids:
        return {}
    return {
        doc_id: count
        for doc_id, count in (
            await session.execute(
                select(DocumentChunk.document_id, func.count())
                .where(DocumentChunk.document_id.in_(doc_ids))
                .group_by(DocumentChunk.document_id)
            )
        ).all()
    }


async def _step_stats_of(
    session: AsyncSession, tenant_id: uuid.UUID, doc_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, str | None]]:
    """流水线步投影（done 步数 + 首个 failed 错误摘录；列表/详情共用）。"""
    stats: dict[uuid.UUID, tuple[int, str | None]] = {}
    if not doc_ids:
        return stats
    for row in (
        await session.execute(
            select(KbPipelineStep.document_id, KbPipelineStep.status, KbPipelineStep.error)
            .where(KbPipelineStep.tenant_id == tenant_id, KbPipelineStep.document_id.in_(doc_ids))
            .order_by(KbPipelineStep.created_at)
        )
    ).all():
        done, err = stats.get(row.document_id, (0, None))
        if row.status == "done":
            done += 1
        elif row.status == "failed" and err is None:
            err = (row.error or "流水线步骤失败")[:200]
        stats[row.document_id] = (done, err)
    return stats


def _document_item_of(
    doc: Document,
    chunk_counts: dict[uuid.UUID, int],
    step_stats: dict[uuid.UUID, tuple[int, str | None]],
) -> DocumentListItem:
    """documents 行 → 前端 KbDocument 行 DTO（列表/详情共用投影，R51/R17 同源）。"""
    total_steps = len(M2_FULL_STEPS)
    done, err = step_stats.get(doc.id, (0, None))
    return DocumentListItem(
        id=doc.id,
        name=doc.title,
        doc_type=doc_type_of(doc.title, doc.mime_type),
        size=doc.size_bytes or 0,
        size_bytes=doc.size_bytes,
        chunk_count=chunk_counts.get(doc.id, 0),
        status=ui_status_of(doc.status),
        progress=100 if doc.status == "indexed" else int(round(100 * done / total_steps)),
        pipeline=DocumentPipelineProgress(step=done, total=total_steps),
        error=err if doc.status == "failed" else None,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        indexed_today=(doc.status == "indexed" and doc.updated_at.date() == datetime.now(UTC).date()),
    )


@router.get("/documents/{document_id}", summary="文档详情（R17-a live 对账补齐：列表 DTO 全字段 + chunk 计数/error）")
async def get_document(document_id: uuid.UUID, principal: KbReadDep, session: SessionDep) -> DocumentDetailEnvelope:
    """单文档详情（api/01 §5 kb 行 GET /kb/documents/{id}）：复用列表投影 + 定位字段。

    404 = 文档域既有口径（_load_document：code 404「文档不存在」，api/01 §5 登记的 404*）；
    已墓碑（DELETE 后 valid_to 封口）文档同样 404 不可见（墓碑口径：删除后详情不可见）。
    """
    doc = await _load_document(session, principal.tenant_id, document_id)
    chunk_counts = await _chunk_counts_of(session, [document_id])
    step_stats = await _step_stats_of(session, principal.tenant_id, [document_id])
    item = _document_item_of(doc, chunk_counts, step_stats)
    return DocumentDetailEnvelope(
        data=DocumentDetailData(**item.model_dump(), collection_id=doc.kb_collection_id, mime_type=doc.mime_type)
    )


@router.delete("/documents/{document_id}", summary="删除文档（墓碑式软删：valid_to 封口下线检索；幂等恒 200）")
async def delete_document(document_id: uuid.UUID, principal: KbWriteDep, session: SessionDep) -> DocumentDeleteEnvelope:
    """墓碑式软删（api/01 §5.15 DELETE 行「不物理删除」=平台底线；database/01 §3.3 documents
    双时间线「失效=封口不删除」）。

    口径（B6 实装裁决）：
    - 墓碑 = documents.valid_to = now()（status 八态 CHECK 无 deleted 态可置；bm25/vector/graph
      三路检索 SQL 均内建 d.valid_to IS NULL 谓词——封口即全线下线检索，契约「已索引内容下线」）；
    - chunks/kb_facts/kb_pipeline_step 物理保留（kb_facts 与审计不删——复核与追溯依据）；
      删除后详情/分片/候选/流水线/重试经 _load_document（valid_to 过滤）一律 404 不可见；
    - 同内容可重传：checksum 唯一性=部分唯一索引（uk_documents_tenant_id_kb_collection_id_
      checksum_sha256，WHERE valid_to IS NULL，迁移 partial-unique 改建）——墓碑行不占唯一性，
      重传=全新插入（database/01 documents 段契约同批登记）；
    - 幂等：重复删除与不存在对调用方等价 → 200 deleted=false（不 404；他人租户同口径 deny-by-default）；
    - 契约行成功码 204 与 live 对账（R17-b）200 强信封解包并存：按 live 口径保留 200+envelope，
      deleted 标志区分命中/幂等未命中（偏离已在报告登记）。
    """
    doc = (
        await session.execute(
            select(Document).where(Document.id == document_id, Document.tenant_id == principal.tenant_id)
        )
    ).scalar_one_or_none()
    if doc is None or doc.valid_to is not None:  # 不存在 / 已墓碑 → 等价幂等未命中
        return DocumentDeleteEnvelope(data=DocumentDeleteData(deleted=False))
    chunk_count = (
        await session.execute(
            select(func.count())
            .select_from(DocumentChunk)
            .where(
                DocumentChunk.document_id == document_id,
                DocumentChunk.valid_to.is_(None),  # 只计当前有效分片（墓碑 chunk 不算下线数，ocr 评审 low）
            )
        )
    ).scalar_one()
    doc.valid_to = datetime.now(UTC)  # 封口即下线（检索三路 d.valid_to IS NULL 谓词即时生效）
    await session.commit()
    return DocumentDeleteEnvelope(
        data=DocumentDeleteData(deleted=True, cascade=DocumentDeleteCascade(chunks=int(chunk_count)))
    )


@router.get("/documents/{document_id}/chunks", summary="分片列表（seq 升序；offset/limit 分页 + meta.total）")
async def list_document_chunks(
    document_id: uuid.UUID,
    principal: KbReadDep,
    session: SessionDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> KbChunkPageOut:
    """分片预览（api/01 §5.4 ★ 行，FR-KB-03）：仅当前有效分片（chunk.valid_to IS NULL），seq 升序。

    content=原文预览截断（≤500 字符；全文走文档原文）；span=命中高亮偏移（meta.span，指向原文
    [start, end)）；has_embedding=布尔（向量本体不回传；pgvector 列缺失恒 False=BM25-only 降级）。
    文档不存在/已墓碑 → 404（_load_document 统一口径）。
    """
    await _load_document(session, principal.tenant_id, document_id)  # 404 前置校验（含墓碑）
    conds = [
        DocumentChunk.tenant_id == principal.tenant_id,
        DocumentChunk.document_id == document_id,
        DocumentChunk.valid_to.is_(None),
    ]
    total = (await session.execute(select(func.count()).select_from(DocumentChunk).where(*conds))).scalar_one()
    rows = (
        (
            await session.execute(
                select(DocumentChunk).where(*conds).order_by(DocumentChunk.seq).offset(offset).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    embedded = await _embedded_chunk_ids(session, [row.id for row in rows])
    items = []
    for row in rows:
        meta = row.meta if isinstance(row.meta, dict) else {}
        span = meta.get("span")
        items.append(
            KbChunkOut(
                id=row.id,
                document_id=row.document_id,
                seq=row.seq,
                content=row.content[:_CHUNK_PREVIEW_CHARS],
                token_count=row.token_count,
                page_no=row.page_no,
                span=[int(v) for v in span] if isinstance(span, list) else None,
                has_embedding=row.id in embedded,
                created_at=row.created_at,
            )
        )
    return KbChunkPageOut(items=items, meta=KbChunkPageMetaOut(offset=offset, limit=limit, total=int(total)))


@router.post(
    "/documents/{document_id}/pipeline/retry",
    status_code=status.HTTP_202_ACCEPTED,
    summary="失败文档流水线重试（202 受理 + 后台断点续跑；非 failed 态 409）",
)
async def retry_pipeline(
    document_id: uuid.UUID,
    principal: KbWriteDep,
    request: Request,
    background: BackgroundTasks,
    session: SessionDep,
    model: ModelPortDep,
) -> PipelineStartOut:
    """api/01 §5.4 retry 行（202 受理 / 409*）：对 failed 文档受理一次 run_pipeline。

    断点续跑语义（kb_pipeline.run_pipeline）：受理即返回，从失败步骤续跑（done 步跳过、
    attempt 跨运行累计 ≤3、checkpoint 幂等）。
    并发防呆（ocr 评审 medium）：document 行 with_for_update 锁内复核 status 并**预复位
    failed→preprocessed（状态机唯一入口）先行提交**——并发第二调用在锁上排队、进锁后见
    非 failed 态 → 409，重试不双跑（复位后 run_pipeline 内的复位断言为幂等 no-op）。
    防呆：非 failed 态（进行中/已入库/uploaded 等）→ 409；文档不存在或已墓碑 → 404。
    """
    doc = (
        await session.execute(  # 含墓碑过滤的行锁装载（不复用 _load_document：本处需 FOR UPDATE）
            select(Document)
            .where(
                Document.id == document_id,
                Document.tenant_id == principal.tenant_id,
                Document.valid_to.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if doc is None:
        raise GatewayError(404, "文档不存在", status_code=404)
    if doc.status != "failed":
        raise GatewayError(409, f"文档非 failed 态（当前 {doc.status}），仅失败文档可重试", status_code=409)
    assert_document_transition("failed", "preprocessed")  # 状态机唯一入口（禁直改 status）
    doc.status = "preprocessed"
    await session.commit()  # 锁内预复位先行持久 → 并发重试在锁上串行化（不双跑）
    background.add_task(_pipeline_task, request.app, principal.tenant_id, document_id, model)
    return PipelineStartOut(document_id=document_id, accepted=True)


_CHUNK_PREVIEW_CHARS = 500  # 分片预览截断长度（FR-KB-03 预览语义；全文走文档原文）


async def _embedded_chunk_ids(session: AsyncSession, chunk_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """已向量化分片 id 集（pgvector 列缺失 → 空集=has_embedding 恒 False，降级契约同源）；
    按当前页 chunk ids 过滤（ocr 评审 low：不整文档扫描）。"""
    if not chunk_ids or not await vector_ready(session):
        return set()
    rows = await session.execute(
        text("SELECT id FROM document_chunks WHERE id = ANY(CAST(:ids AS uuid[])) AND embedding IS NOT NULL"),
        {"ids": list(chunk_ids)},
    )
    return {row[0] for row in rows}


@router.post(
    "/documents/{document_id}/pipeline/start",
    status_code=status.HTTP_202_ACCEPTED,
    summary="启动/续跑流水线（后台执行，202 受理）",
)
async def start_pipeline(
    document_id: uuid.UUID,
    principal: KbWriteDep,
    request: Request,
    background: BackgroundTasks,
    session: SessionDep,
    model: ModelPortDep,
) -> PipelineStartOut:
    """受理即返回；进度经 GET pipeline 轮询。断点续跑：done 步跳过、failed 步重试 ≤3。

    M2 full 步序（preprocess → chunk → embed → extract → align → validate → bm25_index）：
    extract/align/validate 为 M2.5 三步执行器，model=组合根装配的 ModelPort（依赖取用），
    review=候选审核端口（app.state.candidate_review）；无 LLM 配置时 extract 步 5002 失败可重跑。
    """
    await _load_document(session, principal.tenant_id, document_id)  # 404 前置校验
    background.add_task(_pipeline_task, request.app, principal.tenant_id, document_id, model)
    return PipelineStartOut(document_id=document_id, accepted=True)


async def _pipeline_task(app: FastAPI, tenant_id: uuid.UUID, document_id: uuid.UUID, model: ModelPort | None) -> None:
    """后台流水线任务：独立会话工厂（不持请求事务）；异常仅记日志（进度看 checkpoint）。"""
    settings = app.state.settings
    try:
        report = await run_pipeline(
            get_session_factory(settings),  # type: ignore[arg-type]
            tenant_id=tenant_id,
            document_id=document_id,
            embedder=_embedder(app.state),
            model=model,
            review=getattr(app.state, "candidate_review", None),
            steps=M2_FULL_STEPS,
        )
        logger.info(
            "kb_pipeline finished: document_id=%s status=%s degraded=%s steps=%s",
            document_id,
            report.document_status,
            report.degraded,
            [(s.step, s.status, s.attempt) for s in report.steps],
        )
    except PipelineError as exc:
        logger.warning("kb_pipeline rejected: document_id=%s error=%s", document_id, exc)
    except Exception:  # noqa: BLE001 — 后台任务兜底，避免静默丢栈
        logger.exception("kb_pipeline crashed: document_id=%s", document_id)


@router.get("/documents/{document_id}/pipeline", summary="流水线进度（八态 + 步级 checkpoint）")
async def pipeline_progress(document_id: uuid.UUID, principal: KbReadDep, session: SessionDep) -> PipelineProgressOut:
    doc = await _load_document(session, principal.tenant_id, document_id)
    rows = (
        (
            await session.execute(
                select(KbPipelineStep)
                .where(
                    KbPipelineStep.tenant_id == principal.tenant_id,
                    KbPipelineStep.document_id == document_id,
                )
                .order_by(KbPipelineStep.created_at)
            )
        )
        .scalars()
        .all()
    )
    meta = doc.meta or {}
    return PipelineProgressOut(
        document_id=document_id,
        document_status=doc.status,
        degraded="embed" in (meta.get("degraded") or []),
        steps=[
            PipelineStepOut(
                step=row.step,
                status=row.status,
                attempt=row.attempt,
                error=row.error,
                started_at=row.started_at,
                finished_at=row.finished_at,
            )
            for row in rows
        ],
    )


async def _class_hierarchy(
    request: Request, session: AsyncSession, tenant_id: uuid.UUID, ontology_version: str | None = None
) -> ClassHierarchy:
    """类层次只读视图（进程内按租户缓存 TTL）：ontology 读模型公开查询 → kb 侧双向邻接表。

    无已发布本体读模型时返回空层次——图路闭包退化为类自身，同类扩展照常可用（graph.py）。
    """
    state = request.app.state
    cache: dict[str, tuple[float, ClassHierarchy]] = getattr(state, "_kb_hierarchy_cache", None)
    if cache is None:
        cache = {}
        state._kb_hierarchy_cache = cache  # type: ignore[attr-defined]
    key = f"{tenant_id}|{ontology_version or ''}"
    now = time.monotonic()
    cached = cache.get(key)
    if cached is not None and now - cached[0] < _HIERARCHY_TTL_SECONDS:
        return cached[1]
    rows = await get_class_hierarchy(session, tenant_id=tenant_id)
    hierarchy = build_class_hierarchy((row.iri, row.name, row.subclass_of) for row in rows)
    cache[key] = (now, hierarchy)
    return hierarchy


@router.post(
    "/search",
    summary="knowledge.search lite（bm25+向量+图三路 RRF；图=LazyGraphRAG lite 查询时扩展）",
)
async def search(body: KbSearchIn, principal: KbReadDep, request: Request, session: SessionDep) -> KbSearchOut:
    """OntRAG §5 契约 REST 子集全量：citations 全字段 / evidence.graph_paths / answers+confidence / usage。

    agentic=true 走 A0 服务端代跑管线（AgenticRAG优化方案 §3/§8.1，v1 纯规则档零 LLM）：
    判别（寒暄 skip → hits/citations 空数组 + decision 徽标）→ 三路召回 → 规则评级 →
    术语归一改写纠错（≤max_rounds 轮，改写依据进 trace）→ 仍失败 degraded="agentic_exhausted"；
    全程 trace 进响应 agentic 块（false 时恒 None=存量消费方零影响）。
    降级：嵌入路不可用 → BM25-only（degraded=true, reason=vector_unavailable）；
    mode=global/drift 无社区摘要索引 → local 降级（degraded=true, reason=mode_downgraded:*，二期）；
    图路无已发布本体读模型 → 层次闭包退化为类自身，同类扩展照常（非降级）。
    ACL：X-Acl-Tags 头 + OA_KB_ACL_FILTER_ENABLED 开关双条件激活 → 三路同源谓词下推（§4.3）；
    无头 = no-op（调用方未接入标签面，零行为变化红线）。
    """
    started = time.perf_counter()
    embedder = _embedder(request.app.state)
    hierarchy = await _class_hierarchy(request, session, principal.tenant_id, body.ontology_version)
    acl = await AclPushdown.prepare(
        session,
        enabled=request.app.state.settings.kb_acl_filter_enabled,
        allowed_tags=_acl_tags_from_request(request),
    )

    async def bm25_fn(query: str, top_k: int) -> list[SearchHit]:
        rows = await bm25_search(
            session,
            tenant_id=principal.tenant_id,
            query=query,
            top_k=top_k,
            collection_id=body.kb_id,
            as_of=body.as_of,
            include_superseded=body.include_superseded,
            acl=acl,
        )
        return [_dict_to_hit(r) for r in rows]

    async def vector_fn(query: str, top_k: int) -> list[SearchHit]:
        embeddings = await embedder.embed([query])
        rows = await vector_search(
            session,
            tenant_id=principal.tenant_id,
            query_embedding=embeddings[0],
            top_k=top_k,
            collection_id=body.kb_id,
            as_of=body.as_of,
            include_superseded=body.include_superseded,
            acl=acl,
        )
        return [_dict_to_hit(r) for r in rows]

    async def graph_fn(seeds: Sequence[SearchHit]) -> GraphExpansion:
        # 图路锚定扩展（LazyGraphRAG lite，retrieval/graph.py）：KbFact 类 IRI 邻接 + 本体层次闭包
        return await expand_graph(
            session,
            tenant_id=principal.tenant_id,
            collection_id=body.kb_id,
            seeds=seeds,
            hierarchy=hierarchy,
            max_hops=body.max_hops,
            entity_type_filter=body.entity_type_filter,
            as_of=body.as_of,
            include_superseded=body.include_superseded,
            acl=acl,
        )

    if body.agentic:
        # A0 服务端代跑（AgenticRAG优化方案 §3/§8.1）：编排委托 business/agentic——decide 判别
        # （寒暄 skip 时下方回调永不触发=零召回）→ 每轮走同一套三路召回闭包 → 规则评级 →
        # 术语归一改写纠错（≤body.max_rounds 轮）→ 仍失败 degraded="agentic_exhausted"。
        async def agentic_once(round_query: str) -> list[SearchHit]:
            inner = await hybrid_search(
                round_query,
                bm25=bm25_fn,
                vector=vector_fn,
                graph=graph_fn if body.with_evidence else None,
                top_k=body.top_k,
                mode=body.mode,
                entity_type_filter=body.entity_type_filter,
            )
            return inner.hits

        hits, trace = await run_agentic_search(body.query, agentic_once, max_rounds=body.max_rounds)
        mode_used, mode_reason = resolve_mode(body.mode)  # lite 路由口径与 hybrid_search 内部一致
        degraded = mode_reason is not None
        degraded_reasons = [mode_reason] if mode_reason is not None else []
        channels = sorted({channel for hit in hits for channel in hit.channels})
        evidence = KbEvidenceOut(graph_paths=[])  # v1 边界：agentic 管线不回图路证据（trace 为解释面）
        answers: list[KbAnswerOut] = []  # v1 边界：抽取式摘要随完整档接入，不因纠错轮拼装误导
        result_query = body.query
        result_mode = body.mode
    else:
        result = await hybrid_search(
            body.query,
            bm25=bm25_fn,
            vector=vector_fn,
            graph=graph_fn if body.with_evidence else None,
            top_k=body.top_k,
            mode=body.mode,
            entity_type_filter=body.entity_type_filter,
        )
        hits = result.hits
        mode_used = result.mode_used
        degraded = result.degraded
        degraded_reasons = list(result.degraded_reasons)
        channels = list(result.channels)
        evidence = KbEvidenceOut(graph_paths=[_path_to_out(path) for path in result.graph_paths])
        answers = [_answer_to_out(result.answer)] if result.answer is not None else []
        result_query = result.query
        result_mode = result.mode
    # source_context 软路由（多源接入 §5.2 v1，service 层共用助手）：None=原序零开销零 SQL；
    # 非空=按文档 meta.source_system 加权重排（不剔除）。
    final_hits = await rerank_hits_by_source_context(session, hits, source_context=body.source_context)
    latency_ms = int((time.perf_counter() - started) * 1000)
    return KbSearchOut(
        query=result_query,
        mode=result_mode,
        mode_used=mode_used,
        degraded=degraded,
        degraded_reasons=degraded_reasons,
        channels=channels,
        latency_ms=latency_ms,
        hits=[_hit_to_out(hit) for hit in final_hits],
        citations=[_hit_to_citation(hit) for hit in final_hits],
        evidence=evidence,
        answers=answers,
        usage=KbUsageOut(latency_ms=latency_ms),
        agentic=KbAgenticTraceOut.model_validate(trace.model_dump()) if body.agentic else None,
    )


def _dict_to_hit(row: dict) -> SearchHit:
    return SearchHit(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        content=row["content"],
        score=row["score"],
        doc_name=row["doc_name"],
        minio_key=row.get("minio_key"),
        span=row.get("span"),
    )


def _hit_to_out(hit: SearchHit) -> KbHitOut:
    return KbHitOut(
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        content=hit.content,
        score=hit.score,
        doc_name=hit.doc_name,
        minio_key=hit.minio_key,
        span=hit.span,
        channels=hit.channels,
    )


def _hit_to_citation(hit: SearchHit) -> KbCitationOut:
    """citations 投影（§5 全字段）：quote=chunk 原文截片（chunk content 即原文 [span) 切片）。"""
    return KbCitationOut(
        chunk_id=hit.chunk_id,
        doc_id=hit.document_id,
        doc_name=hit.doc_name,
        minio_key=hit.minio_key,
        quote=hit.content,
        span=hit.span,
        score=hit.score,
    )


def _path_to_out(path: GraphPath) -> KbGraphPathOut:
    return KbGraphPathOut(
        nodes=[KbGraphNodeOut(iri=node.iri, name=node.name, type=node.type) for node in path.nodes],
        rels=[KbGraphRelOut(type=rel.type, weight=rel.weight) for rel in path.rels],
        chunk_ids=path.chunk_ids,
    )


def _answer_to_out(answer: ExtractiveAnswer) -> KbAnswerOut:
    return KbAnswerOut(
        summary=answer.summary,
        confidence=answer.confidence,
        sentences=[
            KbAnswerSentenceOut(text=sentence.text, citations=sentence.citations) for sentence in answer.sentences
        ],
        citations=answer.citations,
    )


# ---------------------------------------------------------------- 图三查（api/01 §5.4 ★ GET /kb/graph/*，B6 实装）
#
# lite=类级图（retrieval/graph.py 纯函数，与 /kb/search 图路共用类层次读模型）：
# 节点=本体类（ontology 读模型公开查询，_class_hierarchy 进程内 TTL 缓存共用）；
# 边=subclass_of（层次）+ 权威关系谓词（kb_facts status=authoritative，候选不入图=宪法第 3 条；
# 墓碑文档 valid_to 封口的关系边一并下线——删除后图查询不可见）。
# 空结果非失败（OntRAG §4）：未知类 IRI / 无路径 → 200 空 nodes/rels，404 仅用于资源不存在口径。


def _graph_query_out(result: GraphQueryResult) -> KbGraphQueryOut:
    return KbGraphQueryOut(
        nodes=[KbGraphNodeOut(iri=node.iri, name=node.name, type=node.type) for node in result.nodes],
        rels=[KbGraphRelOut(type=rel.type, weight=rel.weight) for rel in result.rels],
    )


@router.get("/graph/search", summary="图谱实体搜索（q/class_name 匹配类 + 层次 depth 跳 + 权威关系扩展）")
async def search_graph_entities(
    principal: KbReadDep,
    request: Request,
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=256)] = None,
    class_name: Annotated[str | None, Query(max_length=256)] = None,  # 任务面别名：与 q 同义（类名/IRI 片段）
    depth: Annotated[int, Query(ge=0, le=3)] = 1,
    top_k: Annotated[int, Query(ge=1, le=50)] = 10,
) -> KbGraphQueryOut:
    """图检索（api/01 §5.4 ★ 行，`q=` 关键词/IRI 片段）：类名/IRI/本地名片段匹配 → 类层次双向
    BFS depth 跳扩展 + 权威关系边扩展（对端类并入节点集，类层次+关系扩展语义）。

    q 与 class_name 至少一个（缺失 → 3001/422）；无匹配类 → 200 空结果（OntRAG §4 非失败）。
    """
    needle = (q or class_name or "").strip()
    if not needle:
        raise GatewayError(ErrorCode.PARAM_INVALID, "q 与 class_name 至少提供一个", status_code=422)
    hierarchy = await _class_hierarchy(request, session, principal.tenant_id)
    relations = await authoritative_relations(session, tenant_id=principal.tenant_id)
    return _graph_query_out(graph_search(hierarchy, needle, depth=depth, top_k=top_k, relations=relations))


@router.get("/graph/neighborhood", summary="实体邻域展开（类 IRI depth≤2 层次近邻 + 权威关系边，谓词可过滤）")
async def expand_neighborhood(
    principal: KbReadDep,
    request: Request,
    session: SessionDep,
    class_iri: Annotated[str | None, Query(max_length=512)] = None,
    entity_id: Annotated[str | None, Query(max_length=512)] = None,  # 契约行参数名（与 class_iri 同义别名）
    depth: Annotated[int, Query(ge=0, le=2)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    relation_type: Annotated[str | None, Query(max_length=128)] = None,  # 关系谓词精确过滤（契约行）
) -> KbGraphQueryOut:
    """邻域查询（api/01 §5.4 ★ 行）：从类 IRI 出发层次近邻 depth 跳（lite 语义一跳直达，上限 2）
    + 触及该类的权威关系边（relation_type 可过滤，对端类入节点集）。

    class_iri（或契约别名 entity_id）必填（缺失 → 3001/422）；未知类 IRI → 200 空结果
    （OntRAG §4 空结果非失败，404 仅用于资源不存在口径）。
    """
    iri = (class_iri or entity_id or "").strip()
    if not iri:
        raise GatewayError(ErrorCode.PARAM_INVALID, "class_iri（或契约别名 entity_id）必填", status_code=422)
    hierarchy = await _class_hierarchy(request, session, principal.tenant_id)
    relations = await authoritative_relations(session, tenant_id=principal.tenant_id)
    result = graph_neighborhood(
        hierarchy, iri, depth=depth, limit=limit, relations=relations, relation_type=relation_type
    )
    return _graph_query_out(result)


@router.get("/graph/path", summary="两实体间路径查询（类层次图内 BFS 最短路；max_hops 限深）")
async def find_graph_path(
    principal: KbReadDep,
    request: Request,
    session: SessionDep,
    from_class_iri: Annotated[str | None, Query(max_length=512)] = None,
    to_class_iri: Annotated[str | None, Query(max_length=512)] = None,
    source: Annotated[str | None, Query(max_length=512)] = None,  # 契约行参数名（与 from_class_iri 同义）
    target: Annotated[str | None, Query(max_length=512)] = None,  # 契约行参数名（与 to_class_iri 同义）
    max_hops: Annotated[int, Query(ge=1, le=8)] = 3,
) -> KbGraphQueryOut:
    """路径查询（api/01 §5.4 ★ 行）：类层次图内 BFS 最短路（父子边双向可行走，subclass_of 链
    上下行语义均有效），节点按路径序返回、边=逐跳 subclass_of。

    from_class_iri/to_class_iri（或契约别名 source/target）均必填（缺失 → 3001/422）；
    端点未知 / max_hops 内无路 → 200 空结果（OntRAG §4 空结果非失败）。
    """
    src = (from_class_iri or source or "").strip()
    dst = (to_class_iri or target or "").strip()
    if not src or not dst:
        raise GatewayError(
            ErrorCode.PARAM_INVALID,
            "from_class_iri/to_class_iri（或契约别名 source/target）均必填",
            status_code=422,
        )
    hierarchy = await _class_hierarchy(request, session, principal.tenant_id)
    path = graph_shortest_path(hierarchy, src, dst, max_hops=max_hops)
    if path is None:
        return KbGraphQueryOut()
    return KbGraphQueryOut(
        nodes=[KbGraphNodeOut(iri=node.iri, name=node.name, type=node.type) for node in path.nodes],
        rels=[KbGraphRelOut(type=rel.type, weight=rel.weight) for rel in path.rels],
    )


# ---------------------------------------------------------------- 终审工作台（api/01 §5.4 ★ 三端点）
#
# B4 逐候选消费面：B2 抽取深化已把 evidence.quote/span 与 violations 写进 kb_facts，列表端点是
# 它们到达前端终审工作台的正式通道；决策端点是全仓唯一 authoritative 写路径（OntRAG §2.7：
# 人工终审=候选生效唯一关口，宪法第 3 条「候选非成品」，review:approve scope 背书）。
#
# 跨模块边界：审核单写路径经 app.state.candidate_review（lifespan 装配的
# ReviewTicketService，即 review.business.candidates——review.data 模块私有契约六的公开面）
# 鸭子类型调用，本文件零 review 静态 import（admin.py _approvals 同款，契约六收口前不新增边）。

_TICKET_TARGET_TYPE = "knowledge_instance"  # extract 双写同款 target（kb_extraction._persist_candidates）
_TICKET_OPEN_STATUSES = ("draft", "pending_review")  # uk_review_one_open 同口径
_TICKET_SETTLED_STATUSES = ("approved", "published")  # 终态单：迟到决策 4701
_BATCH_DECISION_LIMIT = 200  # api/01 §5.4：批量上限 200 条/批，超出 3001
_DECISION_BUCKET = {"accept": "accepted", "reject": "rejected", "edit_accept": "edited"}


def _tickets(request: Request) -> Any:
    """候选审核单服务（app.state.candidate_review=ReviewTicketService；未装配=503 端口检查先例）。"""
    tickets = getattr(request.app.state, "candidate_review", None)
    if tickets is None:
        raise GatewayError(5004, "候选审核服务未装配", status_code=503)
    return tickets


def _candidate_out(row: KbFact) -> ReviewCandidateOut:
    """kb_facts 行 → 工作台列表项（evidence 信封/violations/align 全量透出，裁决依据不裁剪）。"""
    evidence = row.evidence if isinstance(row.evidence, dict) else {}
    meta = row.meta if isinstance(row.meta, dict) else {}
    align = meta.get("align")
    span = evidence.get("span")
    return ReviewCandidateOut(
        id=row.id,
        fact_type=row.fact_type,
        subject=row.subject,
        subject_type=row.subject_type,
        predicate=row.predicate,
        object=row.object,
        object_type=row.object_type,
        canonical_name=row.canonical_name,
        aliases=[str(a) for a in (row.aliases or [])],
        confidence=float(row.confidence),
        status=row.status,
        evidence=ReviewCandidateEvidenceOut(
            source_ref=dict(evidence.get("source_ref") or {}),
            quote=evidence.get("quote"),
            span=[int(v) for v in span] if isinstance(span, list) else None,
        ),
        violations=[dict(v) for v in (row.violations or []) if isinstance(v, dict)],
        align=dict(align) if isinstance(align, dict) else None,
        created_at=row.created_at,
    )


@router.get(
    "/documents/{document_id}/review/candidates",
    summary="终审候选实例列表（分页；quote/violations/align 全量透出）",
)
async def list_review_candidates(
    document_id: uuid.UUID,
    principal: ReviewReadDep,
    session: SessionDep,
    fact_type: Annotated[FactTypeFilter | None, Query()] = None,
    status_filter: Annotated[ReviewStatusFilter | None, Query(alias="status")] = None,  # 缺省=全部三态
    min_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,  # confidence 下界（含）
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ReviewCandidatePageOut:
    """P4 终审队列（api/01 §5.4 ★）：按 document_id（租户过滤）列出 kb_facts 候选。"""
    await _load_document(session, principal.tenant_id, document_id)  # 404 前置校验
    conds = [KbFact.tenant_id == principal.tenant_id, KbFact.document_id == document_id]
    if fact_type is not None:
        conds.append(KbFact.fact_type == fact_type)
    if status_filter is not None:
        conds.append(KbFact.status == status_filter)
    if min_confidence is not None:
        conds.append(KbFact.confidence >= min_confidence)
    total = (await session.execute(select(func.count()).select_from(KbFact).where(*conds))).scalar_one()
    rows = (
        (
            await session.execute(
                select(KbFact)
                .where(*conds)
                # 同事务插入共享 now() 时间戳，id 作稳定游标（分页不重不漏）
                .order_by(KbFact.created_at.desc(), KbFact.id)
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return ReviewCandidatePageOut(
        items=[_candidate_out(row) for row in rows],
        meta=ReviewCandidatePageMetaOut(offset=offset, limit=limit, total=int(total)),
    )


def _apply_decision(fact: KbFact, action: str, edit: Any) -> str:
    """决策语义落 fact（内存态，随请求会话提交）；返回决策后状态。

    - accept → authoritative（OntRAG §2.7 人工终审关口；全仓唯一 authoritative 写路径）；
    - reject → rejected；
    - edit_accept → 按编辑载荷修订（None 字段不覆盖）后保持/复位 candidate（契约「修订后入审」；
      rejected 候选修订后复活重进审）。
    防呆：accept/reject 仅对 candidate 态（对 rejected/authoritative 重复决策 → 409）；
    edit_accept 不接受 authoritative（终审生效态不被工作台静默降级）；
    accept 对缺 predicate 的 relation 事实拒绝（authoritative 关系必须带谓词——图三查关系边
    与同类扩展均以谓词为语义载体，脏候选不放行进图；ocr 评审 high 防呆纵深配套）。
    """
    if action in ("accept", "reject"):
        if fact.status != "candidate":
            raise GatewayError(409, f"候选非 candidate 态（当前 {fact.status}），重复决策拒绝", status_code=409)
        if action == "accept" and getattr(fact, "fact_type", None) == "relation" and not fact.predicate:
            raise GatewayError(
                409, "关系事实缺 predicate，不可 accept（authoritative 关系必须带谓词）", status_code=409
            )
        fact.status = "authoritative" if action == "accept" else "rejected"
        return fact.status
    # edit_accept：candidate 保持入审；rejected 修订复活；authoritative 拒绝降级
    if fact.status == "authoritative":
        raise GatewayError(409, "候选已 authoritative（终审生效态），不接受修订", status_code=409)
    for name, value in edit.provided().items():  # 单一字段源（ocr 评审 low：与 DTO 双源必漂移）
        setattr(fact, name, value)
    fact.status = "candidate"  # 修订后入审（保持或复位）
    return fact.status


async def _precheck_ticket(tickets: Any, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> Any | None:
    """决策前置防呆（在任何 fact 变更前执行，保证失败项零副作用）：

    候选 open 单 → 返回该单（后续落留痕）；无单 / 单已 rejected-cancelled → None（容忍票据缺失，
    主断言在 fact 状态翻转，事实表是终审裁决的权威记录）；单已 approved/published 后的迟到决策
    → 4701（review 段，admin.py _domain_error 同映射：HTTP 409）——终态单不可再动（08 §4）。
    """
    ticket = await tickets.get_latest_ticket(
        tenant_id=tenant_id, target_type=_TICKET_TARGET_TYPE, target_id=candidate_id
    )
    if ticket is not None and ticket["status"] in _TICKET_SETTLED_STATUSES:
        raise GatewayError(
            ErrorCode.OBJECT_ALREADY_IN_REVIEW,
            f"审核单已终态（{ticket['status']}），迟到决策拒绝",
            status_code=409,
        )
    if ticket is None or ticket["status"] not in _TICKET_OPEN_STATUSES:
        return None
    return ticket


async def _append_decision_trail(
    tickets: Any, ticket: Any, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID, action: str, approver_id: uuid.UUID
) -> None:
    """留痕落单：决策记录（决策人/时间/action/candidate_id）追加进 open 单 payload["decisions"]。

    经 ReviewTicketService.merge_payload 整体重赋值（JSONB 不可原地变更纪律同 attach_gate_result）；
    独立短事务——成功即持久，失败上抛（整请求回滚，不产生半程留痕）。
    """
    payload = dict(ticket["payload"] or {})
    decisions = list(payload.get("decisions") or [])
    decisions.append(
        {
            "action": action,
            "candidate_id": str(candidate_id),
            "decided_by": str(approver_id),
            "decided_at": datetime.now(UTC).isoformat(),
        }
    )
    payload["decisions"] = decisions
    await tickets.merge_payload(tenant_id=tenant_id, ticket_id=ticket["id"], payload=payload)


@router.post(
    "/review/candidates/{candidate_id}/decision",
    status_code=status.HTTP_202_ACCEPTED,
    summary="单条终审决策 accept|reject|edit_accept（唯一 authoritative 写路径，review:approve 背书）",
)
async def decide_candidate(
    candidate_id: uuid.UUID,
    body: CandidateDecisionIn,
    principal: ReviewApproveDep,
    request: Request,
    session: SessionDep,
) -> CandidateDecisionOut:
    """宪法第 3 条（候选非成品）：LLM 产物一律人工终审才生效——本端点即该关口的工作台动作面。

    accept 翻转 authoritative（OntRAG §2.7）；决策留痕经 ReviewTicketService 落候选 open 单
    （无单容忍）；fact 不存在 404（kb 段）、非 candidate 态 409、单已终态迟到决策 4701。
    """
    fact = await session.get(KbFact, candidate_id, with_for_update=True)  # 行锁防并发 accept/reject 双写穿透
    if fact is None or fact.tenant_id != principal.tenant_id:
        raise GatewayError(404, "候选事实不存在", status_code=404)
    tickets = _tickets(request)
    ticket = await _precheck_ticket(tickets, tenant_id=principal.tenant_id, candidate_id=candidate_id)
    new_status = _apply_decision(fact, body.action, body.edit)
    await session.commit()  # 事实翻转先持久（ocr 评审 high：留痕先于翻转=中途失败产生幽灵审计）
    trail_recorded = False
    if ticket is not None:  # 留痕后置 best-effort：失败不回滚已生效裁决（宁可缺痕不产生幽灵痕）
        try:
            await _append_decision_trail(
                tickets,
                ticket,
                tenant_id=principal.tenant_id,
                candidate_id=candidate_id,
                action=body.action,
                approver_id=principal.user_id,
            )
            trail_recorded = True
        except Exception:  # noqa: BLE001 留痕失败不阻断裁决返回（审计缺口走日志告警）
            logger.warning("decision trail persist failed: candidate_id=%s", candidate_id, exc_info=True)
    return CandidateDecisionOut(
        candidate_id=candidate_id, action=body.action, status=new_status, trail_recorded=trail_recorded
    )


@router.post(
    "/documents/{document_id}/review/batch-decision",
    status_code=status.HTTP_202_ACCEPTED,
    summary="批量终审决策（≤200 条/批，超出 3001；逐条独立执行，不整批回滚）",
)
async def batch_decide_candidates(
    document_id: uuid.UUID,
    body: BatchDecisionIn,
    principal: ReviewApproveDep,
    request: Request,
    session: SessionDep,
) -> BatchCandidateDecisionOut:
    """逐条独立执行（单条失败仅记 error 不中断批次），响应 meta 汇总 accepted/rejected/edited/failed。"""
    await _load_document(session, principal.tenant_id, document_id)  # 404 前置校验
    if len(body.decisions) > _BATCH_DECISION_LIMIT:
        raise GatewayError(ErrorCode.PARAM_INVALID, f"批量决策上限 {_BATCH_DECISION_LIMIT} 条/批", status_code=422)
    tickets = _tickets(request)
    results: list[BatchCandidateDecisionItemOut] = []
    counts = {"accepted": 0, "rejected": 0, "edited": 0, "failed": 0}
    for item in body.decisions:
        try:
            fact = await session.get(KbFact, item.candidate_id, with_for_update=True)
            if fact is None or fact.tenant_id != principal.tenant_id:
                raise GatewayError(404, "候选事实不存在", status_code=404)
            if fact.document_id != document_id:  # 路径文档绑定（ocr 评审 medium：防跨文档代决策）
                raise GatewayError(404, "候选不属于该文档", status_code=404)
            # 前置防呆（4701）先于任何变更 → 失败项零副作用（部分成功语义不失真）
            ticket = await _precheck_ticket(tickets, tenant_id=principal.tenant_id, candidate_id=item.candidate_id)
            new_status = _apply_decision(fact, item.action, item.edit)
            await session.commit()  # 逐条独立持久（ocr 评审 medium：一条 DB 异常不回滚整批已 ok 项）
            if ticket is not None:  # 留痕后置 best-effort（同单条口径：不产生幽灵审计）
                try:
                    await _append_decision_trail(
                        tickets,
                        ticket,
                        tenant_id=principal.tenant_id,
                        candidate_id=item.candidate_id,
                        action=item.action,
                        approver_id=principal.user_id,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("decision trail persist failed: candidate_id=%s", item.candidate_id, exc_info=True)
            results.append(BatchCandidateDecisionItemOut(candidate_id=item.candidate_id, ok=True, status=new_status))
            counts[_DECISION_BUCKET[item.action]] += 1
        except GatewayError as exc:  # 逐条隔离：业务失败（404/409/4701/…）只记该条结果
            results.append(
                BatchCandidateDecisionItemOut(
                    candidate_id=item.candidate_id, ok=False, error=f"{exc.code} {exc.message}"
                )
            )
            counts["failed"] += 1
        except Exception as exc:  # noqa: BLE001 逐条隔离兜底（LookupError/DB 异常同款记失败，不 500 整批）
            results.append(BatchCandidateDecisionItemOut(candidate_id=item.candidate_id, ok=False, error=f"500 {exc}"))
            counts["failed"] += 1
    return BatchCandidateDecisionOut(results=results, meta=BatchCandidateDecisionMetaOut(**counts))


# ---------------------------------------------------------------- needs_review 聚合复核（§7.1 批次纪律）
#
# 主文档 §11 待办「needs_review 聚合复核的交互设计（按 subject/主题分组复核）」：单部标准
# 换版可产生数千条 needs_review 候选（§8.1 b 类承接判定），逐条复核不可运行——本组端点按
# subject 分组聚合展示、复核人按组全量裁决（review:approve 权限面，与单条终审同源）。
# 状态词汇：服务缺省过滤 needs_review（§8.1 b 类目标态；现行 v1 candidate 队列经服务参数
# 显式传入复用，落库侧词汇扩展随 §11 待办另切片）。
# 装配缝：app.state.kb_review_queue 优先（组合根显式装配/测试替换缝），缺省即席构造并挂
# app.state 复用（服务无状态：会话工厂 + 可选单据服务端口，留痕复用结论见服务模块头）。


def _review_queue_service(request: Request) -> ReviewQueueService:
    svc = getattr(request.app.state, "kb_review_queue", None)
    if svc is None:
        svc = ReviewQueueService(
            get_session_factory(request.app.state.settings),  # type: ignore[arg-type]
            tickets=getattr(request.app.state, "candidate_review", None),
        )
        request.app.state.kb_review_queue = svc  # 即席装配进程内复用（无状态对象，幂等）
    return svc


@router.get("/review-queue", summary="needs_review 聚合复核队列（按 subject 分组：计数/最旧/样本 ≤3）")
async def review_queue_summary(principal: ReviewReadDep, request: Request) -> ReviewQueueSummaryOut:
    """§7.1 批次纪律聚合面：每组 subject/subject_type/计数/最旧 created_at/样本引用 ≤3 条。

    空队列返回 groups=[]（合法态）；样本取组内最旧 3 条（复核人先看最早堆积的候选）。
    """
    groups = await _review_queue_service(request).queue_summary(principal.tenant_id)
    return ReviewQueueSummaryOut(
        groups=[
            ReviewQueueGroupOut(
                subject=group.subject,
                subject_type=group.subject_type,
                count=group.count,
                oldest_created_at=group.oldest_created_at,
                samples=[
                    ReviewQueueSampleOut(fact_id=s.fact_id, document_id=s.document_id, chunk_id=s.chunk_id)
                    for s in group.samples
                ],
            )
            for group in groups
        ],
        total=sum(group.count for group in groups),
    )


@router.post("/review-queue/batch-decide", summary="按 subject 全组批量裁决（authoritative/rejected，逐行留痕）")
async def review_queue_batch_decide(
    body: ReviewQueueBatchDecideIn,
    principal: ReviewApproveDep,
    request: Request,
) -> ReviewQueueBatchDecideOut:
    """同 subject 全组裁决（跨文档聚拢，组级原子：要么全裁要么全不裁）；返回裁决行数。

    留痕逐行：open 单 payload["decisions"] 复用 ReviewTicketService（best-effort）+ 行内
    meta["review_queue"] 审计恒写（无单也有痕）；非法 decision 由 DTO Literal 先行 422；
    空组幂等返回 decided=0（行数即真值，不 404）。
    """
    outcome = await _review_queue_service(request).batch_decide(
        principal.tenant_id,
        subject=body.subject,
        decision=body.decision,
        reviewer_id=principal.user_id,
        comment=body.comment,
    )
    return ReviewQueueBatchDecideOut(
        subject=outcome.subject,
        decision=outcome.decision,
        decided=outcome.decided,
        trail_recorded=outcome.trail_recorded,
    )

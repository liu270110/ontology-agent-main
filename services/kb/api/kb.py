"""L2 知识库（kb）路由：文档上传 → 流水线 → knowledge.search lite（M2.5）。

端点（api/01 登记册 kb 行；OntRAG §5 检索契约 REST 子集）：
    POST /kb/collections                      建库
    GET  /kb/documents                        文档列表（R51 联调补齐：信封 + 前端
                                              KbDocument DTO；status/type/q 可选过滤 +
                                              offset/limit 分页，limit 缺省 50 上限 200）
    GET  /kb/documents/{id}                   文档详情（R17-a live 对账补齐：列表 DTO 全字段
                                              + chunk 计数/error；404=文档域 404* 同款错误体）
    DELETE /kb/documents/{id}                 删除文档（R17-b live 对账补齐：硬删 + 应用层级联
                                              facts/steps/chunks——DB FK 无 ON DELETE CASCADE；
                                              幂等，不存在亦 200 deleted=false）
    POST /kb/documents                        JSON 内容直传（MinIO 随 M3；checksum 幂等）
    POST /kb/documents/{id}/pipeline/start    后台流水线（202 受理；M2 lite 四步 / M2 full
                                              七步中段 extract/align/validate 已插回）
    GET  /kb/documents/{id}/pipeline          进度（八态 + 步级 checkpoint）
    POST /kb/search                           knowledge.search lite：bm25+向量+图三路 RRF
                                              （图=LazyGraphRAG lite 查询时扩展，retrieval/graph.py）
    GET  /kb/documents/{id}/review/candidates     终审候选列表（api/01 §5.4 ★，quote/violations 透出）
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
from sqlalchemy import delete, func, or_, select
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
    KbAnswerOut,
    KbAnswerSentenceOut,
    KbCitationOut,
    KbDocType,
    KbEvidenceOut,
    KbGraphNodeOut,
    KbGraphPathOut,
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
from services.kb.business.kb_pipeline import M2_FULL_STEPS, PipelineError, run_pipeline
from services.kb.business.review_queue import ReviewQueueService
from services.kb.business.search_service import rerank_hits_by_source_context
from services.kb.data.orm import Document, DocumentChunk, KbCollection, KbFact, KbPipelineStep
from services.kb.retrieval.embed import AclPushdown, OllamaEmbedder, bm25_search, vector_search
from services.kb.retrieval.graph import ClassHierarchy, build_class_hierarchy, expand_graph
from services.kb.retrieval.retrieve import ExtractiveAnswer, GraphExpansion, GraphPath, SearchHit, hybrid_search
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
    """进程内复用的嵌入客户端（挂 app.state；base_url=config.ollama_base_url）。"""
    cached = getattr(state, "_kb_embedder", None)
    if cached is None:
        cached = OllamaEmbedder(state.settings.ollama_base_url)  # type: ignore[attr-defined]
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
    doc = (
        await session.execute(select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id))
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


@router.post("/documents", summary="上传文档（M2 JSON 内容直传；checksum 幂等）")
async def create_document(body: DocumentCreateIn, principal: KbWriteDep, session: SessionDep) -> DocumentOut:
    """§8.0 同源检测第①级：精确重复拒收并幂等返回既有文档（created=false）。"""
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
    existing = (
        await session.execute(
            select(Document).where(
                Document.tenant_id == principal.tenant_id,
                Document.kb_collection_id == body.collection_id,
                Document.checksum_sha256 == checksum,
            )
        )
    ).scalar_one_or_none()
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
    except IntegrityError as exc:  # 并发重复上传兜底（uk checksum）
        await session.rollback()
        raced = (
            await session.execute(
                select(Document).where(
                    Document.tenant_id == principal.tenant_id,
                    Document.kb_collection_id == body.collection_id,
                    Document.checksum_sha256 == checksum,
                )
            )
        ).scalar_one_or_none()
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

    404 = 文档域既有口径（_load_document：code 404「文档不存在」，api/01 §5 登记的 404*）。
    """
    doc = await _load_document(session, principal.tenant_id, document_id)
    chunk_counts = await _chunk_counts_of(session, [document_id])
    step_stats = await _step_stats_of(session, principal.tenant_id, [document_id])
    item = _document_item_of(doc, chunk_counts, step_stats)
    return DocumentDetailEnvelope(
        data=DocumentDetailData(**item.model_dump(), collection_id=doc.kb_collection_id, mime_type=doc.mime_type)
    )


@router.delete("/documents/{document_id}", summary="删除文档（R17-b：硬删 + 应用层级联；幂等）")
async def delete_document(document_id: uuid.UUID, principal: KbWriteDep, session: SessionDep) -> DocumentDeleteEnvelope:
    """级联删除文档（api/01 §5.15 对账行 DELETE /kb/documents/{id}）。

    DB FK（document_chunks/kb_pipeline_step/kb_facts → documents.id）均无 ON DELETE CASCADE
    且 kb_facts.chunk_id 还指向 document_chunks.id → 应用层按依赖逆序手动级联后删文档行。
    幂等：文档不存在（含他人租户，deny-by-default 同口径）亦 200 deleted=false，不 404。
    """
    doc = (
        await session.execute(
            select(Document).where(Document.id == document_id, Document.tenant_id == principal.tenant_id)
        )
    ).scalar_one_or_none()
    if doc is None:
        return DocumentDeleteEnvelope(data=DocumentDeleteData(deleted=False))
    chunk_count = (
        await session.execute(
            select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == document_id)
        )
    ).scalar_one()
    for stmt in (  # kb_facts 先于 chunks（其 chunk_id FK 指向 document_chunks.id）
        delete(KbFact).where(KbFact.tenant_id == principal.tenant_id, KbFact.document_id == document_id),
        delete(KbPipelineStep).where(
            KbPipelineStep.tenant_id == principal.tenant_id, KbPipelineStep.document_id == document_id
        ),
        delete(DocumentChunk).where(
            DocumentChunk.tenant_id == principal.tenant_id, DocumentChunk.document_id == document_id
        ),
    ):
        await session.execute(stmt)
    await session.delete(doc)
    await session.commit()
    return DocumentDeleteEnvelope(
        data=DocumentDeleteData(deleted=True, cascade=DocumentDeleteCascade(chunks=int(chunk_count)))
    )


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

    result = await hybrid_search(
        body.query,
        bm25=bm25_fn,
        vector=vector_fn,
        graph=graph_fn if body.with_evidence else None,
        top_k=body.top_k,
        mode=body.mode,
        entity_type_filter=body.entity_type_filter,
    )
    # source_context 软路由（多源接入 §5.2 v1，service 层共用助手）：None=原序零开销零 SQL；
    # 非空=按文档 meta.source_system 加权重排（不剔除）。answers 摘要仍按融合相关度取——
    # 分组返回 schema 随 v1.5 语境术语表落地。
    final_hits = await rerank_hits_by_source_context(session, result.hits, source_context=body.source_context)
    latency_ms = int((time.perf_counter() - started) * 1000)
    return KbSearchOut(
        query=result.query,
        mode=result.mode,
        mode_used=result.mode_used,
        degraded=result.degraded,
        degraded_reasons=result.degraded_reasons,
        channels=result.channels,
        latency_ms=latency_ms,
        hits=[_hit_to_out(hit) for hit in final_hits],
        citations=[_hit_to_citation(hit) for hit in final_hits],
        evidence=KbEvidenceOut(graph_paths=[_path_to_out(path) for path in result.graph_paths])
        if body.with_evidence
        else KbEvidenceOut(graph_paths=[]),
        answers=[_answer_to_out(result.answer)] if result.answer is not None else [],
        usage=KbUsageOut(latency_ms=latency_ms),
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
    edit_accept 不接受 authoritative（终审生效态不被工作台静默降级）。
    """
    if action in ("accept", "reject"):
        if fact.status != "candidate":
            raise GatewayError(409, f"候选非 candidate 态（当前 {fact.status}），重复决策拒绝", status_code=409)
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

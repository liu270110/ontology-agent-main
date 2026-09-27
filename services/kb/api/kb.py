"""L2 知识库（kb）路由：文档上传 → 流水线 → knowledge.search lite（M2.5）。

端点（api/01 登记册 kb 行；OntRAG §5 检索契约 REST 子集）：
    POST /kb/collections                      建库
    GET  /kb/documents                        文档列表（R51 联调补齐：信封 + 前端
                                              KbDocument DTO；status/type 可选过滤）
    POST /kb/documents                        JSON 内容直传（MinIO 随 M3；checksum 幂等）
    POST /kb/documents/{id}/pipeline/start    后台流水线（202 受理；M2 lite 四步 / M2 full
                                              七步中段 extract/align/validate 已插回）
    GET  /kb/documents/{id}/pipeline          进度（八态 + 步级 checkpoint）
    POST /kb/search                           knowledge.search lite：bm25+向量+图三路 RRF
                                              （图=LazyGraphRAG lite 查询时扩展，retrieval/graph.py）

scope：kb:write（写路径）/ kb:read（检索与进度），deny-by-default（08 §2.5）。
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
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from services.kb.api.schemas.kb import (
    DOCUMENT_TYPE_FILTER,
    DOCUMENT_UI_STATUS_FILTER,
    CollectionCreateIn,
    CollectionOut,
    DocumentCreateIn,
    DocumentListData,
    DocumentListEnvelope,
    DocumentListItem,
    DocumentOut,
    DocumentPipelineProgress,
    DocumentStatusQuery,
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
    doc_type_of,
    ui_status_of,
)
from services.kb.business.kb_pipeline import M2_FULL_STEPS, PipelineError, run_pipeline
from services.kb.data.orm import Document, DocumentChunk, KbCollection, KbPipelineStep
from services.kb.retrieval.embed import AclPushdown, OllamaEmbedder, bm25_search, vector_search
from services.kb.retrieval.graph import ClassHierarchy, build_class_hierarchy, expand_graph
from services.kb.retrieval.retrieve import ExtractiveAnswer, GraphExpansion, GraphPath, SearchHit, hybrid_search
from services.ontology.business.hierarchy_service import get_class_hierarchy

# 跨模块显式服务调用（standards/01 §2.1 规则 3：business 为许可面，调用处注释模块文档）：
# 类层次读模型（database/01 §3.4）经 ontology 公开服务获取（kb 禁入 ontology.data）；
# 消费场景 = LazyGraphRAG lite 类闭包扩展（docs/OntRAG §4.0）。禁放 ontology.api——api 链触达
# ontology.data 会击穿「ontology.data 模块私有」契约（import-linter 强制）。
from services.platform.deps import Principal, SessionDep, get_session_factory, require_scope
from services.platform.errors import GatewayError
from services.platform.ports.model_port import ModelPort

if TYPE_CHECKING:  # 仅类型注解（运行时零 import——app.py 同款纪律）
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/kb", tags=["knowledge-base"])

KbReadDep = Annotated[Principal, Depends(require_scope("kb:read"))]
KbWriteDep = Annotated[Principal, Depends(require_scope("kb:write"))]

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


@router.get("/documents", summary="文档列表（管理页；status/type 可选过滤 + 流水线进度投影）")
async def list_documents(
    principal: KbReadDep,
    session: SessionDep,
    status_filter: Annotated[DocumentStatusQuery | None, Query(alias="status")] = None,
    type_filter: Annotated[KbDocType | None, Query(alias="type")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> DocumentListEnvelope:
    """当前有效文档分页（bi-temporal：valid_to IS NULL，最新优先），R51 联调补齐。

    live 对账契约（2026-09-28）：① 前端 client apiFetchEnvelope 强信封解包 → 返回
    {code,message,data:{items,total,next_cursor}}；② items=前端 KbDocument 全字段
    （name/doc_type/size_bytes/chunk_count/status 四态/progress/job_id/error/updated_at/
    indexed_today）+ pipeline{step,total}/size/created_at/tier；③ ?status=（前端四态别名
    或后端八态原值）与 ?type=（五类文档类型）均为可选过滤。空列表合法。
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

    chunk_counts: dict[uuid.UUID, int] = {}
    step_stats: dict[uuid.UUID, tuple[int, str | None]] = {}
    if docs:
        doc_ids = [doc.id for doc in docs]
        for doc_id, count in (
            await session.execute(
                select(DocumentChunk.document_id, func.count())
                .where(DocumentChunk.document_id.in_(doc_ids))
                .group_by(DocumentChunk.document_id)
            )
        ).all():
            chunk_counts[doc_id] = count
        for row in (
            await session.execute(
                select(KbPipelineStep.document_id, KbPipelineStep.status, KbPipelineStep.error)
                .where(KbPipelineStep.tenant_id == principal.tenant_id, KbPipelineStep.document_id.in_(doc_ids))
                .order_by(KbPipelineStep.created_at)
            )
        ).all():
            done, err = step_stats.get(row.document_id, (0, None))
            if row.status == "done":
                done += 1
            elif row.status == "failed" and err is None:
                err = (row.error or "流水线步骤失败")[:200]
            step_stats[row.document_id] = (done, err)

    total_steps = len(M2_FULL_STEPS)
    today = datetime.now(UTC).date()
    items = [
        DocumentListItem(
            id=doc.id,
            name=doc.title,
            doc_type=doc_type_of(doc.title, doc.mime_type),
            size=doc.size_bytes or 0,
            size_bytes=doc.size_bytes,
            chunk_count=chunk_counts.get(doc.id, 0),
            status=ui_status_of(doc.status),
            progress=100
            if doc.status == "indexed"
            else int(round(100 * step_stats.get(doc.id, (0, None))[0] / total_steps)),
            pipeline=DocumentPipelineProgress(step=step_stats.get(doc.id, (0, None))[0], total=total_steps),
            error=step_stats.get(doc.id, (0, None))[1] if doc.status == "failed" else None,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
            indexed_today=(doc.status == "indexed" and doc.updated_at.date() == today),
        )
        for doc in docs
    ]
    return DocumentListEnvelope(data=DocumentListData(items=items, total=total, offset=offset, limit=limit))


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
    latency_ms = int((time.perf_counter() - started) * 1000)
    return KbSearchOut(
        query=result.query,
        mode=result.mode,
        mode_used=result.mode_used,
        degraded=result.degraded,
        degraded_reasons=result.degraded_reasons,
        channels=result.channels,
        latency_ms=latency_ms,
        hits=[_hit_to_out(hit) for hit in result.hits],
        citations=[_hit_to_citation(hit) for hit in result.hits],
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

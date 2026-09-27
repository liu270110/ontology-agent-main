"""L3 知识库公开检索服务：knowledge.search（OntRAG §5）的跨模块轻量消费面。

存在理由：hybrid_search 及其三路召回实现归 services.kb.retrieval（import-linter
「kb.retrieval 模块私有」，standards/01 §2.1 规则 3 强制），而 chat_orchestrator
（docs/architecture/03 §3 步骤 3）需要「检索带引用」的证据链——本模块即规则 3 的
「调 kb 模块公开服务获取检索」显式调用面（business=许可面，调用处注释负责模块文档
引用）。模式照抄 services/ontology/business/hierarchy_service.py：自持返回类型
（消费方禁 import kb.retrieval 类型）、短只读会话即用即弃（检索与记忆调用在事务外，
03 §6.1）、不 import 本模块 data/ 层（bm25/vector/graph SQL 实现经 retrieval 函数
消费 AsyncSession，未触达 kb.data ORM——本文件不得新增任何 services.kb.data import）。

降级口径（03 §3 步骤 3 降级链的实现底座）：vector 路不可用 → BM25-only 且
degraded=true（hybrid_search 内建）；本服务层不再叠加重试——编排器持有
ChatPolicy.retrieval_retry_max 并在调用侧裁决重试与「无检索上下文继续」。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.kb.retrieval.embed import AclPushdown, OllamaEmbedder, bm25_search, vector_search
from services.kb.retrieval.graph import ClassHierarchy, build_class_hierarchy, expand_graph
from services.kb.retrieval.retrieve import GraphPath, SearchHit, hybrid_search
from services.ontology.business.hierarchy_service import get_class_hierarchy

# 跨模块显式服务调用（standards/01 §2.1 规则 3：business 为许可面，模块文档=database/01 §3.4）：
# 类层次读模型经 ontology 公开服务获取（kb 禁入 ontology.data；与 kb/api/kb.py 同一消费面），
# 消费场景 = LazyGraphRAG lite 类闭包扩展（docs/OntRAG §4.0）。

_HIERARCHY_TTL_SECONDS = 300.0  # 类层次进程内缓存 TTL（与 kb/api/kb.py 同值；示例值）

# 多源软路由（多源接入与连接器设计 §5.2 v1 口径：同词异义按源系统路由；知识库 GraphRAG 设计
# §11 待办 v1 裁量）：source_context 非空时对命中 chunk 按其文档 meta.source_system（documents.meta
# JSONB 键，上传方写入）匹配度微调 score 后重排——匹配 ×BOOST / 不匹配 ×PENALTY（降序不剔除，
# 软路由不硬过滤）/ 未标注 ×1.0（中性）。硬过滤与分组返回 schema 随 v1.5 语境术语表落地。
_SOURCE_CONTEXT_BOOST = 1.5  # 匹配源系统加权（示例值）
_SOURCE_CONTEXT_PENALTY = 0.5  # 异源降权（不剔除；示例值）


class KnowledgeCitation(BaseModel):
    """单条引用（OntRAG §5 citations 全字段；RETRIEVAL_EVIDENCE.citations 同构）。"""

    model_config = ConfigDict(frozen=True)

    chunk_id: uuid.UUID
    doc_id: uuid.UUID
    doc_name: str | None = None
    minio_key: str | None = None
    quote: str  # chunk 原文截片（保出处指针纪律）
    span: list[int] | None = None
    score: float = 0.0


class KnowledgeSearchResult(BaseModel):
    """公开检索结果（消费方拿到的全部形状；kb.retrieval 类型不出本模块）。"""

    model_config = ConfigDict(frozen=True)

    query: str
    degraded: bool = False
    degraded_reasons: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    citations: list[KnowledgeCitation] = Field(default_factory=list)
    graph_paths: list[dict[str, Any]] = Field(default_factory=list)  # lite=类 IRI 链（§5 evidence）
    latency_ms: int = 0


class KnowledgeSearchService:
    """knowledge.search 公开服务：短只读会话内完成三路召回 + RRF 融合 + 引用投影。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        ollama_base_url: str,
        hierarchy_ttl_s: float = _HIERARCHY_TTL_SECONDS,
        acl_filter_enabled: bool | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._embedder = OllamaEmbedder(ollama_base_url)
        self._hierarchy_ttl_s = hierarchy_ttl_s
        self._hierarchy_cache: dict[str, tuple[float, ClassHierarchy]] = {}
        # ACL 预过滤开关透传（OntRAG §4.3；本类仅透传，缺省读统一配置层 OA_ 环境变量——
        # 组合根零改动即可受 OA_KB_ACL_FILTER_ENABLED 控制）
        if acl_filter_enabled is None:
            from services.platform.config import get_settings

            acl_filter_enabled = get_settings().kb_acl_filter_enabled
        self._acl_filter_enabled = acl_filter_enabled

    async def search(
        self,
        *,
        tenant_id: uuid.UUID,
        query: str,
        kb_id: uuid.UUID | None = None,
        top_k: int = 8,
        mode: str = "local",
        entity_type_filter: Sequence[str] | None = None,
        with_evidence: bool = True,
        acl_tags: Sequence[str] | None = None,
        source_context: str | None = None,
    ) -> KnowledgeSearchResult:
        """检索（OntRAG §5 子集）：返回引用与 lite 图路；空库/零命中为空结果非失败。

        ``acl_tags``=调用方 acl 标签面（§4.3；None=调用方未接入标签面，开关开启也不激活谓词）。
        ``source_context``=源系统标识软路由（多源接入设计 §5.2：同词异义按源系统加权；
        None=维持相关度序零开销——匹配文档 meta.source_system 的命中加分排前，不匹配者降序
        不剔除，软路由不硬过滤；硬过滤与分组返回随 v1.5 语境术语表落地）。
        """
        started = time.perf_counter()
        async with self._session_factory() as db:
            hierarchy = await self._class_hierarchy(db, tenant_id)
            acl = await AclPushdown.prepare(db, enabled=self._acl_filter_enabled, allowed_tags=acl_tags)

            async def bm25_fn(q: str, k: int) -> list[SearchHit]:
                rows = await bm25_search(db, tenant_id=tenant_id, query=q, top_k=k, collection_id=kb_id, acl=acl)
                return [_dict_to_hit(r) for r in rows]

            async def vector_fn(q: str, k: int) -> list[SearchHit]:
                embeddings = await self._embedder.embed([q])
                rows = await vector_search(
                    db,
                    tenant_id=tenant_id,
                    query_embedding=embeddings[0],
                    top_k=k,
                    collection_id=kb_id,
                    acl=acl,
                )
                return [_dict_to_hit(r) for r in rows]

            async def graph_fn(seeds: Sequence[SearchHit]) -> Any:
                # 图路锚定扩展（LazyGraphRAG lite，retrieval/graph.py）；返回 GraphExpansion
                return await expand_graph(
                    db,
                    tenant_id=tenant_id,
                    collection_id=kb_id,
                    seeds=seeds,
                    hierarchy=hierarchy,
                    entity_type_filter=entity_type_filter,
                    acl=acl,
                )

            result = await hybrid_search(
                query,
                bm25=bm25_fn,
                vector=vector_fn,
                graph=graph_fn if with_evidence else None,
                top_k=top_k,
                mode=mode,
                entity_type_filter=entity_type_filter,
            )
            # source_context 软路由（多源接入 §5.2 v1）：None=原序零开销零 SQL；非空=score 微调重排
            hits = await rerank_hits_by_source_context(db, result.hits, source_context=source_context)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return KnowledgeSearchResult(
            query=result.query,
            degraded=result.degraded,
            degraded_reasons=list(result.degraded_reasons),
            channels=list(result.channels),
            citations=[_hit_to_citation(hit) for hit in hits],
            graph_paths=[_path_to_dict(path) for path in result.graph_paths],
            latency_ms=latency_ms,
        )

    async def _class_hierarchy(self, db: AsyncSession, tenant_id: uuid.UUID) -> ClassHierarchy:
        """租户类层次（当前发布版）+ 进程内 TTL 缓存；无已发布本体 → 空层次（闭包退化非失败）。"""
        key = str(tenant_id)
        now = time.monotonic()
        cached = self._hierarchy_cache.get(key)
        if cached is not None and now - cached[0] < self._hierarchy_ttl_s:
            return cached[1]
        rows = await get_class_hierarchy(db, tenant_id=tenant_id)  # ontology 公开服务（规则 3）
        hierarchy = build_class_hierarchy((row.iri, row.name, row.subclass_of) for row in rows)
        self._hierarchy_cache[key] = (now, hierarchy)
        return hierarchy


# ---------------------------------------------------------------- source_context 软路由（多源接入 §5.2 v1）


async def rerank_hits_by_source_context(
    db: AsyncSession, hits: Sequence[SearchHit], *, source_context: str | None
) -> list[SearchHit]:
    """knowledge.search 软路由重排入口（本服务与 api/kb.py search 端点共用）。

    source_context 为空 → 原样返回（零 SQL 零行为变化，向后兼容红线）；非空 → 取命中文档
    meta.source_system 后按 §5.2 语义加权重排（apply_source_context_weights）。
    """
    if not source_context or not hits:
        return list(hits)
    doc_ids = list({hit.document_id for hit in hits})
    source_systems = await _fetch_document_source_systems(db, doc_ids)
    return apply_source_context_weights(hits, source_systems, source_context=source_context)


def apply_source_context_weights(
    hits: Sequence[SearchHit],
    source_systems: Mapping[uuid.UUID, str | None],
    *,
    source_context: str,
) -> list[SearchHit]:
    """软路由纯函数（独立可单测）：score 按文档源系统匹配度微调后降序稳定重排。

    匹配 ×_SOURCE_CONTEXT_BOOST / 不匹配 ×_SOURCE_CONTEXT_PENALTY / 未标注（无
    source_system 键）×1.0；sorted 稳定性保证同分保原相关度序。只调序不剔除（§5.2 v1：
    软路由不硬过滤，不匹配者降序保留）。
    """

    def _weight(hit: SearchHit) -> float:
        doc_source = source_systems.get(hit.document_id)
        if doc_source is None:
            return 1.0
        return _SOURCE_CONTEXT_BOOST if doc_source == source_context else _SOURCE_CONTEXT_PENALTY

    adjusted = [replace(hit, score=hit.score * _weight(hit)) for hit in hits]
    return sorted(adjusted, key=lambda hit: -hit.score)


async def _fetch_document_source_systems(db: AsyncSession, doc_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str | None]:
    """批量取文档 meta.source_system（documents.meta JSONB 键，上传方写入；本切片只读）。

    raw SQL 直查 documents 表：retrieval/ 层不改（本切片边界）→ 召回行 dict 不携带文档 meta；
    模块头纪律「不得新增 services.kb.data import」不破（零 kb.data 依赖，表契约=database/01）。
    无该键的文档 → None（未标注，软路由中性）。
    """
    rows = await db.execute(
        text("SELECT id, meta->>'source_system' FROM documents WHERE id = ANY(:doc_ids)"),
        {"doc_ids": list(doc_ids)},
    )
    return {row[0]: row[1] for row in rows}


def _dict_to_hit(row: dict) -> SearchHit:
    """retrieval 行 dict → SearchHit（与 kb/api/kb.py _dict_to_hit 同构投影）。"""
    return SearchHit(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        content=row["content"],
        score=row["score"],
        doc_name=row["doc_name"],
        minio_key=row.get("minio_key"),
        span=row.get("span"),
    )


def _hit_to_citation(hit: SearchHit) -> KnowledgeCitation:
    """citations 投影（§5 全字段）：quote=chunk 原文截片（chunk content 即原文 [span) 切片）。"""
    return KnowledgeCitation(
        chunk_id=hit.chunk_id,
        doc_id=hit.document_id,
        doc_name=hit.doc_name,
        minio_key=hit.minio_key,
        quote=hit.content,
        span=hit.span,
        score=hit.score,
    )


def _path_to_dict(path: GraphPath) -> dict[str, Any]:
    """lite 图路 → JSON 形状（RETRIEVAL_EVIDENCE.graph_paths 载荷；节点=类 IRI 链）。"""
    return {
        "nodes": [{"iri": n.iri, "name": n.name, "type": n.type} for n in path.nodes],
        "rels": [{"type": r.type, "weight": r.weight} for r in path.rels],
        "chunk_ids": [str(cid) for cid in path.chunk_ids],
    }

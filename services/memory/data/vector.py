"""L2 向量通道（lite=pgvector；docs/memory/多层记忆设计.md §7 嵌入注 + 锚点 §3.2 部署分档）。

- 向量 = PG pgvector：memory_l2_facts.embedding vector(1024)（迁移 add_memory_l2_embedding
  按扩展可用性条件创建）；full 档 Milvus 随引擎裁决（memory §1 裁决框）后同构替换；
- 读写统一 raw SQL（embedding 列不进 MemoryL2Fact ORM 映射——无 pgvector 环境全链路可用），
  模式照抄 services/kb/retrieval/embed.py（列探测 + ::vector 字面量入参先例）；
- 降级契约（对齐在线四率设计）：嵌入模型不可用 / pgvector 列缺失 → 抛 EmbeddingUnavailableError，
  调用方（api/search、consolidation）降级关键词+新近通道且 degraded=true，不阻断主流程。

ORM 纪律：本文件零 ORM import（kb.retrieval.embed 同款——模块私有契约下 raw SQL 即隔离层）。
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:  # 仅类型注解
    from collections.abc import Sequence

    from services.memory.domain.model.l2_fact import L2Fact

EMBED_DIM = 1024  # bge-m3 维度契约（07 篇 §2.4；与 document_chunks 同源同参）
EMBED_MODEL = "bge-m3"
OLLAMA_TIMEOUT_SECONDS = 10.0

logger = logging.getLogger("services.memory.data.vector")


class EmbeddingUnavailableError(RuntimeError):
    """嵌入链路不可用（模型离线 / pgvector 列缺失）——调用方据此降级，不中断主流程。"""


class FactEmbedder:
    """Ollama /api/embed 轻量客户端（httpx；单条嵌入口径，kb.retrieval.embed 同款容错）。

    AsyncClient 由本类持有复用（app.state 单例，get_embedder 装配）；连接失败/超时/非 200/
    响应缺字段一律归一为 EmbeddingUnavailableError（降级口径唯一）。
    领域端口=services.memory.domain.repo.fact_repo.FactEmbedderPort（business 层仅消费端口）。
    """

    def __init__(self, base_url: str, *, model: str = EMBED_MODEL, timeout: float = OLLAMA_TIMEOUT_SECONDS) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = httpx.AsyncClient(timeout=timeout)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量嵌入；输入顺序即输出顺序；任何失败抛 EmbeddingUnavailableError。"""
        try:
            resp = await self._client.post(
                f"{self._base_url}/api/embed", json={"model": self._model, "input": list(texts)}
            )
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailableError(f"嵌入服务不可达（{self._base_url}）: {exc}") from exc
        if resp.status_code != 200:
            raise EmbeddingUnavailableError(f"嵌入服务返回 {resp.status_code}: {resp.text[:200]}")
        try:
            embeddings = resp.json()["embeddings"]
            if not isinstance(embeddings, list) or len(embeddings) != len(texts):
                raise KeyError("embeddings")
            return embeddings
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingUnavailableError(f"嵌入响应结构异常: {exc}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


def vector_literal(vector: Sequence[float]) -> str:
    """向量 → pgvector 字面量（'[0.1,0.2,...]'，raw SQL ::vector 入参；embed.py 同款）。"""
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"


async def fact_vector_ready(session: AsyncSession) -> bool:
    """pgvector 扩展与 memory_l2_facts.embedding 列是否同时可用（迁移容错跳过场景 → False）。"""
    row = await session.execute(
        text(
            "SELECT to_regtype('vector') IS NOT NULL AND EXISTS ("
            "  SELECT 1 FROM information_schema.columns"
            "  WHERE table_name = 'memory_l2_facts' AND column_name = 'embedding')"
        )
    )
    return bool(row.scalar())


_FACT_FIELDS = "id, tenant_id, user_id, content, category, confidence, decay_score, status, valid_to, created_at"


def _row_to_fact(row: RowMapping) -> L2Fact:
    """向量召回行 → 领域事实（列集只含检索排序所需最小面；指纹由模型校验器自动同步）。"""
    from services.memory.domain.model.l2_fact import FactCategory, FactStatus, L2Fact

    return L2Fact(
        id=row["id"],
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        content=row["content"],
        category=FactCategory(row["category"]),
        confidence=float(row["confidence"]),
        decay_score=float(row["decay_score"]),
        status=FactStatus(row["status"]),
        valid_to=row["valid_to"],
        created_at=row["created_at"],
    )


async def set_fact_embedding(
    session: AsyncSession, *, fact_id: uuid.UUID, vector: Sequence[float], model: str = EMBED_MODEL
) -> bool:
    """回写事实向量 + embedding_ref 模型标注（列不可用即抛降级异常）。返回是否写入。"""
    if not await fact_vector_ready(session):
        raise EmbeddingUnavailableError("pgvector embedding 列不可用（迁移容错跳过）")
    result = await session.execute(
        text(
            "UPDATE memory_l2_facts SET embedding = CAST(:vec AS vector), embedding_ref = :model "
            "WHERE id = CAST(:fact_id AS uuid)"
        ),
        {"vec": vector_literal(vector), "model": model, "fact_id": str(fact_id)},
    )
    return bool(result.rowcount)


async def vector_search_facts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    query_embedding: Sequence[float],
    top_k: int = 8,
) -> list[L2Fact]:
    """向量语义路（活跃事实，pgvector 余弦距离降序）；列不可用即抛降级异常。

    租户/用户/状态硬过滤在排序前下推（先过滤后排序，OntRAG §4.3 同款纪律）；
    embedding IS NOT NULL 只在已回写向量的事实中召回（未嵌入者仍走关键词/新近通道）。
    """
    if not await fact_vector_ready(session):
        raise EmbeddingUnavailableError("pgvector embedding 列不可用（迁移容错跳过）")
    rows = await session.execute(
        text(
            f"SELECT {_FACT_FIELDS} FROM memory_l2_facts "
            "WHERE tenant_id = CAST(:tenant_id AS uuid) AND user_id = CAST(:user_id AS uuid) "
            "AND status = 'active' AND embedding IS NOT NULL "
            "ORDER BY embedding <=> CAST(:vec AS vector) LIMIT :top_k"
        ),
        {
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "vec": vector_literal(query_embedding),
            "top_k": top_k,
        },
    )
    return [_row_to_fact(r) for r in rows.mappings()]


async def embed_fact(session: AsyncSession, embedder: FactEmbedder, *, fact_id: uuid.UUID, content: str) -> bool:
    """写入路径嵌入口：单条事实嵌入并回写（最佳努力，失败 DEBUG 留痕返回 False 不上抛）。

    调用方口径：向量是召回加速面而非正确性依赖——嵌入失败不影响事实已落账的事实
    （关键词/新近通道恒可用）；EmbeddingUnavailableError 在此收敛为返回值。
    """
    try:
        vectors = await embedder.embed([content])
        return await set_fact_embedding(session, fact_id=fact_id, vector=vectors[0])
    except EmbeddingUnavailableError as exc:
        logger.debug("fact 嵌入降级跳过（fact=%s）: %s", fact_id, exc)
        return False

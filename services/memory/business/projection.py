"""向量投影 Port（06 篇 §5.2：RRF 四策略之向量通道；§9.4-7 降级语义）。

v1 实装 pgvector lite：无独立 memory_l2_embedding 表——向量是 memory_l2_facts.embedding
vector(1024) 列（迁移 d1e2f3a4b5c6 建表 + b2d4f6a8c0e2 按扩展可用性加列加 ivfflat 余弦
索引），SQL 语义与 services/memory/data/vector.py 同源（::vector 字面量 + <=> 余弦，
kb.retrieval.embed 先例的同款模块内私有实现）；Milvus/Neo4j 为后续 Port 实现。
嵌入向量的生成（Ollama embeddings）不在本模块——调用方持预计算向量；
memory_vector_enabled=False 时使用 NullProjection（检索侧向量通道静默跳过，
RRF 按剩余通道融合）。列缺失场景（无 pgvector 环境）由调用方经 data.vector
的 fact_vector_ready/EmbeddingUnavailableError 探测降级，本 Port 不重复探测。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, Protocol

from sqlalchemy import text

EMBED_MODEL = "bge-m3"  # 维度契约 1024（07 篇 §2.4；data.vector.EMBED_MODEL 同值，避免跨层 import）


class VectorProjection(Protocol):
    async def upsert(self, record_id: uuid.UUID, embedding: list[float]) -> None: ...
    async def search(self, embedding: list[float], *, top_k: int) -> list[dict]: ...


class NullProjection:
    """关闭态：一切操作空转（§9.4-7 降级语义的静态形态）。"""

    async def upsert(self, record_id: uuid.UUID, embedding: list[float]) -> None:
        return None

    async def search(self, embedding: list[float], *, top_k: int) -> list[dict]:
        return []


def _vector_literal(vector: list[float]) -> str:
    """向量 → pgvector 字面量 '[0.1,0.2,...]'（data.vector.vector_literal 同格式，CAST(:vec AS vector) 入参）。"""
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"


class PgVectorProjection:
    """pgvector lite 投影（memory_l2_facts.embedding 列；DDL 见迁移 d1e2f3a4b5c6/b2d4f6a8c0e2）。

    conn_factory 返回 async context manager（sqlalchemy AsyncSession/AsyncConnection
    或单测 FakeConn 均可）；SQL 全参数化，向量经 CAST 入参防注入。
    """

    def __init__(self, conn_factory: Callable[[], Any]) -> None:
        self._conn_factory = conn_factory

    async def upsert(self, record_id: uuid.UUID, embedding: list[float], *, model: str = EMBED_MODEL) -> None:
        """回写事实向量（embedding 列载体=事实行本身，冲突目标=主键 id）。

        UPDATE-if-exists 语义：record 未落账时 rowcount=0 静默无行（事实生命周期归
        记录仓储管，投影只补向量面）；embedding_ref 同步标注模型（data.vector
        .set_fact_embedding 同款列对，向量缺模型注记即悬空）。
        """
        async with self._conn_factory() as conn:
            await conn.execute(
                text(
                    "UPDATE memory_l2_facts SET embedding = CAST(:vec AS vector), embedding_ref = :model "
                    "WHERE id = CAST(:rid AS uuid)"
                ),
                {"rid": str(record_id), "vec": _vector_literal(embedding), "model": model},
            )

    async def search(
        self,
        embedding: list[float],
        *,
        top_k: int,
        tenant_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
    ) -> list[dict]:
        """余弦近邻召回（活跃且已回写向量的事实），返回 [{record_id, distance}]。

        租户/用户过滤可选下推（先过滤后排序，OntRAG §4.3 同款纪律；None=不过滤，
        检索侧 RRF 接线时必须传租户面防跨租户召回）。embedding IS NOT NULL 只在
        已回写向量的事实中召回（未嵌入者走关键词/新近通道）。
        """
        filters = "WHERE status = 'active' AND embedding IS NOT NULL"
        params: dict[str, Any] = {"vec": _vector_literal(embedding), "top_k": top_k}
        if tenant_id is not None:
            filters += " AND tenant_id = CAST(:tenant_id AS uuid)"
            params["tenant_id"] = str(tenant_id)
        if user_id is not None:
            filters += " AND user_id = CAST(:user_id AS uuid)"
            params["user_id"] = str(user_id)
        async with self._conn_factory() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            f"SELECT id AS record_id, embedding <=> CAST(:vec AS vector) AS distance "
                            f"FROM memory_l2_facts {filters} "
                            "ORDER BY embedding <=> CAST(:vec AS vector) LIMIT :top_k"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            return [{"record_id": r["record_id"], "distance": float(r["distance"])} for r in rows]

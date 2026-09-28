"""向量投影 Port（06 篇 §5.2：RRF 四策略之向量通道）。

v1 实装 pgvector lite：无独立 memory_l2_embedding 表——向量是 memory_l2_facts.embedding
vector(1024) 列（迁移 d1e2f3a4b5c6 建表 + b2d4f6a8c0e2 按扩展可用性加列加 ivfflat 余弦
索引；M3 过渡裁决：lite 档 pgvector / full 档 Milvus，docs/memory/多层记忆设计.md §7）。
SQL 语义与 services/memory/data/vector.py 同源（EMBED_MODEL/vector_literal 单源 import，
<=> 余弦 + ::vector 字面量入参，kb.retrieval.embed 先例）；Milvus/Neo4j 为后续 Port 实现。
嵌入向量的生成（Ollama embeddings）不在本模块——调用方持预计算向量；
memory_vector_enabled=False 时使用 NullProjection（检索侧向量通道静默跳过，
RRF 按剩余通道融合）。列缺失场景（无 pgvector 环境）沿用 data.vector 降级契约
（EmbeddingUnavailableError → 调用方降级关键词/新近通道，degraded=true 不阻断主流程），
本 Port 不重复探测。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from sqlalchemy import text

from services.memory.data.vector import EMBED_MODEL, vector_literal

logger = logging.getLogger("services.memory.business.projection")


class VectorProjection(Protocol):
    async def upsert(self, record_id: uuid.UUID, embedding: list[float]) -> None: ...
    async def search(
        self,
        embedding: list[float],
        *,
        top_k: int,
        tenant_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
    ) -> list[dict]: ...


class NullProjection:
    """关闭态：一切操作空转（data.vector 降级契约的静态形态——向量通道整体缺席，RRF 按剩余通道融合）。"""

    async def upsert(self, record_id: uuid.UUID, embedding: list[float]) -> None:
        return None

    async def search(
        self,
        embedding: list[float],
        *,
        top_k: int,
        tenant_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
    ) -> list[dict]:
        return []


class PgVectorProjection:
    """pgvector lite 投影（memory_l2_facts.embedding 列；DDL 见迁移 d1e2f3a4b5c6/b2d4f6a8c0e2）。

    conn_factory 须提供 AsyncConnection/AsyncSession（async-with 协议）；本类内部管理
    事务：upsert 显式 begin 提交（防 factory 会话 __aexit__=close 隐式回滚丢写），
    search 只读不 begin。SQL 全参数化，向量经 CAST 入参防注入。
    """

    def __init__(self, conn_factory: Callable[[], Any]) -> None:
        self._conn_factory = conn_factory

    async def upsert(self, record_id: uuid.UUID, embedding: list[float], *, model: str = EMBED_MODEL) -> None:
        """回写事实向量（embedding 列载体=事实行本身，冲突目标=主键 id）。

        UPDATE-if-exists 语义：record 未落账时 rowcount=0 DEBUG 留痕静默无行
        （事实生命周期归记录仓储管，投影只补向量面）；embedding_ref 同步标注模型
        （data.vector.set_fact_embedding 同款列对，向量缺模型注记即悬空）。
        """
        updated = 0
        async with self._conn_factory() as conn:
            async with conn.begin():
                result = await conn.execute(
                    text(
                        "UPDATE memory_l2_facts SET embedding = CAST(:vec AS vector), embedding_ref = :model "
                        "WHERE id = CAST(:rid AS uuid)"
                    ),
                    {"rid": str(record_id), "vec": vector_literal(embedding), "model": model},
                )
                updated = result.rowcount
        if updated == 0:
            logger.debug("向量投影落空（record 未落账，rowcount=0）: %s", record_id)

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
        params: dict[str, Any] = {"vec": vector_literal(embedding), "top_k": top_k}
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

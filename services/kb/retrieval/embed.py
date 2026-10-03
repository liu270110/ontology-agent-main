"""嵌入客户端 + BM25/向量召回 SQL（OntRAG §4.0 检索默认档的 M2 lite 存储执行层）。

- 向量 = PG pgvector（deployment lite；Milvus 随 M5+）：document_chunks.embedding
  vector(1024)（迁移 add_kb_vector_embedding 按扩展可用性条件创建）；
- BM25 = PG tsvector + ts_rank（GIN 表达式索引同迁移；'simple' 配置为任务口径基线，
  中文分词随 M3+）；
- 降级契约（对齐在线四率设计）：嵌入模型不可用 / pgvector 列缺失 → 抛
  EmbeddingUnavailableError，上层 hybrid_search 降级 BM25-only 且响应 degraded=true；
  流水线 embed 步同样捕获该异常做软降级（BM25-only 索引，可重跑补向量）。

ORM 纪律：embedding 列不在 DocumentChunk ORM 映射中（无 pgvector 环境全链路可用），
读写统一走本文件 raw SQL。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:  # 仅类型注解
    from collections.abc import Sequence

EMBED_DIM = 1024  # bge-m3 维度契约（07 篇 §2.4；换模型必须走重建切换，OntRAG §8.7）
EMBED_MODEL = "bge-m3"
OLLAMA_TIMEOUT_SECONDS = 10.0
EMBED_BATCH_SIZE = 32

# 嵌入端点协议（docs/Agent/09 §2.1 工程问题 2「嵌入协议漂移」；OA_EMBED_PROTOCOL 配置开关）：
# - ollama：POST {base}/api/embed，body {model, input}，响应取 embeddings 字段（ops/03 单轨口径）；
# - tei：POST {base}/embed，body {inputs}，响应直接是数组的数组（huggingface text-embeddings-inference）。
EMBED_PROTOCOLS = ("ollama", "tei")

logger = logging.getLogger("services.kb.retrieval.acl")


class EmbeddingUnavailableError(RuntimeError):
    """嵌入链路不可用（模型离线 / pgvector 列缺失）——调用方据此降级，不中断主流程。"""


class OllamaEmbedder:
    """批量嵌入客户端（httpx；超时 10s；批量 ≤ batch_size 分批；双协议 ollama|tei）。

    协议由配置开关选择（Settings.embed_protocol，OA_EMBED_PROTOCOL；docs/Agent/09 §2.1
    工程问题 2「嵌入协议漂移」的正式解法）：ollama=POST /api/embed（默认，存量口径零变化）；
    tei=POST /embed（本机 GPU 栈 text-embeddings-inference 部署）。``model`` 仅 ollama 协议
    出网（TEI 模型在服务端部署期固定，请求无 model 字段）——tei 模式换模型须重启 TEI 服务。
    AsyncClient 由调用方持有复用（网关挂 app.state）；连接失败/超时/非 200/响应缺字段/返回
    长度与批次不一致一律归一为 EmbeddingUnavailableError（调用方降级口径唯一）。
    """

    def __init__(
        self,
        base_url: str,
        *,
        model: str = EMBED_MODEL,
        timeout: float = OLLAMA_TIMEOUT_SECONDS,
        batch_size: int = EMBED_BATCH_SIZE,
        client: httpx.AsyncClient | None = None,
        protocol: str = "ollama",  # ollama|tei；tei 模式下 model 不出网（TEI 部署期固定模型）
    ) -> None:
        if protocol not in EMBED_PROTOCOLS:
            raise ValueError(f"未知嵌入协议 {protocol!r}（可选：{'|'.join(EMBED_PROTOCOLS)}）")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._batch_size = max(1, batch_size)
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._protocol = protocol

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量嵌入；输入顺序即输出顺序；任何失败抛 EmbeddingUnavailableError。"""
        items = list(texts)
        out: list[list[float]] = []
        for start in range(0, len(items), self._batch_size):
            batch = items[start : start + self._batch_size]
            out.extend(await self._embed_batch(batch))
        return out

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        try:
            if self._protocol == "tei":
                resp = await self._client.post(f"{self._base_url}/embed", json={"inputs": batch})
            else:
                resp = await self._client.post(
                    f"{self._base_url}/api/embed", json={"model": self._model, "input": batch}
                )
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailableError(f"嵌入服务不可达（{self._base_url}）: {exc}") from exc
        if resp.status_code != 200:
            raise EmbeddingUnavailableError(f"嵌入服务返回 {resp.status_code}: {resp.text[:200]}")
        try:
            if self._protocol == "tei":
                embeddings = resp.json()  # TEI 响应直接是数组的数组（无包裹字段）
            else:
                embeddings = resp.json()["embeddings"]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingUnavailableError(f"嵌入响应结构异常（{self._protocol}）: {exc}") from exc
        if not isinstance(embeddings, list) or len(embeddings) != len(batch):
            got = f"向量数 {len(embeddings)}" if isinstance(embeddings, list) else f"非数组 {type(embeddings).__name__}"
            raise EmbeddingUnavailableError(
                f"嵌入响应与批次不一致（{self._protocol}）: 响应{got} != 批次 {len(batch)}"
            )
        return embeddings

    async def aclose(self) -> None:
        await self._client.aclose()


def vector_literal(vector: Sequence[float]) -> str:
    """向量 → pgvector 字面量（'[0.1,0.2,...]'，raw SQL ::vector 入参）。"""
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"


async def vector_ready(session: AsyncSession) -> bool:
    """pgvector 扩展与 document_chunks.embedding 列是否同时可用（迁移容错跳过场景 → False）。"""
    row = await session.execute(
        text(
            "SELECT to_regtype('vector') IS NOT NULL AND EXISTS ("
            "  SELECT 1 FROM information_schema.columns"
            "  WHERE table_name = 'document_chunks' AND column_name = 'embedding')"
        )
    )
    return bool(row.scalar())


# ---------------------------------------------------------------- ACL 预过滤下推（OntRAG §4.3；M5 收缩开关化）

# documents.acl_tags 列谓词（jsonb 标签数组）：未标注/空标注文档=继承租户全员可见（§4.3 缺省口径）；
# 已标注文档须与调用方标签面相交（deny-by-default）。片段由 AclPushdown 激活时才拼接，
# 开关关闭时调用方 SQL 与存量逐字节一致（零行为变化红线）。
_ACL_TAGS_PREDICATE = " AND (d.acl_tags IS NULL OR d.acl_tags = '[]'::jsonb OR d.acl_tags ?| CAST(:acl_tags AS text[]))"


async def acl_tags_ready(session: AsyncSession) -> bool:
    """documents.acl_tags 列是否存在（无迁移部署 → False；information_schema 探测，vector_ready 同款）。"""
    row = await session.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
            " WHERE table_name = 'documents' AND column_name = 'acl_tags')"
        )
    )
    return bool(row.scalar())


class AclPushdown:
    """ACL 预过滤下推单元（每次检索调用构造一份；_bm25/_vector/_graph 三路共用同源谓词）。

    语义（OntRAG §4.3 + 13 篇 M5-2 收缩裁决「配置开关化（无迁移方案）」）：
    - enabled=False → 空片段零参数：调用方 SQL 与存量逐字节一致（零行为变化红线）；
    - enabled=True + acl_tags 列缺失 → 自动 no-op 并 DEBUG 留痕（列存在性探测，迁移容错同 vector）；
    - enabled=True + 列存在 + allowed_tags=None → no-op（调用方未接入调用方标签面，无过滤依据）；
    - enabled=True + 列存在 + allowed_tags 给出（含空表）→ 谓词激活：未标注文档放行、
      已标注文档须命中调用方标签面（空表=仅租户继承文档可见，deny-by-default）。
    """

    __slots__ = ("fragment", "params")

    def __init__(self) -> None:
        self.fragment: str = ""
        self.params: dict[str, object] = {}

    @classmethod
    async def prepare(cls, session: AsyncSession, *, enabled: bool, allowed_tags: Sequence[str] | None) -> AclPushdown:
        """按开关/列存在性/调用方标签面三态准备下推单元（一次检索构造一次，三路共用）。"""
        pushdown = cls()
        if not enabled:
            return pushdown
        if not await acl_tags_ready(session):
            logger.debug("ACL 预过滤降级 no-op: documents.acl_tags 列缺失（迁移未应用，开关开启不生效）")
            return pushdown
        if allowed_tags is None:
            logger.debug("ACL 预过滤未激活: 调用方未声明 acl 标签面（allowed_tags=None）")
            return pushdown
        pushdown.fragment = _ACL_TAGS_PREDICATE
        pushdown.params = {"acl_tags": list(allowed_tags)}
        return pushdown


# §5 citations 全字段：minio_key 随 chunk 行一并取（出处指针；2026-09-27 任务 2.3 契约补全）
_CHUNK_FIELDS = "c.id AS chunk_id, c.document_id, c.content, c.meta AS chunk_meta, d.title AS doc_name, d.minio_key"


def _acl_filter(as_of: datetime | None, include_superseded: bool) -> str:
    """§4.3 ACL + §8.2 bi-temporal 谓词（时间参数权威=docs/OntRAG §8.2）。

    缺省=当前有效视图（valid_to 封口排除）；as_of=时点检索（valid_from ≤ as_of < valid_to）；
    include_superseded=True 放开封口（被取代知识一并返回；lite 档仅 chunk/document 代际，
    事实级代际激活随 kb_facts 状态机 v0.2 二期）。注：text() 不绑定后随 :: 的参数，cast 写 CAST()。
    """
    base = (
        "c.tenant_id = :tenant_id "
        "AND (CAST(:collection_id AS uuid) IS NULL OR d.kb_collection_id = CAST(:collection_id AS uuid))"
    )
    if include_superseded:
        return base
    if as_of is not None:
        return (
            base
            + " AND c.valid_from <= :as_of AND (c.valid_to IS NULL OR c.valid_to > :as_of)"
            + " AND d.valid_from <= :as_of AND (d.valid_to IS NULL OR d.valid_to > :as_of)"
        )
    return base + " AND c.valid_to IS NULL AND d.valid_to IS NULL"


async def bm25_search(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    query: str,
    top_k: int = 8,
    collection_id: uuid.UUID | None = None,
    as_of: datetime | None = None,
    include_superseded: bool = False,
    acl: AclPushdown | None = None,
) -> list[dict]:
    """BM25 词法路：tsvector @@ tsquery，ts_rank 排序（GIN 表达式索引命中同款表达式）。

    §4.3 ACL 预过滤：tenant/collection/valid_to 硬过滤在排序前下推（先过滤后排序）；
    acl 标签谓词（AclPushdown 激活时）同样在排序前拼接。
    空查询 / 无命中 → 空列表（websearch_to_tsquery 对空串产出空查询，自然零命中）。
    """
    rows = await session.execute(
        text(
            f"SELECT {_CHUNK_FIELDS}, ts_rank(to_tsvector('simple', c.content), q) AS score "
            "FROM document_chunks c JOIN documents d ON d.id = c.document_id "
            "CROSS JOIN websearch_to_tsquery('simple', :query) AS q "
            f"WHERE {_acl_filter(as_of, include_superseded)}{acl.fragment if acl else ''} "
            "AND to_tsvector('simple', c.content) @@ q "
            "ORDER BY score DESC LIMIT :top_k"
        ),
        {
            "tenant_id": tenant_id,
            "query": query,
            "top_k": top_k,
            "collection_id": collection_id,
            "as_of": as_of,
            **(acl.params if acl else {}),
        },
    )
    return [_row_to_hit(r) for r in rows.mappings()]


async def vector_search(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    query_embedding: Sequence[float],
    top_k: int = 8,
    collection_id: uuid.UUID | None = None,
    as_of: datetime | None = None,
    include_superseded: bool = False,
    acl: AclPushdown | None = None,
) -> list[dict]:
    """向量语义路：pgvector 余弦距离（1 - cosine_distance），列不可用即抛降级异常。"""
    if not await vector_ready(session):
        raise EmbeddingUnavailableError("pgvector embedding 列不可用（迁移容错跳过）")
    rows = await session.execute(
        text(
            f"SELECT {_CHUNK_FIELDS}, 1 - (c.embedding <=> CAST(:vec AS vector)) AS score "
            "FROM document_chunks c JOIN documents d ON d.id = c.document_id "
            f"WHERE {_acl_filter(as_of, include_superseded)}{acl.fragment if acl else ''} "
            "AND c.embedding IS NOT NULL "
            "ORDER BY c.embedding <=> CAST(:vec AS vector) LIMIT :top_k"
        ),
        {
            "tenant_id": tenant_id,
            "vec": vector_literal(query_embedding),
            "top_k": top_k,
            "collection_id": collection_id,
            "as_of": as_of,
            **(acl.params if acl else {}),
        },
    )
    return [_row_to_hit(r) for r in rows.mappings()]


def _row_to_hit(row: RowMapping) -> dict:
    meta = row["chunk_meta"] or {}
    span = meta.get("span") if isinstance(meta, dict) else None
    return {
        "chunk_id": row["chunk_id"],
        "document_id": row["document_id"],
        "content": row["content"],
        "score": float(row["score"]),
        "doc_name": row["doc_name"],
        "minio_key": row["minio_key"],
        "span": span,
    }


async def fetch_chunks_missing_embedding(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> list[tuple[uuid.UUID, str]]:
    """取未嵌入 chunks（id, content），按 seq 序；embed 步增量口径（§8.6 未变块跳过）。"""
    rows = await session.execute(
        text(
            "SELECT id, content FROM document_chunks "
            "WHERE tenant_id = :tenant_id AND document_id = :document_id AND valid_to IS NULL "
            "ORDER BY seq"
        ),
        {"tenant_id": tenant_id, "document_id": document_id},
    )
    return [(r[0], r[1]) for r in rows]


async def set_chunk_embeddings(session: AsyncSession, items: Sequence[tuple[uuid.UUID, Sequence[float]]]) -> int:
    """逐条回写 chunk 向量（embed 步专用；列不可用即抛降级异常）。返回写入行数。"""
    if not await vector_ready(session):
        raise EmbeddingUnavailableError("pgvector embedding 列不可用（迁移容错跳过）")
    result = await session.execute(
        text("UPDATE document_chunks SET embedding = CAST(:vec AS vector) WHERE id = :chunk_id"),
        [{"chunk_id": cid, "vec": vector_literal(vec)} for cid, vec in items],
    )
    return result.rowcount or 0

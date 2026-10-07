"""向量重建 v1 最小五步（OntRAG §8.7「建新 → 全量重嵌 → 影子双读 → 切流 → 可回滚」的 lite 承载）。

v1 最小口径（诚实收缩，任务 KB-G1b 2026-09-29）——落「任务模型 + 进度 + 影子双读对比 +
切流开关」四件，**不建物理双 collection**（lite 档 pgvector 无 Milvus 别名，§8.7 步 1
lite 注记：版本切换由 kb_reembed_jobs 状态行 + 配置承载）：

===============  ==============================================================
§8.7 五步        v1 lite 落地
===============  ==============================================================
1 建 v2          plan_reembed：登记 kb_reembed_jobs 行（status=planning），
                 new_model 记目标模型；同租户活跃任务至多一个
2 全量重嵌       run_reembed：planning→reembedding 时先把旧向量备份到
                 document_chunks.backup_embedding（幂等：仅补 NULL），随后批量
                 重嵌直接写 embedding 列；断点续跑=progress.cursor（chunk id
                 hex32 游标），完成→status=shadow
3 影子双读       shadow_compare：采样查询用**同一新模型**嵌入后分别查
                 embedding（新）与 backup_embedding（旧）两列 top-k，重叠率差
                 = 新旧向量对同查询的召回差（v1 无查询侧双模型嵌入，报告差值，
                 判据沿用 §8.7 命中率差 <2%）
4 切流           cutover：检索读 embedding 列已是新向量——v1 切流即重嵌完成
                 确认态（status=cutover），无别名/配置翻转动作
5 保留与回滚     backup_embedding 保留旧向量（30 天保留期处置随 ops 变更单）；
                 rollback=两列交换（单条 UPDATE 原子交换，新向量保留在 backup）
===============  ==============================================================

已知边界（登记遗留，不在本切片）：
- **维度变更需先扩列**：backup_embedding/embedding 均为 vector(1024)，换不同维度模型
  须先走 DDL 扩列迁移（07 篇 §2.4 维度契约的 v1 简化）；超维向量按 pgvector 报错自然
  失败，不静默截断；
- backup 列不建 ivfflat 索引（低频影子读，不付写放大）；
- 迁移窗口新写 chunk（backup 为 NULL）不参与回滚交换（无旧向量可还原），保持新向量。

ORM 纪律：embedding/backup_embedding 不映射 ORM，raw SQL 读写（embed.py 同款）；
方言分支——PG 走 pgvector CAST/`<=>` 算子与 uuid 原生绑定，SQLite（测试）走文本存储
（uuid 以 hex32 绑定，与 CHAR(32) 存储同构）+ Python 余弦。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.kb.data.maintenance_orm import ACTIVE_REEMBED_STATUSES, KbReembedJob
from services.kb.data.orm import DocumentChunk
from services.kb.retrieval.embed import EmbeddingUnavailableError, vector_literal

logger = logging.getLogger(__name__)

SHADOW_DIFF_THRESHOLD = 0.02  # §8.7 切换判据：命中率差 <2%（连续达标窗口随 PoC ③ 冻结）
DEFAULT_BATCH_SIZE = 64  # §8.7 步 2 批量口径（租户并发让路随空闲闸门接入）
_VECTOR_COLUMNS = frozenset({"embedding", "backup_embedding"})  # raw SQL 列名白名单（防注入）


class EmbedderProtocol(Protocol):
    """最小嵌入面（OllamaEmbedder 同构；测试桩注入）。"""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _is_pg(session: AsyncSession) -> bool:
    return session.bind is not None and session.bind.dialect.name == "postgresql"


def _tid(session: AsyncSession, tenant_id: uuid.UUID | str) -> Any:
    """raw SQL 租户/主键参数方言适配：PG 绑 uuid 对象（psycopg 原生）；SQLite 绑 hex32
    （与 UUID 类型的 CHAR(32) 存储同构比较；SQLite 往返可能回 str，一律归一化）。
    ORM 查询不适用（类型自处理）。"""
    value = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
    return value if _is_pg(session) else value.hex


# ---------------------------------------------------------------- 列就绪探测与读写（raw SQL，方言分支）


async def vector_column_ready(session: AsyncSession, column: str) -> bool:
    """向量列是否存在（列存在性探测：PG=information_schema，SQLite=pragma_table_info；
    acl_tags_ready/vector_ready 同款意图，本处补方言分支以支撑 SQLite 测试直跑）。"""
    if column not in _VECTOR_COLUMNS:
        raise ValueError(f"未知向量列: {column}")
    if _is_pg(session):
        probe = (
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
            " WHERE table_name = 'document_chunks' AND column_name = :col)"
        )
    else:
        probe = "SELECT EXISTS (SELECT 1 FROM pragma_table_info('document_chunks') WHERE name = :col)"
    row = await session.execute(text(probe), {"col": column})
    return bool(row.scalar())


async def write_chunk_vectors(
    session: AsyncSession, items: Sequence[tuple[uuid.UUID, Sequence[float]]], *, column: str = "embedding"
) -> int:
    """批量回写 chunk 向量到指定列（embedding/backup_embedding）；返回写入行数。

    PG 走 CAST(:vec AS vector)；SQLite（测试环境）按文本直写——列由迁移按 pgvector
    可用性条件创建，调用方须先经 vector_column_ready 探测（本函数不重复探测）。
    """
    if column not in _VECTOR_COLUMNS:
        raise ValueError(f"未知向量列: {column}")
    cast = "CAST(:vec AS vector)" if _is_pg(session) else ":vec"
    result = await session.execute(
        text(f"UPDATE document_chunks SET {column} = {cast} WHERE id = :chunk_id"),  # noqa: S608（列名白名单）
        [{"chunk_id": _tid(session, cid), "vec": vector_literal(vec)} for cid, vec in items],
    )
    return result.rowcount or 0


def _parse_stored_vector(raw: Any) -> list[float] | None:
    """读回存储向量 → float 列表（PG vector 已是序列；SQLite 为 '[a,b]' 文本）。"""
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    try:
        return [float(x) for x in str(raw).strip("[] \n").split(",") if x.strip()]
    except ValueError:
        return None


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（SQLite 影子读路径；PG 语义 1 - cosine_distance 同口径）。"""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


async def _topk_by_column(
    session: AsyncSession, *, column: str, tenant_id: uuid.UUID, query_vector: Sequence[float], top_k: int
) -> list[uuid.UUID]:
    """按指定向量列取 top-k chunk id（PG: `<=>` 算子；SQLite: 全量读回 Python 余弦）。"""
    if column not in _VECTOR_COLUMNS:
        raise ValueError(f"未知向量列: {column}")
    tid = _tid(session, tenant_id)
    if _is_pg(session):
        rows = await session.execute(
            text(
                f"SELECT id FROM document_chunks WHERE tenant_id = :tenant_id AND valid_to IS NULL"  # noqa: S608
                f" AND {column} IS NOT NULL"
                f" ORDER BY {column} <=> CAST(:vec AS vector) LIMIT :top_k"
            ),
            {"tenant_id": tid, "vec": vector_literal(query_vector), "top_k": top_k},
        )
        return [r[0] for r in rows]
    rows = await session.execute(
        text(
            f"SELECT id, {column} FROM document_chunks WHERE tenant_id = :tenant_id AND valid_to IS NULL"  # noqa: S608
            f" AND {column} IS NOT NULL"
        ),
        {"tenant_id": tid},
    )
    scored: list[tuple[float, uuid.UUID]] = []
    for raw_id, raw_vec in rows:
        vec = _parse_stored_vector(raw_vec)
        if vec is not None:
            scored.append((_cosine_similarity(query_vector, vec), uuid.UUID(str(raw_id))))
    scored.sort(key=lambda pair: -pair[0])
    return [cid for _, cid in scored[:top_k]]


# ---------------------------------------------------------------- 任务行装载与状态机


async def _load_job(session: AsyncSession, job_id: uuid.UUID) -> KbReembedJob:
    job = (await session.execute(select(KbReembedJob).where(KbReembedJob.id == job_id))).scalar_one_or_none()
    if job is None:
        raise ValueError(f"404 重嵌任务不存在: {job_id}")
    return job


def _assert_transition(job: KbReembedJob, *, from_statuses: Sequence[str], to: str) -> None:
    """状态迁移断言（from 白名单；to==当前态幂等放行）。"""
    if job.status == to:
        return
    if job.status not in from_statuses:
        raise ValueError(f"409 重嵌任务状态不可迁移: {job.status} → {to}")
    job.status = to


# ---------------------------------------------------------------- 步 1：plan（建 v2 任务）


async def plan_reembed(session: AsyncSession, tenant_id: uuid.UUID, new_model: str) -> KbReembedJob:
    """登记重嵌任务（§8.7 步 1 lite）：status=planning 任务行；同租户活跃任务至多一个。

    事务边界：函数内 commit（maintenance._run 同款口径）；new_model 仅登记不校验可达
    （可达性由 run_reembed 首批嵌入调用自然暴露）。
    """
    active = (
        (
            await session.execute(
                select(KbReembedJob.id).where(
                    KbReembedJob.tenant_id == tenant_id, KbReembedJob.status.in_(ACTIVE_REEMBED_STATUSES)
                )
            )
        )
        .scalars()
        .first()
    )
    if active is not None:
        raise ValueError(f"409 该租户已有活跃重嵌任务: {active}")
    job = KbReembedJob(tenant_id=tenant_id, status="planning", new_model=new_model, progress={})
    session.add(job)
    await session.commit()
    await session.refresh(job)  # commit 过期后取属性（expire_on_commit=True 工厂安全）
    logger.info("kb_reembed planned: tenant=%s job=%s new_model=%s", tenant_id, job.id, new_model)
    return job


# ---------------------------------------------------------------- 步 2：全量重嵌（断点续跑）


async def _backup_embeddings(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """旧向量备份到 backup_embedding（幂等：仅补 NULL；迁移窗口边界见模块头）。"""
    result = await session.execute(
        text(
            "UPDATE document_chunks SET backup_embedding = embedding"
            " WHERE tenant_id = :tenant_id AND embedding IS NOT NULL AND backup_embedding IS NULL"
        ),
        {"tenant_id": _tid(session, tenant_id)},
    )
    return result.rowcount or 0


async def run_reembed(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    embedder: EmbedderProtocol,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict:
    """批量全量重嵌（§8.7 步 2）：备份→逐批重嵌→进度落账；完成转 shadow。

    - 断点续跑：每批一短事务（向量写入 + progress{done,cursor} 同事务提交），崩溃后
      从 cursor 续跑；批间窗口的重复嵌入幂等（同内容同向量，仅成本）；
    - 嵌入调用在事务外（03 §6.1 同款：长调用不持事务）；
    - embedding 列缺失（无 pgvector 环境）→ EmbeddingUnavailableError（任务停在
      planning/reembedding，环境修复后原 job 续跑）；
    - 重嵌面=租户全部 live chunk（含 embedding IS NULL 缺向量块，§8.7 全量口径）。
    """
    async with session_factory() as session, session.begin():  # 首跑事务：状态翻转 + 备份
        job = await _load_job(session, job_id)
        if job.status not in ("planning", "reembedding"):
            raise ValueError(f"409 重嵌任务状态不可执行重嵌: {job.status}")
        if job.status == "planning":
            if not await vector_column_ready(session, "embedding"):
                raise EmbeddingUnavailableError("embedding 列不可用（无 pgvector 环境，迁移容错跳过）")
            _assert_transition(job, from_statuses=("planning",), to="reembedding")
            job.progress = {**(job.progress or {}), "backed_up": await _backup_embeddings(session, job.tenant_id)}
        tenant_id, job_pk = job.tenant_id, job.id
        progress: dict = dict(job.progress or {})

    done = int(progress.get("done") or 0)
    cursor_hex: str | None = progress.get("cursor")
    while True:
        async with session_factory() as session:  # 短事务：取批（ORM 查询，游标后继 id 序）
            stmt = select(DocumentChunk.id, DocumentChunk.content).where(
                DocumentChunk.tenant_id == tenant_id,
                DocumentChunk.valid_to.is_(None),
            )
            if cursor_hex is not None:
                stmt = stmt.where(DocumentChunk.id > uuid.UUID(cursor_hex))
            rows = (await session.execute(stmt.order_by(DocumentChunk.id).limit(batch_size))).all()
        if not rows:
            break
        vectors = await embedder.embed([content for _, content in rows])  # 事务外（03 §6.1）
        pairs = [(cid, vec) for (cid, _), vec in zip(rows, vectors, strict=True)]
        async with session_factory() as session, session.begin():  # 短事务：写向量 + 进度
            if not await vector_column_ready(session, "embedding"):
                raise EmbeddingUnavailableError("embedding 列不可用（重嵌中途环境漂移）")
            await write_chunk_vectors(session, pairs, column="embedding")
            done += len(pairs)
            cursor_hex = pairs[-1][0].hex  # hex32 统一（游标持久化的稳定表述）
            progress = {**progress, "done": done, "cursor": cursor_hex, "updated_at": _utcnow().isoformat()}
            await session.execute(
                text(
                    "UPDATE kb_reembed_jobs SET progress ="
                    f" {'CAST(:progress AS jsonb)' if _is_pg(session) else ':progress'}"
                    " WHERE id = :id"
                ),
                {"progress": json.dumps(progress, ensure_ascii=False), "id": _tid(session, job_pk)},
            )
        if len(rows) < batch_size:
            break

    async with session_factory() as session, session.begin():  # 完成：转 shadow（影子双读期）
        total = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentChunk)
                    .where(DocumentChunk.tenant_id == tenant_id, DocumentChunk.valid_to.is_(None))
                )
            ).scalar_one()
        )
        job = await _load_job(session, job_pk)
        _assert_transition(job, from_statuses=("reembedding",), to="shadow")
        job.progress = {**progress, "done": done, "total": total}
    final_progress = {**progress, "done": done, "total": total}
    logger.info("kb_reembed finished: job=%s done=%s total=%s → shadow", job_pk, done, total)
    return final_progress


# ---------------------------------------------------------------- 步 3：影子双读对比


class QueryShadow(BaseModel):
    """单查询影子对比：新旧两列 top-k 命中集与重叠数。"""

    query: str
    new_hits: list[uuid.UUID]
    old_hits: list[uuid.UUID]
    overlap: int


class ShadowReport(BaseModel):
    """影子双读报告：重叠率均值与差值（v1 口径=同查询两列 top-k 重叠率，见模块头）。"""

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    top_k: int
    queries: list[QueryShadow] = Field(default_factory=list)
    mean_overlap: float = 0.0  # 平均重叠率（1.0=两列 top-k 完全一致）
    diff: float = 1.0  # 差值 = 1 - mean_overlap（§8.7「命中率差」的 v1 同构）
    threshold: float = SHADOW_DIFF_THRESHOLD
    passed: bool = False


async def shadow_compare(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    sample_queries: Sequence[str],
    embedder: EmbedderProtocol,
    *,
    top_k: int = 8,
    threshold: float = SHADOW_DIFF_THRESHOLD,
) -> ShadowReport:
    """影子双读（§8.7 步 3 v1）：同一新模型嵌入查询，分别查新/旧两列 top-k 比重叠。

    判据：diff = 1 - mean(overlap/top_k) < threshold（默认 2%，§8.7；连续达标窗口与
    在线采样随 PoC ③ / ops 观测接入，v1 为显式调用面）。
    """
    if not sample_queries:
        raise ValueError("3001 PARAM_INVALID: sample_queries 为空（影子对比无判据）")
    for column in _VECTOR_COLUMNS:
        if not await vector_column_ready(session, column):
            raise EmbeddingUnavailableError(f"{column} 列不可用（影子双读前置缺失）")
    query_vectors = await embedder.embed(list(sample_queries))
    results: list[QueryShadow] = []
    for query, qvec in zip(sample_queries, query_vectors, strict=True):
        new_hits = await _topk_by_column(
            session, column="embedding", tenant_id=tenant_id, query_vector=qvec, top_k=top_k
        )
        old_hits = await _topk_by_column(
            session, column="backup_embedding", tenant_id=tenant_id, query_vector=qvec, top_k=top_k
        )
        results.append(
            QueryShadow(
                query=query,
                new_hits=new_hits,
                old_hits=old_hits,
                overlap=len(set(new_hits) & set(old_hits)),
            )
        )
    mean_overlap = sum(r.overlap for r in results) / (len(results) * top_k)
    diff = 1.0 - mean_overlap
    return ShadowReport(
        tenant_id=tenant_id,
        top_k=top_k,
        queries=results,
        mean_overlap=round(mean_overlap, 6),
        diff=round(diff, 6),
        threshold=threshold,
        passed=diff < threshold,
    )


# ---------------------------------------------------------------- 步 4/5：切流、回滚、退役


async def cutover(session: AsyncSession, job_id: uuid.UUID) -> str:
    """切流（§8.7 步 4 v1）：status→cutover——检索读 embedding 列已是新向量，切流=确认态。

    幂等：已 cutover 再调原样返回；planning（未重嵌）/终态拒绝。
    """
    async with session.begin():
        job = await _load_job(session, job_id)
        _assert_transition(job, from_statuses=("shadow", "reembedding"), to="cutover")
        status = job.status  # commit 过期前取值（expire_on_commit=True 工厂安全）
    logger.info("kb_reembed cutover: job=%s", job_id)
    return status


async def rollback_reembed(session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> int:
    """回滚（§8.7 步 5 v1）：backup_embedding 与 embedding 两列交换 → status=rolled_back。

    交换为**单条 UPDATE**（`SET embedding = backup_embedding, backup_embedding = embedding`）：
    PG/SQLite 的 SET 右值均绑定旧行值，一语句原子完成真交换（无需中间列）；交换对称，
    新向量保留在 backup 列（再切流入口随 v1.5 状态机扩展登记）。backup 为 NULL 的行
    （迁移窗口新写 chunk）不参与交换：无旧向量可还原，保持新向量（模块头边界注）。
    """
    async with session_factory() as session, session.begin():
        job = await _load_job(session, job_id)
        if job.status in ("planning", "rolled_back", "retired"):
            raise ValueError(f"409 重嵌任务状态不可回滚: {job.status}")
        result = await session.execute(
            text(
                "UPDATE document_chunks SET embedding = backup_embedding, backup_embedding = embedding"
                " WHERE tenant_id = :tenant_id AND backup_embedding IS NOT NULL"
            ),
            {"tenant_id": _tid(session, job.tenant_id)},
        )
        job.status = "rolled_back"
    swapped = result.rowcount or 0
    logger.info("kb_reembed rolled back: job=%s swapped_rows=%s", job_id, swapped)
    return swapped


async def retire_reembed(session: AsyncSession, job_id: uuid.UUID) -> str:
    """保留期满退役（§8.7 步 5：30 天保留期满经 ops 变更单下线；v1 仅状态迁移面）。"""
    async with session.begin():
        job = await _load_job(session, job_id)
        _assert_transition(job, from_statuses=("cutover", "rolled_back"), to="retired")
        status = job.status  # commit 过期前取值
    logger.info("kb_reembed retired: job=%s", job_id)
    return status


__all__ = [
    "ACTIVE_REEMBED_STATUSES",
    "DEFAULT_BATCH_SIZE",
    "EmbedderProtocol",
    "QueryShadow",
    "SHADOW_DIFF_THRESHOLD",
    "ShadowReport",
    "cutover",
    "plan_reembed",
    "retire_reembed",
    "rollback_reembed",
    "run_reembed",
    "shadow_compare",
    "vector_column_ready",
    "write_chunk_vectors",
]

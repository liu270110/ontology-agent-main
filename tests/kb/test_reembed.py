# tests/kb/test_reembed.py
"""向量重建 v1 最小五步用例（OntRAG §8.7；KB-G1b；纯 aiosqlite 内存库，零 PG 真连）。

覆盖：plan 登记与活跃互斥、全量重嵌（备份列 + 直写 embedding + 完成转 shadow）、
断点续跑（progress.cursor 游标，崩溃后仅补剩余）、影子双读对比（重叠率差值与判据、
两列一致=0 通过 / 错位=1 拒绝）、切流状态机（幂等/非法迁移拒绝）、回滚列交换
（旧向量还原、NULL backup 行不参与、再交换=再切流）、退役状态迁移。

环境纪律：test_maintenance.py 同款——StaticPool 内存库 + @compiles 方言 shim +
document_chunks 临时补 embedding/backup_embedding 文本列（模拟迁移条件列）；
向量读写经 reembed.write_chunk_vectors（方言分支），读回用 _parse_stored_vector。

psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.ddl import CreateIndex

from services.iam.data.orm import Tenant as TenantORM  # noqa: F401 — FK 解析需 tenants 入共享 metadata
from services.kb.business.reembed import (
    _parse_stored_vector,
    cutover,
    plan_reembed,
    retire_reembed,
    rollback_reembed,
    run_reembed,
    shadow_compare,
    write_chunk_vectors,
)
from services.kb.data.maintenance_orm import KbReembedJob
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.platform.db.base import Base

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    import aiosqlite  # noqa: F401

    _HAS_AIOSQLITE = True
except ImportError:
    _HAS_AIOSQLITE = False

sqlite_needed = pytest.mark.skipif(not _HAS_AIOSQLITE, reason="aiosqlite 未安装：重嵌用例")

TENANT = uuid.uuid4()
NOW = datetime(2026, 9, 29, 4, 0)

_OLD_VEC = [9.0, 9.0]  # 旧向量（重嵌前播种）
_NEW_VEC = [1.0, 2.0]  # 桩嵌入器产出（与新向量可区分，供备份/回滚断言）


# ── SQLite 方言 shim（test_maintenance.py 同款）────────────────────────────


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSONB"


@compiles(PgUuid, "sqlite")
def _sqlite_uuid(type_: Any, compiler: Any, **kw: Any) -> str:
    return "CHAR(32)"


@compiles(CreateIndex, "sqlite")
def _sqlite_skip_pg_only_index(element: Any, compiler: Any, **kw: Any) -> Any:
    if element.element.name == "ix_document_chunks_fts":
        return ""
    return compiler.visit_create_index(element, **kw)


# ── 桩与种子 ────────────────────────────────────────────────────────────────


class _StubEmbedder:
    """桩嵌入器：恒返固定向量；fail_after=N 时第 N+1 次调用抛错（断点续跑注入点）。"""

    def __init__(self, vec: list[float] | None = None, *, fail_after: int | None = None) -> None:
        self.vec = vec or _NEW_VEC
        self.calls: list[Sequence[str]] = []
        self.fail_after = fail_after

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise RuntimeError("嵌入服务不可达（桩注入失败）")
        self.calls.append(list(texts))
        return [list(self.vec) for _ in texts]


def _doc() -> DocumentORM:
    return DocumentORM(
        id=uuid.uuid4(),  # 显式主键（客户端生成）：chunk 构造即引用（PkMixin 默认值 flush 才生成）
        tenant_id=TENANT,
        kb_collection_id=uuid.uuid4(),
        title="reembed-t",
        minio_key="raw-docs/reembed-t",
        checksum_sha256="b" * 64,
        meta={},
        status="indexed",
    )


def _chunk(doc_id: uuid.UUID, *, seq: int, content: str = "c") -> DocumentChunkORM:
    return DocumentChunkORM(tenant_id=TENANT, document_id=doc_id, seq=seq, content=content, meta={})


async def _seed(factory: async_sessionmaker[AsyncSession], instances: Sequence[Any]) -> None:
    async with factory() as db, db.begin():
        db.add_all(list(instances))
        await db.flush()


@pytest.fixture
async def kb_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """内存库 + 建表 + 临时补 embedding/backup_embedding 文本列（模拟迁移条件列）。"""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c,
                tables=[DocumentORM.__table__, DocumentChunkORM.__table__, KbReembedJob.__table__],
            )
        )
        await conn.execute(text("ALTER TABLE document_chunks ADD COLUMN embedding TEXT"))
        await conn.execute(text("ALTER TABLE document_chunks ADD COLUMN backup_embedding TEXT"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _stored(
    factory: async_sessionmaker[AsyncSession], chunk_id: uuid.UUID, column: str
) -> list[float] | None:
    async with factory() as db:
        raw = (
            await db.execute(text(f"SELECT {column} FROM document_chunks WHERE id = :id"), {"id": chunk_id.hex})
        ).scalar_one_or_none()
    return _parse_stored_vector(raw)


async def _job(factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> KbReembedJob:
    async with factory() as db:
        return (
            (await db.execute(select(KbReembedJob).where(KbReembedJob.id == job_id))).scalar_one()
        )


async def _seed_doced_chunks(
    factory: async_sessionmaker[AsyncSession], n: int, *, with_old_vectors: bool = True
) -> list[uuid.UUID]:
    """n chunk 单文档播种；with_old_vectors=True 时旧向量写入 embedding 列。"""
    doc = _doc()
    chunks = [_chunk(doc.id, seq=i, content=f"chunk-{i}") for i in range(n)]
    await _seed(factory, [doc, *chunks])
    if with_old_vectors:
        async with factory() as db, db.begin():
            await write_chunk_vectors(db, [(c.id, _OLD_VEC) for c in chunks], column="embedding")
    return [c.id for c in chunks]


# ── 步 1：plan ─────────────────────────────────────────────────────────────


@sqlite_needed
async def test_plan_登记planning任务_同租户活跃互斥(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    # Arrange：无任务
    async with kb_factory() as db:
        # Act：首次登记成功
        job = await plan_reembed(db, TENANT, "new-bge")
        # Assert：planning + progress 空
        assert job.status == "planning" and job.new_model == "new-bge" and job.progress == {}
        # Act：同租户再次登记 → 409 拒绝
        with pytest.raises(ValueError, match="活跃重嵌任务"):
            await plan_reembed(db, TENANT, "another")


# ── 步 2：全量重嵌 ─────────────────────────────────────────────────────────


@sqlite_needed
async def test_全量重嵌_备份旧向量_直写新向量_完成转shadow(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    # Arrange：3 chunk 带旧向量，登记任务
    chunk_ids = await _seed_doced_chunks(kb_factory, 3)
    async with kb_factory() as db:
        job = await plan_reembed(db, TENANT, "new-bge")
    # Act：全量重嵌
    progress = await run_reembed(kb_factory, job.id, _StubEmbedder(), batch_size=2)
    # Assert：进度收口 + 状态 shadow
    assert progress["done"] == 3 and progress["total"] == 3
    row = await _job(kb_factory, job.id)
    assert row.status == "shadow"
    # Assert：旧向量已备份、embedding 列为新向量（备份列方案的可回滚前提）
    for cid in chunk_ids:
        assert await _stored(kb_factory, cid, "backup_embedding") == _OLD_VEC
        assert await _stored(kb_factory, cid, "embedding") == _NEW_VEC


@sqlite_needed
async def test_断点续跑_崩溃后从游标续_仅补剩余chunk(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    # Arrange：4 chunk；桩在第 3 次调用（每批 1 条）失败
    await _seed_doced_chunks(kb_factory, 4)
    async with kb_factory() as db:
        job = await plan_reembed(db, TENANT, "new-bge")
    failing = _StubEmbedder(fail_after=2)
    # Act：首轮崩在第 3 chunk
    with pytest.raises(RuntimeError, match="嵌入服务不可达"):
        await run_reembed(kb_factory, job.id, failing, batch_size=1)
    # Assert：任务停在 reembedding + 进度持久（done=2、游标在位）
    row = await _job(kb_factory, job.id)
    assert row.status == "reembedding"
    assert row.progress["done"] == 2 and row.progress["cursor"]
    # Act：修复后续跑（新桩）——仅补剩余
    resumed = _StubEmbedder()
    progress = await run_reembed(kb_factory, job.id, resumed, batch_size=1)
    # Assert：完成转 shadow；续跑仅嵌剩余 2 条（断点不重做）
    assert progress["done"] == 4 and progress["total"] == 4
    assert (await _job(kb_factory, job.id)).status == "shadow"
    assert sum(len(c) for c in resumed.calls) == 2


# ── 步 3：影子双读 ─────────────────────────────────────────────────────────


async def _write_columns(
    factory: async_sessionmaker[AsyncSession],
    items: Sequence[tuple[uuid.UUID, list[float]]],
    *,
    column: str,
) -> None:
    async with factory() as db, db.begin():
        await write_chunk_vectors(db, items, column=column)


@sqlite_needed
async def test_影子对比_两列一致_diff为0通过(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    # Arrange：3 chunk，新旧两列向量一致（重嵌零漂移的理想态）
    chunk_ids = await _seed_doced_chunks(kb_factory, 3)
    same = [[1.0, 0.0], [0.0, 1.0], [0.7, 0.7]]
    await _write_columns(kb_factory, list(zip(chunk_ids, same, strict=True)), column="backup_embedding")
    await _write_columns(kb_factory, list(zip(chunk_ids, same, strict=True)), column="embedding")
    # Act
    report = await shadow_compare(kb_factory(), TENANT, ["q1", "q2"], _StubEmbedder([1.0, 0.1]), top_k=2)
    # Assert：top-k 完全重叠 → diff=0 < 2% 判据通过
    assert report.mean_overlap == 1.0 and report.diff == 0.0 and report.passed
    assert all(q.overlap == 2 for q in report.queries)


@sqlite_needed
async def test_影子对比_排序错位_diff超阈不通过(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    # Arrange：新列向量与旧列错位（chunk0 的新向量=chunk1 的旧向量）
    chunk_ids = await _seed_doced_chunks(kb_factory, 2)
    old = [[1.0, 0.0], [0.0, 1.0]]
    new = [[0.0, 1.0], [1.0, 0.0]]  # 互换 → 同查询两列 top-1 必不相同
    await _write_columns(kb_factory, list(zip(chunk_ids, old, strict=True)), column="backup_embedding")
    await _write_columns(kb_factory, list(zip(chunk_ids, new, strict=True)), column="embedding")
    # Act
    report = await shadow_compare(kb_factory(), TENANT, ["q"], _StubEmbedder([1.0, 0.0]), top_k=1)
    # Assert：top-1 零重叠 → diff=1.0 ≥ 2% 不通过（§8.7：差 ≥2% 不切流）
    assert report.mean_overlap == 0.0 and report.diff == 1.0 and not report.passed
    assert report.queries[0].new_hits != report.queries[0].old_hits


@sqlite_needed
async def test_影子对比_空查询拒绝(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    with pytest.raises(ValueError, match="sample_queries 为空"):
        await shadow_compare(kb_factory(), TENANT, [], _StubEmbedder())


# ── 步 4/5：切流 / 回滚 / 退役 ─────────────────────────────────────────────


@sqlite_needed
async def test_切流状态机_shadow进cutover_幂等_planning拒绝(
    kb_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Arrange：A 租户任务完成到 shadow；B 任务停在 planning
    await _seed_doced_chunks(kb_factory, 1)
    async with kb_factory() as db:
        job = await plan_reembed(db, TENANT, "new-bge")
        fresh = KbReembedJob(tenant_id=uuid.uuid4(), status="planning", new_model="m", progress={})
        db.add(fresh)
        await db.commit()
    await run_reembed(kb_factory, job.id, _StubEmbedder(), batch_size=4)
    # Act/Assert：shadow → cutover；重复切流幂等
    async with kb_factory() as db:
        assert await cutover(db, job.id) == "cutover"
        assert await cutover(db, job.id) == "cutover"
        # planning（未重嵌）不可切流
        with pytest.raises(ValueError, match="不可迁移"):
            await cutover(db, fresh.id)


@sqlite_needed
async def test_回滚列交换_旧向量还原_backup持有新向量_NULL_backup行不动(
    kb_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Arrange：2 chunk 完成重嵌；另 1 chunk 迁移窗口新写（backup=NULL、仅新向量）
    chunk_ids = await _seed_doced_chunks(kb_factory, 2)
    async with kb_factory() as db:
        job = await plan_reembed(db, TENANT, "new-bge")
    await run_reembed(kb_factory, job.id, _StubEmbedder(), batch_size=4)
    window_doc = _doc()
    window_chunk = _chunk(window_doc.id, seq=99)
    await _seed(kb_factory, [window_doc, window_chunk])
    async with kb_factory() as db, db.begin():
        await write_chunk_vectors(db, [(window_chunk.id, _NEW_VEC)], column="embedding")
    # Act：回滚（列交换）
    swapped = await rollback_reembed(kb_factory, job.id)
    # Assert：旧向量还原到 embedding；新向量移入 backup（可再交换=再切流）
    assert swapped == 2
    for cid in chunk_ids:
        assert await _stored(kb_factory, cid, "embedding") == _OLD_VEC
        assert await _stored(kb_factory, cid, "backup_embedding") == _NEW_VEC
    assert (await _job(kb_factory, job.id)).status == "rolled_back"
    # Assert：窗口期 chunk（backup=NULL）不参与交换，保持新向量
    assert await _stored(kb_factory, window_chunk.id, "embedding") == _NEW_VEC
    assert await _stored(kb_factory, window_chunk.id, "backup_embedding") is None
    # Assert：rolled_back 终态不可再回滚（再切流入口随 v1.5 状态机扩展登记）
    with pytest.raises(ValueError, match="不可回滚"):
        await rollback_reembed(kb_factory, job.id)


@sqlite_needed
async def test_退役状态迁移_cutover与rolled_back进retired(
    kb_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Arrange：两条任务分别到 cutover / rolled_back
    await _seed_doced_chunks(kb_factory, 1)
    async with kb_factory() as db:
        job = await plan_reembed(db, TENANT, "new-bge")
    await run_reembed(kb_factory, job.id, _StubEmbedder(), batch_size=4)
    async with kb_factory() as db:
        await cutover(db, job.id)
    # Act/Assert
    async with kb_factory() as db:
        assert await retire_reembed(db, job.id) == "retired"
        rolled = KbReembedJob(tenant_id=uuid.uuid4(), status="rolled_back", new_model="m", progress={})
        db.add(rolled)
        await db.commit()
        assert await retire_reembed(db, rolled.id) == "retired"

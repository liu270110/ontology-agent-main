# tests/kb/test_kb_nightly.py
"""kb nightly 编排用例（OntRAG §8.4 v1 收缩范围；KB-G1b；纯 aiosqlite 内存库，零 PG 真连）。

覆盖：Redis 锁互斥（桩 Redis——锁忙 skipped 零写零扫 / 真抢锁-释放生命周期 / token
防误删）、补嵌增量与幂等（仅 indexed 文档缺向量 chunk；重跑收敛）、预算上限截断
（超限顺延 stats.deferred）、催办阈值集成（14 天入催办、30 天归档，经 maintenance
委托）、悬空出处进 stats.dangling_refs、无嵌入模型跳过不失败、游标落账
（kb_maintenance_runs 行 finished/stats/watermark）。

时间口径：naive datetime 全程一致（test_maintenance.py 同款）。

psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta
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
from services.kb.business.kb_nightly import LOCK_KEY, RedisNightlyLock, run_nightly
from services.kb.business.reembed import _parse_stored_vector
from services.kb.data.maintenance_orm import KbMaintenanceRun
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbFact as KbFactORM
from services.platform.config import Settings
from services.platform.db.base import Base

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    import aiosqlite  # noqa: F401

    _HAS_AIOSQLITE = True
except ImportError:
    _HAS_AIOSQLITE = False

sqlite_needed = pytest.mark.skipif(not _HAS_AIOSQLITE, reason="aiosqlite 未安装：nightly 落库用例")

NOW = datetime(2026, 9, 29, 3, 0)  # naive 全程一致（test_maintenance.py 同款时间口径）
TENANT = uuid.uuid4()


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


# ── 桩 Redis（SET NX EX / GET / EXPIRE / DELETE 最小面）与桩嵌入器 ──────────


class _FakeAsyncRedis:
    """桩 Redis：dict 存储共享（多实例=多 worker 视角）；不模拟真实过期（用例不依赖）。"""

    def __init__(self, store: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = store if store is not None else {}
        self.expiries: dict[str, int] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.expiries[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def expire(self, key: str, ttl: int) -> bool:
        if key not in self.store:
            return False
        self.expiries[key] = ttl
        return True

    async def delete(self, key: str) -> int:
        self.expiries.pop(key, None)
        return int(self.store.pop(key, None) is not None)


class _StubEmbedder:
    """桩嵌入器：恒返 [len(text), 1.0]（确定性；调用留痕供断言）。"""

    def __init__(self) -> None:
        self.calls: list[Sequence[str]] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(t)), 1.0] for t in texts]


class _StubLock:
    """桩锁（maintenance 测试同款）：ok=False 模拟他 worker 持锁。"""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.acquire_calls = 0
        self.release_calls = 0

    async def acquire(self) -> bool:
        self.acquire_calls += 1
        return self.ok

    async def release(self) -> None:
        self.release_calls += 1


# ── 种子构造（悬空出处可借 SQLite 无 FK 强制特性显式注入）──────────────────


def _doc(doc_id: uuid.UUID | None = None, *, status: str = "indexed") -> DocumentORM:
    return DocumentORM(
        id=doc_id or uuid.uuid4(),  # 显式主键：chunk 构造即引用（PkMixin 默认值 flush 才生成）
        tenant_id=TENANT,
        kb_collection_id=uuid.uuid4(),
        title="t",
        minio_key="raw-docs/t",
        checksum_sha256="a" * 64,
        meta={},
        status=status,
    )


def _chunk(doc_id: uuid.UUID, *, seq: int = 0, content: str = "c") -> DocumentChunkORM:
    return DocumentChunkORM(tenant_id=TENANT, document_id=doc_id, seq=seq, content=content, meta={})


def _fact(
    created_at: datetime,
    *,
    status: str = "candidate",
    subject: str = "S",
    source_ref: dict[str, Any] | None = None,
) -> KbFactORM:
    if source_ref is None:
        source_ref = {}  # 免随机悬空引用污染告警计数（test_maintenance 同款）
    return KbFactORM(
        tenant_id=TENANT,
        document_id=uuid.uuid4(),
        fact_type="entity",
        subject=subject,
        confidence=0.9,
        status=status,
        evidence={"source_ref": source_ref},
        violations=[],
        meta={},
        created_at=created_at,
    )


def _d(days: int) -> datetime:
    return NOW - timedelta(days=days)


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
                tables=[
                    DocumentORM.__table__,
                    DocumentChunkORM.__table__,
                    KbFactORM.__table__,
                    KbMaintenanceRun.__table__,
                ],
            )
        )
        await conn.execute(text("ALTER TABLE document_chunks ADD COLUMN embedding TEXT"))
        await conn.execute(text("ALTER TABLE document_chunks ADD COLUMN backup_embedding TEXT"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _runs(factory: async_sessionmaker[AsyncSession]) -> list[KbMaintenanceRun]:
    async with factory() as db:
        return (await db.execute(select(KbMaintenanceRun).order_by(KbMaintenanceRun.started_at))).scalars().all()


async def _stored_embedding(factory: async_sessionmaker[AsyncSession], chunk_id: uuid.UUID) -> list[float] | None:
    async with factory() as db:
        raw = (
            await db.execute(text("SELECT embedding FROM document_chunks WHERE id = :id"), {"id": chunk_id.hex})
        ).scalar_one_or_none()
    return _parse_stored_vector(raw)


# ── 互斥（§8.4 纪律 3）────────────────────────────────────────────────────


@sqlite_needed
async def test_锁忙_直接skipped_零写零扫(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    # Arrange：他 worker 持锁
    lock = _StubLock(ok=False)
    # Act
    report = await run_nightly(kb_factory, now=NOW, lock=lock)
    # Assert：skipped 报告原样返回，无运行账本行（零写零扫）
    assert report.skipped and "lock_busy" in (report.skip_reason or "")
    assert await _runs(kb_factory) == []
    assert lock.release_calls == 0  # 拿锁失败不释放（未持有；maintenance 同款口径）


@sqlite_needed
async def test_真实锁抢占_第二worker被拒_释放后可再跑(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    # Arrange：worker A 经桩 Redis 持锁
    store: dict[str, str] = {}
    redis_a = _FakeAsyncRedis(store)
    lock_a = RedisNightlyLock(redis_a, ttl_seconds=3600)
    assert await lock_a.acquire()
    # Act：worker B（共享存储的第二个客户端视角）抢锁失败
    lock_b = RedisNightlyLock(_FakeAsyncRedis(store), ttl_seconds=3600)
    assert not await lock_b.acquire()
    # Assert：B 的失败释放不得误删 A 的锁（token 防误删）
    await lock_b.release()
    assert LOCK_KEY in store
    # Act：A 释放后 B 可抢
    await lock_a.release()
    assert LOCK_KEY not in store
    assert await lock_b.acquire()
    await lock_b.release()


@sqlite_needed
async def test_运行结束锁自动释放_串行两轮均可执行(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    # Arrange：共享存储的两个客户端（同存储=互斥可观测）
    store: dict[str, str] = {}
    settings = Settings(kb_nightly_lock_ttl_seconds=3600)
    # Act：两轮串行运行（各自新建锁客户端；空库+桩嵌入器 → 两轮均零补嵌）
    first = await run_nightly(
        kb_factory,
        now=NOW,
        embedder=_StubEmbedder(),
        lock=RedisNightlyLock(_FakeAsyncRedis(store), ttl_seconds=3600),
        settings=settings,
    )
    second = await run_nightly(
        kb_factory,
        now=NOW,
        embedder=_StubEmbedder(),
        lock=RedisNightlyLock(_FakeAsyncRedis(store), ttl_seconds=3600),
        settings=settings,
    )
    # Assert：两轮均真实运行（每轮结束 finally 释放），且第二轮幂等收敛
    assert not first.skipped and not second.skipped
    assert second.stats["reembedded"] == 0
    assert len(await _runs(kb_factory)) == 2


# ── a) 增量重编译：缺嵌补嵌（§8.4 a / §8.6 同口径）─────────────────────────


@sqlite_needed
async def test_补嵌_仅indexed文档缺向量chunk_重跑幂等游标落账(
    kb_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Arrange：indexed 文档 2 缺嵌 chunk；uploaded 文档 1 chunk（未终审不补）；indexed 已嵌 1 chunk（不重嵌）
    indexed_doc = _doc(status="indexed")
    pending = [_chunk(indexed_doc.id, seq=i, content=f"p{i}") for i in range(2)]
    uploaded_doc = _doc(status="uploaded")
    uploaded_chunk = _chunk(uploaded_doc.id, seq=0, content="u")
    done_doc = _doc(status="indexed")
    done_chunk = _chunk(done_doc.id, seq=0, content="done")
    await _seed(kb_factory, [indexed_doc, *pending, uploaded_doc, uploaded_chunk, done_doc, done_chunk])
    async with kb_factory() as db, db.begin():
        await db.execute(
            text("UPDATE document_chunks SET embedding = '[9.0,9.0]' WHERE id = :id"),
            {"id": done_chunk.id.hex},
        )
    # Act：首轮补嵌
    report = await run_nightly(kb_factory, now=NOW, embedder=_StubEmbedder(), lock=_StubLock())
    # Assert：只补 2 条缺嵌（uploaded 未终审排除、已嵌跳过）
    assert report.stats["reembedded"] == 2 and report.stats["reembed_deferred"] == 0
    for chunk in pending:
        assert await _stored_embedding(kb_factory, chunk.id) == [2.0, 1.0]  # len("p0")=2
    assert await _stored_embedding(kb_factory, done_chunk.id) == [9.0, 9.0]
    # Assert：游标落账（watermark=末次补嵌 chunk id；事件驱动 v1.5 留位）
    (run_row,) = await _runs(kb_factory)
    assert run_row.finished_at == NOW and run_row.watermark_event_id is not None
    assert run_row.stats["reembedded"] == 2 and run_row.stats["token_budget"] > 0
    # Act：同 now 重跑 → 幂等收敛（补后非 NULL 退出选择集）
    again = await run_nightly(kb_factory, now=NOW, embedder=_StubEmbedder(), lock=_StubLock())
    # Assert
    assert again.stats["reembedded"] == 0
    assert len(await _runs(kb_factory)) == 2


@sqlite_needed
async def test_预算上限_超限顺延记deferred(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    # Arrange：3 缺嵌 chunk，预算 1
    doc = _doc(status="indexed")
    await _seed(kb_factory, [doc, *[_chunk(doc.id, seq=i) for i in range(3)]])
    settings = Settings(kb_nightly_max_items_per_run=1)
    # Act
    report = await run_nightly(kb_factory, now=NOW, embedder=_StubEmbedder(), lock=_StubLock(), settings=settings)
    # Assert：只补 1 条；剩余 2 条顺延记 stats.deferred（§8.4 纪律 1）
    assert report.stats["reembedded"] == 1
    assert report.stats["reembed_deferred"] == 2 and report.stats["deferred"] == 2


@sqlite_needed
async def test_未配嵌入模型_补嵌跳过不失败(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    # Arrange：缺嵌 chunk 存在但无 embedder
    doc = _doc(status="indexed")
    await _seed(kb_factory, [doc, _chunk(doc.id, seq=0)])
    # Act
    report = await run_nightly(kb_factory, now=NOW, embedder=None, lock=_StubLock())
    # Assert：跳过原因入 stats（诚实降级，不 mock）
    assert report.stats["recompile_skipped"] == "no_embedder"
    assert "reembedded" not in report.stats


# ── b/c) 催办 + 归档 + 悬空出处（委托 maintenance，§8.4 b/c v1）────────────


@sqlite_needed
async def test_催办与归档阈值集成_14天催_30天归档_入stats与结构化日志(
    kb_factory: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange：15 天（催办带）/ 31 天（自动归档）/ 3 天（不催）各一条候选
    await _seed(
        kb_factory,
        [_fact(_d(15), subject="催办对象"), _fact(_d(31), subject="归档对象"), _fact(_d(3), subject="新候选")],
    )
    # Act
    with caplog.at_level("WARNING"):
        report = await run_nightly(kb_factory, now=NOW, embedder=None, lock=_StubLock())
    # Assert：催办入 stats + 结构化日志（通知通道接 event_sink 登记遗留）
    assert report.stats["remind_groups"] == 1 and report.stats["remind_facts"] == 1
    assert report.maintenance is not None and report.maintenance.reminded[0].subject == "催办对象"
    assert any("催办" in r.message for r in caplog.records)
    # Assert：30 天自动归档走既有状态口径（rejected + meta.maintenance 留痕）
    assert report.stats["archived"] == 1
    async with kb_factory() as db:
        archived = (await db.execute(select(KbFactORM).where(KbFactORM.subject == "归档对象"))).scalar_one()
    assert archived.status == "rejected"
    assert archived.meta["maintenance"]["action"] == "auto_archived"
    # Assert：3 天候选不受影响
    async with kb_factory() as db:
        fresh = (await db.execute(select(KbFactORM).where(KbFactORM.subject == "新候选"))).scalar_one()
    assert fresh.status == "candidate"


@sqlite_needed
async def test_悬空出处告警_进stats_dangling_refs(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    # Arrange：权威事实指向不存在文档（悬空）
    await _seed(
        kb_factory,
        [
            _fact(NOW, status="authoritative", subject="悬空", source_ref={"document_id": str(uuid.uuid4())}),
            _fact(NOW, status="authoritative", subject="健康"),
        ],
    )
    # Act
    report = await run_nightly(kb_factory, now=NOW, embedder=None, lock=_StubLock())
    # Assert：纯 SQL 廉价项汇总入 stats.dangling_refs
    assert report.stats["dangling_refs"] == 1
    assert report.maintenance is not None and len(report.maintenance.issues) == 1


@sqlite_needed
async def test_空库运行_零动作零告警_账本照落(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    # Act
    report = await run_nightly(kb_factory, now=NOW, embedder=_StubEmbedder(), lock=_StubLock())
    # Assert：空库口径（reembedded=0 计入 stats；maintenance 明细空报告）
    assert report.stats["reembedded"] == 0 and report.stats["dangling_refs"] == 0
    assert report.stats["archived"] == 0 and report.stats["remind_groups"] == 0
    assert len(await _runs(kb_factory)) == 1

# tests/kb/test_usage_counters.py
"""kb 知识活性埋点用例（多源接入设计 §6.1；aiosqlite 内存库 + 纯桩，零 PG）。

背景红线：共享 PG 连接池被并行会话耗尽——本文件测试主体一律不触真实 PG：
- 落库用例跑 aiosqlite 内存库（生产同一 ORM/会话路径；JSONB/UUID 经本文件 @compiles shim
  降编译到 SQLite DDL，不影响 PG 方言；upsert ON CONFLICT 语法两方言一致，单 SQL 路径不变）；
- search 挂钩用例纯桩：hybrid_search / 类层次打桩，验证 citations → usage 埋点调度（含
  store 抛错不伤主链路、usage_store 缺省 None 零行为变化）。

隐私边界回归：计数表只有计数与时间戳列——不记"谁查了什么"（§6.1）。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUuid
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from services.kb.business import search_service as search_service_mod
from services.kb.business.search_service import KnowledgeSearchService
from services.kb.business.usage_service import UsageStore
from services.kb.data.usage_orm import KbUsageCounter
from services.kb.retrieval.graph import build_class_hierarchy
from services.kb.retrieval.retrieve import SearchHit
from services.platform.db.base import Base

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ── SQLite 方言 shim（仅测试进程：PG 专列类型建表降编译；绑定/结果处理走通用 Uuid 路径）──


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSONB"


@compiles(PgUuid, "sqlite")
def _sqlite_uuid(type_: Any, compiler: Any, **kw: Any) -> str:
    return "CHAR(32)"


TENANT = uuid.uuid4()
KB = uuid.uuid4()
KB_OTHER = uuid.uuid4()


# ── 夹具（aiosqlite 内存库：StaticPool 单连接共享，建表与会话同库）─────────────────


@pytest.fixture
async def kb_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[KbUsageCounter.__table__]))
    yield engine
    await engine.dispose()


@pytest.fixture
async def kb_factory(kb_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield async_sessionmaker(kb_engine, expire_on_commit=False)


@pytest.fixture
async def store(kb_factory: async_sessionmaker[AsyncSession]) -> UsageStore:
    return UsageStore(kb_factory)


async def _all_rows(factory: async_sessionmaker[AsyncSession]) -> list[KbUsageCounter]:
    async with factory() as session:
        return list((await session.execute(select(KbUsageCounter).order_by(KbUsageCounter.chunk_id))).scalars().all())


# ── UsageStore：计数 upsert ──────────────────────────────────────────────────


async def test_upsert首建_计数一_时间戳落列_隐私无内容列(store: UsageStore, kb_factory: Any) -> None:
    c1, c2 = uuid.uuid4(), uuid.uuid4()
    await store.record_search_hits(tenant_id=TENANT, kb_collection_id=KB, chunk_ids=[c1, c2])

    rows = await _all_rows(kb_factory)
    assert {(r.tenant_id, r.kb_collection_id, r.chunk_id) for r in rows} == {(TENANT, KB, c1), (TENANT, KB, c2)}
    for row in rows:
        assert row.search_hits == 1  # 首建即 1（写入时即有，零成本埋点）
        assert row.action_refs == 0
        assert row.last_searched_at is not None
        assert row.last_action_at is None  # action 通道未接（v1 预留列）
        assert row.created_at is not None and row.updated_at is not None
    # 隐私边界：表全列只有维度/计数/时间戳，无"谁查了什么"内容级列
    assert {c.name for c in KbUsageCounter.__table__.columns} == {
        "id",
        "tenant_id",
        "kb_collection_id",
        "chunk_id",
        "search_hits",
        "action_refs",
        "last_searched_at",
        "last_action_at",
        "created_at",
        "updated_at",
    }


async def test_upsert递增_不重复建行_时间戳推进(store: UsageStore, kb_factory: Any) -> None:
    c1 = uuid.uuid4()
    await store.record_search_hits(tenant_id=TENANT, kb_collection_id=KB, chunk_ids=[c1])
    first_ts = (await _all_rows(kb_factory))[0].last_searched_at
    assert first_ts is not None

    await asyncio.sleep(0.001)
    await store.record_search_hits(tenant_id=TENANT, kb_collection_id=KB, chunk_ids=[c1])
    rows = await _all_rows(kb_factory)
    assert len(rows) == 1  # uk 吸收：不重复建行
    assert rows[0].search_hits == 2  # +1 原子递增
    assert rows[0].last_searched_at is not None and rows[0].last_searched_at >= first_ts


async def test_批量一次往返_单条insert语句(store: UsageStore, kb_engine: AsyncEngine, kb_factory: Any) -> None:
    statements: list[str] = []
    event.listen(
        kb_engine.sync_engine,
        "before_cursor_execute",
        lambda conn, cursor, stmt, params, context, executemany: statements.append(stmt),
    )
    chunk_ids = [uuid.uuid4() for _ in range(3)]
    await store.record_search_hits(tenant_id=TENANT, kb_collection_id=KB, chunk_ids=chunk_ids)

    assert len(statements) == 1  # N 个 chunk 一次往返（禁止逐条 N 次）
    assert statements[0].lstrip().upper().startswith("INSERT")
    assert "ON CONFLICT" in statements[0].upper()
    rows = await _all_rows(kb_factory)
    assert len(rows) == 3 and all(r.search_hits == 1 for r in rows)


async def test_top_usage_降序_限量_租户库隔离(store: UsageStore) -> None:
    hot, warm_a, warm_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    other = uuid.uuid4()
    await store.record_search_hits(tenant_id=TENANT, kb_collection_id=KB, chunk_ids=[warm_a])
    await store.record_search_hits(tenant_id=TENANT, kb_collection_id=KB, chunk_ids=[hot])
    await store.record_search_hits(tenant_id=TENANT, kb_collection_id=KB, chunk_ids=[hot])
    await store.record_search_hits(tenant_id=TENANT, kb_collection_id=KB, chunk_ids=[warm_b])
    await store.record_search_hits(tenant_id=TENANT, kb_collection_id=KB_OTHER, chunk_ids=[other])  # 异库
    await store.record_search_hits(tenant_id=uuid.uuid4(), kb_collection_id=KB, chunk_ids=[uuid.uuid4()])  # 异租户

    top2 = await store.top_usage(tenant_id=TENANT, kb_collection_id=KB, limit=2)
    assert [u.chunk_id for u in top2] == [hot, min(warm_a, warm_b)]  # 降序 + 同分按 chunk_id 稳定序
    assert top2[0].search_hits == 2 and top2[1].search_hits == 1
    assert other not in {u.chunk_id for u in top2}  # 租户/库隔离

    assert await store.top_usage(tenant_id=TENANT, kb_collection_id=uuid.uuid4(), limit=5) == []  # 空库=空非失败


# ── search 挂钩：埋点不伤主链路（纯桩）───────────────────────────────────────


class _NullSession:
    """零 SQL 会话桩：hierarchy 打桩 + ACL 关闭 + source_context=None 时 search 不触会话。"""

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _make_service(usage_store: Any = None) -> KnowledgeSearchService:
    service = KnowledgeSearchService(
        _NullSession,  # type: ignore[arg-type]  纯桩：search 主链路不触会话（见 _NullSession）
        ollama_base_url="http://localhost:0",
        acl_filter_enabled=False,
        usage_store=usage_store,
    )

    async def _empty_hierarchy(db: Any, tenant_id: uuid.UUID) -> Any:
        return build_class_hierarchy(iter([]))

    service._class_hierarchy = _empty_hierarchy  # type: ignore[method-assign]  测试打桩
    return service


def _fake_hits() -> list[SearchHit]:
    return [
        SearchHit(chunk_id=uuid.uuid4(), document_id=uuid.uuid4(), content="a", score=0.9),
        SearchHit(chunk_id=uuid.uuid4(), document_id=uuid.uuid4(), content="b", score=0.8),
    ]


@pytest.fixture
def patch_hybrid(monkeypatch: pytest.MonkeyPatch) -> list[SearchHit]:
    hits = _fake_hits()

    async def _fake_hybrid(query: str, **kwargs: Any) -> Any:
        return SimpleNamespace(
            query=query, degraded=False, degraded_reasons=[], channels=["bm25"], hits=hits, graph_paths=[]
        )

    monkeypatch.setattr(search_service_mod, "hybrid_search", _fake_hybrid)
    return hits


async def test_search挂钩_注入store_citations计数落行(
    kb_factory: Any, patch_hybrid: list[SearchHit]
) -> None:
    store = UsageStore(kb_factory)
    service = _make_service(usage_store=store)
    result = await service.search(tenant_id=TENANT, query="q", kb_id=KB)

    assert [c.chunk_id for c in result.citations] == [h.chunk_id for h in patch_hybrid]  # 主链路产物不变
    for task in list(service._usage_tasks):
        await task  # join fire-and-forget 埋点任务
    rows = await _all_rows(kb_factory)
    assert {r.chunk_id for r in rows} == {h.chunk_id for h in patch_hybrid}
    assert all(r.search_hits == 1 for r in rows)


async def test_search挂钩_kb_id缺省跨库不埋点(kb_factory: Any, patch_hybrid: list[SearchHit]) -> None:
    store = UsageStore(kb_factory)
    service = _make_service(store)
    await service.search(tenant_id=TENANT, query="q", kb_id=None)  # 跨库检索无法零成本归因
    for task in list(service._usage_tasks):
        await task
    assert await _all_rows(kb_factory) == []  # 零归因零埋点（宁缺勿错）


async def test_search挂钩_store抛错不影响主链路_DEBUG留痕(
    kb_factory: Any, patch_hybrid: list[SearchHit], caplog: pytest.LogCaptureFixture
) -> None:
    class _BoomStore:
        async def record_search_hits(self, **kwargs: Any) -> None:
            raise RuntimeError("usage db down")

    service = _make_service(_BoomStore())
    with caplog.at_level(logging.DEBUG, logger="services.kb.business.search_service"):
        result = await service.search(tenant_id=TENANT, query="q", kb_id=KB)
        for task in list(service._usage_tasks):
            await task  # join 留在 at_level 块内：DEBUG 记录须在级别还原前产生并捕获

    assert [c.chunk_id for c in result.citations] == [h.chunk_id for h in patch_hybrid]  # 检索主链路无损
    assert any("usage 埋点失败" in r.getMessage() for r in caplog.records)  # DEBUG 留痕
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)  # 统计失败不升级不打扰


async def test_search挂钩_缺省无store_零行为变化(patch_hybrid: list[SearchHit]) -> None:
    service = _make_service()  # usage_store 缺省 None：组合根零改动
    result = await service.search(tenant_id=TENANT, query="q", kb_id=KB)
    assert [c.chunk_id for c in result.citations] == [h.chunk_id for h in patch_hybrid]
    assert not service._usage_tasks  # 零调度零行为变化

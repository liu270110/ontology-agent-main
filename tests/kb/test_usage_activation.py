# tests/kb/test_usage_activation.py
"""usage 埋点三路激活 + chat 链路 ACL 透传用例（问题清单 A3+C1；多源接入设计 §6.1）。

A3 激活（UsageStore 组合根接线——此前仅 search_service 可选注入、全部组合根不注入）：
- REST：POST /kb/search 处理尾部 fire-and-forget record_search_hits（路由函数级直调，
  桩 UsageStore 记录调用；kb_id=None 跨库零归因零埋点；store 抛错不伤检索主链路）；
- chat：build_chat_context_assembler 组合根工厂注入 usage_store（构造后 service 持有非 None）；
- 消费方：kb_nightly → maintenance ④ 零引用清理候选计数进 report（live chunk 满 90 天且
  usage 表缺行/全零；只报告不动数据）。

C1 修复（chat 链路 ACL 透传）：ChatContextAssembler.assemble 携带调用方 acl 标签面 →
KnowledgeSearchService.search acl_tags 透传（桩检索回调断言参数）；缺省 None=现状 no-op
（不改变默认关闭语义，只修「开关开了也不生效」的断链）。

环境纪律：纯 aiosqlite 内存库 + 纯桩，零 PG 真连、零网络（test_usage_counters.py /
test_maintenance.py 同款 shim 与窗口策略；SQLite 无 FK 强制，chunk 引用可悬空同先例）。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUuid
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.ddl import CreateIndex

from services.agent.business.chat_context import ChatContextAssembler, build_chat_context_assembler
from services.iam.data.orm import Tenant as TenantORM  # noqa: F401 — FK 解析入共享 metadata
from services.kb.api.kb import search as kb_search_route
from services.kb.api.schemas.kb import KbSearchIn, KbSearchOut
from services.kb.business.kb_nightly import run_nightly
from services.kb.business.maintenance import MaintenanceReport, run_kb_maintenance
from services.kb.business.search_service import KnowledgeSearchResult
from services.kb.data.maintenance_orm import KbMaintenanceRun
from services.kb.data.orm import Document as DocumentORM  # 零引用 SQL JOIN documents（valid_to 封口过滤）
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbFact as KbFactORM  # noqa: F401 — 建表入 metadata（maintenance 扫描面）
from services.kb.data.usage_orm import KbUsageCounter
from services.kb.retrieval.embed import EmbeddingUnavailableError
from services.kb.retrieval.graph import build_class_hierarchy
from services.platform.config import Settings
from services.platform.db.base import Base
from services.platform.deps import Principal

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ── SQLite 方言 shim（test_usage_counters.py / test_maintenance.py 同款，幂等重注册无害）──


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSONB"


@compiles(PgUuid, "sqlite")
def _sqlite_uuid(type_: Any, compiler: Any, **kw: Any) -> str:
    return "CHAR(32)"


@compiles(CreateIndex, "sqlite")
def _sqlite_skip_pg_only_index(element: Any, compiler: Any, **kw: Any) -> Any:
    """PG 专用 BM25 GIN 函数索引 SQLite 跳过建 DDL（test_maintenance 先例）。"""
    if element.element.name == "ix_document_chunks_fts":
        return ""
    return compiler.visit_create_index(element, **kw)


TENANT = uuid.uuid4()
KB = uuid.uuid4()
NOW = datetime(2026, 10, 4, 3, 0)  # naive 全程一致（SQLite 往返同值；时间口径同 test_maintenance）


# ── 通用桩 ───────────────────────────────────────────────────────────────────


class _RecordingStore:
    """埋点桩：记录 record_search_hits 调用参数（REST 尾部 fire-and-forget 接线断言口）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def record_search_hits(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class _BoomStore:
    """埋点桩：调用必抛（吞异常 + DEBUG 留痕纪律断言口）。"""

    async def record_search_hits(self, **kwargs: Any) -> None:
        raise RuntimeError("usage db down")


class _DegradedEmbedder:
    """嵌入桩：调用必抛 → hybrid 内建 vector→bm25 降级（test_acl_search_api 同口径）。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailableError("埋点激活用例：嵌入路注入降级")


class _RecordingKnowledge:
    """检索服务桩：记录 search 调用 kwargs（assemble acl_tags 透传断言口）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def search(self, **kwargs: Any) -> KnowledgeSearchResult:
        self.calls.append(kwargs)
        return KnowledgeSearchResult(query=str(kwargs.get("query", "")), citations=[])


class _BrokenL1:
    """L1 桩：read 必抛 → 记忆面降级（透传用例只关注检索面，chat_context 降级链承载）。"""

    async def read(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> Any:
        raise RuntimeError("Redis 不可达（模拟）")


@asynccontextmanager
async def _null_session_factory() -> AsyncIterator[None]:
    yield None


class _AlwaysAcquireLock:
    """锁桩：恒可获取（nightly 编排用例绕开 Redis）。"""

    async def acquire(self) -> bool:
        return True

    async def release(self) -> None:
        return None


# ── 夹具（aiosqlite 内存库：StaticPool 单连接共享，建表与会话同库）─────────────────


@pytest.fixture
async def kb_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c,
                tables=[
                    KbUsageCounter.__table__,
                    DocumentORM.__table__,
                    DocumentChunkORM.__table__,
                    KbFactORM.__table__,
                    KbMaintenanceRun.__table__,  # nightly 编排游标落行（§8.4 纪律 2）
                ],
            )
        )
    yield engine
    await engine.dispose()


@pytest.fixture
async def kb_factory(kb_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield async_sessionmaker(kb_engine, expire_on_commit=False)


# ══ ① REST 端点直调（路由函数级）：POST /kb/search 尾部 fire-and-forget 埋点 ══


def _bm25_rows() -> list[dict]:
    """bm25 臂桩返回行（dict 形状同检索 SQL 投影；vector 臂嵌入桩必抛 → BM25-only 降级）。"""
    return [
        {
            "chunk_id": uuid.uuid4(),
            "document_id": uuid.uuid4(),
            "content": "feeder F100 巡检手册",
            "score": 0.9,
            "doc_name": "巡检手册",
            "minio_key": None,
            "span": [0, 10],
        },
        {
            "chunk_id": uuid.uuid4(),
            "document_id": uuid.uuid4(),
            "content": "feeder F200 停电工单",
            "score": 0.8,
            "doc_name": "停电工单",
            "minio_key": None,
            "span": [0, 10],
        },
    ]


def _patch_retrieval(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> None:
    """路由模块内桩：bm25 三路命中 + 类层次空闭包（REST search 全链其余真实执行）。"""

    async def _fake_bm25(session: Any, **kwargs: Any) -> list[dict]:
        return rows

    async def _fake_hierarchy(request: Any, session: Any, tenant_id: uuid.UUID, version: Any = None) -> Any:
        return build_class_hierarchy(iter([]))

    monkeypatch.setattr("services.kb.api.kb.bm25_search", _fake_bm25)
    monkeypatch.setattr("services.kb.api.kb._class_hierarchy", _fake_hierarchy)


def _principal() -> Principal:
    return Principal(
        {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(TENANT),
            "roles": ["viewer"],
            "scopes": ["kb:read"],
            "typ": "access",
            "jti": f"test-{uuid.uuid4().hex[:8]}",
        }
    )


def _rest_request(store: Any, settings: Any | None = None) -> SimpleNamespace:
    """路由签名所需最小 Request 替身：预挂桩 store 命中 app.state 缓存（不建真实引擎）。"""
    return SimpleNamespace(
        headers={},
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=settings or Settings(kb_acl_filter_enabled=False),
                _kb_embedder=_DegradedEmbedder(),
                _kb_usage_store=store,
            )
        ),
    )


async def _join_usage_tasks(request: SimpleNamespace) -> None:
    for task in list(getattr(request.app.state, "_kb_usage_tasks", set())):
        await task  # join fire-and-forget 埋点任务（断言前收敛）


async def test_REST检索尾部_埋点store被调_kb与citations一致(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _bm25_rows()
    _patch_retrieval(monkeypatch, rows)
    store = _RecordingStore()
    request = _rest_request(store)
    out = await kb_search_route(
        body=KbSearchIn(query="feeder", kb_id=KB, with_evidence=False),
        principal=_principal(),
        request=request,  # type: ignore[arg-type]
        session=object(),  # type: ignore[arg-type]  桩后零 SQL 面（ACL 关闭 + 软路由 None）
    )
    await _join_usage_tasks(request)

    assert isinstance(out, KbSearchOut)
    assert {str(c.chunk_id) for c in out.citations} == {str(r["chunk_id"]) for r in rows}  # 主链路产物不变
    assert len(store.calls) == 1  # 一次检索恰好一次调度
    call = store.calls[0]
    assert call["kb_collection_id"] == KB and call["tenant_id"] == TENANT
    assert set(call["chunk_ids"]) == {r["chunk_id"] for r in rows}  # citations 同源 chunk 入计


async def test_REST跨库检索_kb_id缺省_零埋点(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_retrieval(monkeypatch, _bm25_rows())
    store = _RecordingStore()
    request = _rest_request(store)
    await kb_search_route(
        body=KbSearchIn(query="feeder", kb_id=None, with_evidence=False),
        principal=_principal(),
        request=request,  # type: ignore[arg-type]
        session=object(),  # type: ignore[arg-type]
    )
    await _join_usage_tasks(request)
    assert store.calls == []  # 跨库无法零成本归因 → 零动作（宁缺勿错，search_service 同口径）


async def test_REST埋点store抛错_不伤检索主链路_DEBUG留痕(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_retrieval(monkeypatch, _bm25_rows())
    request = _rest_request(_BoomStore())
    with caplog.at_level(logging.DEBUG, logger="services.gateway.kb"):  # api/kb.py logger 实名（模块 L169）
        out = await kb_search_route(
            body=KbSearchIn(query="feeder", kb_id=KB, with_evidence=False),
            principal=_principal(),
            request=request,  # type: ignore[arg-type]
            session=object(),  # type: ignore[arg-type]
        )
        await _join_usage_tasks(request)

    assert len(out.citations) == 2  # 检索主链路无损
    assert any("usage 埋点失败" in r.getMessage() for r in caplog.records)  # DEBUG 留痕
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)  # 统计失败不升级不打扰


async def test_REST埋点store构造失败_本轮零埋点_不伤检索主链路(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """app.state 未预挂 store 且 settings 不可哈希（缺省构造路径炸）→ DEBUG 吞掉，检索照常。

    回归：端点级用例的 SimpleNamespace settings 曾使 get_engine 缓存键炸（TypeError 外溢
    伤主链路）——埋点是旁路增强，构造环境缺失=本轮零埋点（调度同步段守卫）。
    """
    _patch_retrieval(monkeypatch, _bm25_rows())
    # 不可哈希桩 settings（get_engine 缓存键 TypeError）+ 未预挂 store → 缺省构造必炸（确定性触发）
    unhashable_settings = SimpleNamespace(
        ollama_base_url="http://localhost:9", kb_acl_filter_enabled=False, embed_protocol="ollama"
    )
    request = _rest_request(store=None, settings=unhashable_settings)
    with caplog.at_level(logging.DEBUG, logger="services.gateway.kb"):
        out = await kb_search_route(
            body=KbSearchIn(query="feeder", kb_id=KB, with_evidence=False),
            principal=_principal(),
            request=request,  # type: ignore[arg-type]
            session=object(),  # type: ignore[arg-type]
        )
        await _join_usage_tasks(request)

    assert len(out.citations) == 2  # 检索主链路无损
    assert any("usage 埋点调度失败" in r.getMessage() for r in caplog.records)  # DEBUG 留痕
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)  # 不升级不打扰


# ══ ② chat 组合根装配断言：build_chat_context_assembler 注入 usage_store ══


def test_chat组合根_usage_store注入装配非空() -> None:
    assembler = build_chat_context_assembler(
        l1_store=SimpleNamespace(),  # type: ignore[arg-type]  构造期仅存引用（Protocol 桩）
        session_factory=_null_session_factory,  # type: ignore[arg-type]
        ollama_base_url="http://localhost:0",
    )
    # A3：组合根注入后检索服务持有 usage_store（此前全部组合根不注入=chat 链路恒零埋点）
    assert assembler._knowledge._usage_store is not None  # type: ignore[union-attr]


# ══ ③ C1：assemble 调用方 acl 标签面 → search acl_tags 透传 ══


def _assembler(knowledge: _RecordingKnowledge) -> ChatContextAssembler:
    return ChatContextAssembler(
        l1_store=_BrokenL1(),  # type: ignore[arg-type]  记忆面降级承载（本组用例只关注检索透传）
        session_factory=_null_session_factory,  # type: ignore[arg-type]
        knowledge=knowledge,  # type: ignore[arg-type]
        repo_factory=lambda db, tenant: SimpleNamespace(),  # type: ignore[arg-type,return-value]
    )


async def test_assemble_acl_tags透传_search收到调用方标签面() -> None:
    knowledge = _RecordingKnowledge()
    assembler = _assembler(knowledge)
    ctx = await assembler.assemble(
        tenant_id=TENANT,
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        query="线路A停电原因",
        acl_tags=["dept:power", "dept:gridops"],
    )

    assert len(knowledge.calls) == 1
    assert knowledge.calls[0]["acl_tags"] == ["dept:power", "dept:gridops"]  # 标签面原样透传（C1 断链修复）
    assert ctx.evidence is not None  # 检索面照常返回（透传不改变检索主链路）


async def test_assemble缺省无标签面_search收到None_现状no_op() -> None:
    knowledge = _RecordingKnowledge()
    assembler = _assembler(knowledge)
    await assembler.assemble(
        tenant_id=TENANT, user_id=uuid.uuid4(), session_id=uuid.uuid4(), query="q"
    )  # 不传 acl_tags

    assert knowledge.calls[0]["acl_tags"] is None  # 缺省 None=调用方未接入标签面 no-op（默认关闭语义不变）


# ══ ④ 消费方激活：maintenance ④ 零引用清理候选 → nightly report ══


async def _seed_zero_ref_env(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, uuid.UUID]:
    """四 chunk 对照组：老缺行（计入）/ 老全零行（计入）/ 老有命中（不计）/ 新缺行（不计）。

    chunk 挂真实 Document 行（零引用 SQL JOIN documents 做封口过滤——悬空 chunk 归悬空出处
    告警面管辖，不属零引用清理候选口径）。
    """
    old = NOW - timedelta(days=100)
    fresh = NOW - timedelta(days=1)
    stale_no_usage = uuid.uuid4()  # 满 90 天 + usage 缺行 → 清理候选
    stale_zero_hits = uuid.uuid4()  # 满 90 天 + usage 行 search_hits=0 → 清理候选
    stale_hit = uuid.uuid4()  # 满 90 天 + search_hits>0 → 活性知识，不候选
    fresh_no_usage = uuid.uuid4()  # 未满期 → 观察期，不候选
    doc_id = uuid.uuid4()
    async with factory() as session, session.begin():
        session.add(
            DocumentORM(
                id=doc_id,
                tenant_id=TENANT,
                kb_collection_id=KB,
                title="零引用对照组",
                minio_key="raw-docs/zero-ref",
                checksum_sha256="a" * 64,
                meta={},
            )
        )
        for seq, (cid, created_at) in enumerate(
            (
                (stale_no_usage, old),
                (stale_zero_hits, old),
                (stale_hit, old),
                (fresh_no_usage, fresh),
            )
        ):
            session.add(
                DocumentChunkORM(
                    id=cid,
                    tenant_id=TENANT,
                    document_id=doc_id,
                    seq=seq,  # uk(document_id, seq)：同文档递增
                    content="c",
                    meta={},
                    created_at=created_at,
                )
            )
        session.add(
            KbUsageCounter(
                tenant_id=TENANT, kb_collection_id=KB, chunk_id=stale_zero_hits, search_hits=0
            )  # 全零行（§6.1「计数缺行/全零」两形态之一）
        )
        session.add(
            KbUsageCounter(
                tenant_id=TENANT, kb_collection_id=KB, chunk_id=stale_hit, search_hits=3
            )  # 活性命中行
        )
    return {
        "stale_no_usage": stale_no_usage,
        "stale_zero_hits": stale_zero_hits,
        "stale_hit": stale_hit,
        "fresh_no_usage": fresh_no_usage,
    }


async def test_零引用清理候选_满期缺行或全零计数_只报告不动数据(
    kb_factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed_zero_ref_env(kb_factory)
    report: MaintenanceReport = await run_kb_maintenance(kb_factory, now=NOW, zero_ref_days=90)

    assert report.stats["zero_ref_candidates"] == 2  # 老缺行 + 老全零行；活性/未满期不计
    # 只报告不动数据（v1）：chunk 行与 usage 行原样保留
    async with kb_factory() as session:
        chunk_ids = {
            row[0] for row in (await session.execute(select(DocumentChunkORM.id))).all()
        }
        usage_rows = (await session.execute(select(KbUsageCounter))).scalars().all()
    assert chunk_ids == set(ids.values())
    assert {(u.chunk_id, u.search_hits) for u in usage_rows} == {
        (ids["stale_zero_hits"], 0),
        (ids["stale_hit"], 3),
    }


async def test_零引用候选_天数阈值边界_严格超期才计(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    """「超 N 天」= age > N 严格口径（恰满不计=清理候选宁保守；91 天计入、恰 90 天不计）。"""
    doc_id = uuid.uuid4()
    async with kb_factory() as session, session.begin():
        session.add(
            DocumentORM(
                id=doc_id,
                tenant_id=TENANT,
                kb_collection_id=KB,
                title="阈值边界",
                minio_key="raw-docs/threshold",
                checksum_sha256="b" * 64,
                meta={},
            )
        )
        session.add(
            DocumentChunkORM(
                id=uuid.uuid4(),
                tenant_id=TENANT,
                document_id=doc_id,
                seq=0,
                content="c",
                meta={},
                created_at=NOW - timedelta(days=91),
            )
        )
        session.add(
            DocumentChunkORM(
                id=uuid.uuid4(),
                tenant_id=TENANT,
                document_id=doc_id,
                seq=1,
                content="c",
                meta={},
                created_at=NOW - timedelta(days=90),  # 恰满 90 天（不「超」）→ 不计
            )
        )
    report = await run_kb_maintenance(kb_factory, now=NOW, zero_ref_days=90)
    assert report.stats["zero_ref_candidates"] == 1  # 仅 91 天者（严格超期）


async def test_nightly编排_零引用项收进report_stats(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    await _seed_zero_ref_env(kb_factory)
    nightly = await run_nightly(
        kb_factory,
        now=NOW,
        embedder=None,  # 未配嵌入模型 → 补嵌跳过（不 mock，零网络）
        lock=_AlwaysAcquireLock(),  # 桩锁：绕开 Redis
        settings=Settings(kb_nightly_max_items_per_run=100, kb_nightly_lock_ttl_seconds=60),
    )

    assert nightly.skipped is False
    assert nightly.stats["zero_ref_candidates"] == 2  # maintenance ④ 计数透传进 nightly stats
    assert nightly.maintenance is not None
    assert nightly.maintenance.stats["zero_ref_candidates"] == 2  # 明细报告同键可对账

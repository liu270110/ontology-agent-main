# tests/kb/test_acl_prefilter.py
"""ACL 预过滤开关用例（OntRAG §4.3；13 篇 M5-2 收缩裁决「配置开关化（无迁移方案）」）。

断言目标：
- 开关 false → 零行为变化：不下推探测、SQL 不含 acl 谓词与绑定参数（红线）；
- 开关 true + acl_tags 列缺失 → 自动 no-op 并 DEBUG 留痕（迁移容错，vector_ready 同款）；
- 开关 true + 列存在（用例内临时建列，非迁移文件）→ 三路（BM25/向量/图）同源谓词过滤生效：
  未标注文档继承租户全员可见、异标签文档被拒、空标签面仅租户继承文档可见（deny-by-default）。

环境纪律：纯函数/桩用例零外部依赖；集成用例直连本地 PG（不可达即跳过）。
psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.retrieval.embed import AclPushdown, bm25_search, vector_search
from services.kb.retrieval.graph import ClassHierarchy, expand_graph
from services.kb.retrieval.retrieve import SearchHit
from services.platform.config import Settings
from services.platform.db import registry as orm_registry  # noqa: F401  全模块 ORM 入 metadata（FK 解析）

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

TENANT = uuid.uuid4()

# BM25 'simple' 配置按空格分词（中文分词随 M3+；tests/mcp/test_standalone_e2e.py 同口径）
SEEDS = {
    "public": "feeder F100 public manual inspection steps",
    "power": "feeder F200 power outage ticket dispatch workflow",
    "finance": "feeder F300 finance billing contract settlement",
}

_ADD_COLUMN_DDL = text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS acl_tags jsonb")
_DROP_COLUMN_DDL = text("ALTER TABLE documents DROP COLUMN IF EXISTS acl_tags")


# ── 桩会话（探测可编程 / SQL 捕获）────────────────────────────────────────


class _StubResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        return self._value


class ProbeStubSession:
    """execute 可编程桩：information_schema 探测返回预设值；业务 SQL 记入 captured 供断言。"""

    def __init__(self, *, column_exists: bool) -> None:
        self.column_exists = column_exists
        self.captured: list[tuple[str, dict]] = []

    async def execute(self, clause: object, params: dict | None = None) -> _StubResult:
        sql = str(clause)
        if "information_schema" in sql:
            return _StubResult(self.column_exists)
        self.captured.append((sql, dict(params or {})))
        raise AssertionError(f"桩会话不应执行业务 SQL: {sql}")

    async def __aenter__(self) -> ProbeStubSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class CapturingSession:
    """捕获 bm25_search 编译 SQL 与参数的桩（零行为变化断言口；恒返空结果）。"""

    def __init__(self) -> None:
        self.sql = ""
        self.params: dict = {}

    async def execute(self, clause: object, params: dict | None = None) -> Any:
        self.sql = str(clause)
        self.params = dict(params or {})
        return type("_Rows", (), {"mappings": staticmethod(lambda: [])})()


# ── AclPushdown 三态（零外部依赖）────────────────────────────────────────


async def test_开关关闭_零行为变化_不下推不探测不拼谓词() -> None:
    """红线：enabled=False → 空片段零参数、不触发列存在性探测（零行为变化）。"""
    session = ProbeStubSession(column_exists=True)
    pushdown = await AclPushdown.prepare(session, enabled=False, allowed_tags=["dept:power"])
    assert pushdown.fragment == ""
    assert pushdown.params == {}
    assert session.captured == []  # 未下发任何探测/业务 SQL


async def test_开关开启列缺失_自动no_op并DEBUG留痕(caplog: pytest.LogCaptureFixture) -> None:
    """无迁移部署：列存在性探测为 False → no-op + DEBUG 留痕（开关开启不生效的降级面）。"""
    session = ProbeStubSession(column_exists=False)
    with caplog.at_level(logging.DEBUG, logger="services.kb.retrieval.acl"):
        pushdown = await AclPushdown.prepare(session, enabled=True, allowed_tags=["dept:power"])
    assert pushdown.fragment == "" and pushdown.params == {}
    assert any("acl_tags 列缺失" in r.message for r in caplog.records)


async def test_开关开启列存在但调用方未声明标签面_no_op() -> None:
    """allowed_tags=None（调用方未接入标签面）→ 无过滤依据，不激活谓词。"""
    session = ProbeStubSession(column_exists=True)
    pushdown = await AclPushdown.prepare(session, enabled=True, allowed_tags=None)
    assert pushdown.fragment == "" and pushdown.params == {}


async def test_开关开启列存在且标签面给出_谓词激活() -> None:
    """激活态：片段含 acl_tags 谓词 + 绑定参数为标签数组（deny-by-default 语义）。"""
    session = ProbeStubSession(column_exists=True)
    pushdown = await AclPushdown.prepare(session, enabled=True, allowed_tags=["dept:power", "dept:grid"])
    assert "acl_tags" in pushdown.fragment
    assert pushdown.params == {"acl_tags": ["dept:power", "dept:grid"]}


async def test_bm25_开关关闭_SQL不含acl谓词与参数() -> None:
    """开关关闭（acl=None）时 bm25_search 编译 SQL 不含 acl_tags，参数键集与存量一致。"""
    session = CapturingSession()
    await bm25_search(session, tenant_id=TENANT, query="馈线 F100", top_k=5)
    assert "acl_tags" not in session.sql
    assert set(session.params) == {"tenant_id", "query", "top_k", "collection_id", "as_of"}


# ── 集成用例：列存在（用例内临时建列）→ 过滤生效 ──────────────────────────


@pytest.fixture
async def acl_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """本地 PG 会话工厂；不可达即跳过（同 tests/kb 夹具纪律）。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过 ACL 预过滤集成用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def acl_seeded(acl_pg: async_sessionmaker[AsyncSession]) -> AsyncIterator[dict]:
    """独立租户 + 三文档（public 未标注 / power 标注 / finance 异标签）各单 chunk；用例内临时建列。"""
    async with acl_pg() as db, db.begin():
        await db.execute(_ADD_COLUMN_DDL)  # 测试自管 DDL（用例内临时建列，非迁移文件；结束即撤）
        tenant = TenantORM(name="acl-it-租户", slug=f"acl-it-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="acl-it-库", embedding_model="bge-m3")
        db.add(collection)
        await db.flush()
        doc_ids: dict[str, uuid.UUID] = {}
        for key, content in SEEDS.items():
            doc = DocumentORM(
                tenant_id=tenant.id,
                kb_collection_id=collection.id,
                title=f"acl-it-{key}",
                source_type="upload",
                size_bytes=len(content.encode()),
                minio_key=f"raw-docs/{tenant.id}/{collection.id}/{uuid.uuid4()}/source.md",
                checksum_sha256=hashlib.sha256(content.encode()).hexdigest(),
                meta={"content": content},
                status="indexed",
            )
            db.add(doc)
            await db.flush()
            doc_ids[key] = doc.id
            if key != "public":  # 未标注文档 = 继承租户全员（§4.3 缺省口径）；列未入 ORM，raw SQL 标注
                tag = ["dept:power"] if key == "power" else ["dept:finance"]
                await db.execute(
                    text("UPDATE documents SET acl_tags = CAST(:tags AS jsonb) WHERE id = :doc_id"),
                    {"tags": json.dumps(tag), "doc_id": doc.id},
                )
            db.add(
                DocumentChunkORM(
                    tenant_id=tenant.id,
                    document_id=doc.id,
                    seq=0,
                    content=content,
                    token_count=len(content) // 2,
                    meta={"span": [0, len(content)]},
                )
            )
    env = {"tenant_id": tenant.id, "collection_id": collection.id, "doc_ids": doc_ids}
    yield env
    async with acl_pg() as db, db.begin():  # FK 逆序清理 + 撤临时列
        for stmt in (
            delete(DocumentChunkORM).where(DocumentChunkORM.tenant_id == env["tenant_id"]),
            delete(DocumentORM).where(DocumentORM.tenant_id == env["tenant_id"]),
            delete(KbCollectionORM).where(KbCollectionORM.id == env["collection_id"]),
            delete(TenantORM).where(TenantORM.id == env["tenant_id"]),
            _DROP_COLUMN_DDL,
        ):
            await db.execute(stmt)


def _visible_doc_ids(rows: list[dict]) -> set[str]:
    return {str(r["document_id"]) for r in rows}


def _seed_doc_ids(env: dict) -> dict[str, str]:
    return {key: str(doc_id) for key, doc_id in env["doc_ids"].items()}


async def test_bm25路_ACL过滤_未标注放行_命中标签放行_异标签拒绝(
    acl_pg: async_sessionmaker[AsyncSession], acl_seeded: dict
) -> None:
    db = acl_pg()
    pushdown = await AclPushdown.prepare(db, enabled=True, allowed_tags=["dept:power"])
    rows = await bm25_search(db, tenant_id=acl_seeded["tenant_id"], query="feeder", top_k=10, acl=pushdown)
    await db.close()
    visible = _visible_doc_ids(rows)
    doc_ids = _seed_doc_ids(acl_seeded)
    assert doc_ids["public"] in visible  # 未标注=继承租户全员（§4.3 缺省口径）
    assert doc_ids["power"] in visible  # 标签面命中
    assert doc_ids["finance"] not in visible  # 异标签文档被拒


async def test_bm25路_空标签面仅租户继承文档可见(acl_pg: async_sessionmaker[AsyncSession], acl_seeded: dict) -> None:
    """deny-by-default：空标签面调用方只见未标注文档。"""
    db = acl_pg()
    pushdown = await AclPushdown.prepare(db, enabled=True, allowed_tags=[])
    rows = await bm25_search(db, tenant_id=acl_seeded["tenant_id"], query="feeder", top_k=10, acl=pushdown)
    await db.close()
    assert _visible_doc_ids(rows) == {_seed_doc_ids(acl_seeded)["public"]}


async def test_开关关闭时同库检索_标注文档照常返回_零行为变化(
    acl_pg: async_sessionmaker[AsyncSession], acl_seeded: dict
) -> None:
    """红线：开关关闭（acl=None）→ 已标注文档不被过滤（存量行为不变）。"""
    db = acl_pg()
    rows = await bm25_search(db, tenant_id=acl_seeded["tenant_id"], query="feeder", top_k=10)
    await db.close()
    assert len(_visible_doc_ids(rows)) == 3


async def test_向量路_ACL过滤生效(acl_pg: async_sessionmaker[AsyncSession], acl_seeded: dict) -> None:
    """向量路同源谓词（pgvector/embedding 列不可用 → 既有降级契约，跳过不伪造成功向量）。"""
    db = acl_pg()
    probe = await db.execute(
        text(
            "SELECT to_regtype('vector') IS NOT NULL AND EXISTS ("
            " SELECT 1 FROM information_schema.columns"
            " WHERE table_name = 'document_chunks' AND column_name = 'embedding')"
        )
    )
    if not bool(probe.scalar()):
        await db.close()
        pytest.skip("pgvector 扩展或 embedding 列不可用，跳过向量路 ACL 用例")
    pushdown = await AclPushdown.prepare(db, enabled=True, allowed_tags=["dept:power"])
    rows = await vector_search(
        db,
        tenant_id=acl_seeded["tenant_id"],
        query_embedding=[0.1] * 1024,
        top_k=10,
        acl=pushdown,
    )
    visible = _visible_doc_ids(rows)
    await db.close()
    doc_ids = _seed_doc_ids(acl_seeded)
    assert doc_ids["finance"] not in visible
    assert {doc_ids["public"], doc_ids["power"]} <= visible


async def test_图路_ACL过滤_邻接扩展排除异标签文档(acl_pg: async_sessionmaker[AsyncSession], acl_seeded: dict) -> None:
    """图路（LazyGraphRAG lite）邻接查询同源谓词：finance 文档不进扩展命中。"""
    db = acl_pg()
    chunk_rows = (
        (await db.execute(select(DocumentChunkORM).where(DocumentChunkORM.tenant_id == acl_seeded["tenant_id"])))
        .scalars()
        .all()
    )
    seeds = [SearchHit(chunk_id=row.id, document_id=row.document_id, content=row.content) for row in chunk_rows]
    hierarchy = ClassHierarchy(
        names={"http://o/设备": "设备"},
        parents={"http://o/设备": ()},
        children={"http://o/设备": ()},
    )
    pushdown = await AclPushdown.prepare(db, enabled=True, allowed_tags=["dept:power"])
    expansion = await expand_graph(
        db,
        tenant_id=acl_seeded["tenant_id"],
        collection_id=acl_seeded["collection_id"],
        seeds=seeds,
        hierarchy=hierarchy,
        max_hops=1,
        acl=pushdown,
    )
    await db.close()
    doc_ids = _seed_doc_ids(acl_seeded)
    hit_docs = {str(hit.document_id) for hit in expansion.hits}
    assert doc_ids["finance"] not in hit_docs


async def test_会话工厂按配置开关透传_关闭时三路零过滤(
    acl_pg: async_sessionmaker[AsyncSession], acl_seeded: dict
) -> None:
    """KnowledgeSearchService 开关透传：缺省读统一配置层（默认 false）→ 标注文档照常返回。"""
    from services.kb.business.search_service import KnowledgeSearchService

    service = KnowledgeSearchService(acl_pg, ollama_base_url="http://localhost:9")  # acl_filter_enabled=None
    result = await service.search(
        tenant_id=acl_seeded["tenant_id"], query="feeder", kb_id=acl_seeded["collection_id"], top_k=10
    )
    assert len(result.citations) == 3  # 向量路 Ollama 不可达自动降级 BM25-only（既有降级链）

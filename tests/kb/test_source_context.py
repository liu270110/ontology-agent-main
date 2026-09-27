# tests/kb/test_source_context.py
"""knowledge.search source_context 软路由用例（多源接入与连接器设计 §5.2 v1 口径）。

断言目标（知识库 GraphRAG 设计 §11 待办 v1 裁量：软路由不硬过滤，分组返回随 v1.5）：
- 纯函数：匹配 meta.source_system 加权排前、异源降权不剔除、未标注中性；
- 零行为变化：source_context 为空 → 重排助手原样返回且不下发任何 SQL（向后兼容红线）；
- 服务级：双源文档（meta.source_system=PMS/ERP）同查询命中——source_context="PMS" 时
  PMS 引用排前且异源保留；source_context=None 维持相关度序（ERP 查询词频高排前）；
- 端点层：KbSearchIn.source_context 透传冒烟（直调路由函数；tests 无 TestClient 先例）。

环境纪律：纯函数/桩用例零外部依赖；集成用例直连本地 PG（不可达即跳过）。
psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略。
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from fastapi import Request
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.kb.api.kb import search as kb_search_route
from services.kb.api.schemas.kb import KbSearchIn
from services.kb.business.search_service import (
    KnowledgeSearchService,
    apply_source_context_weights,
    rerank_hits_by_source_context,
)
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.retrieval.retrieve import SearchHit
from services.platform.config import Settings
from services.platform.db import registry as orm_registry  # noqa: F401  全模块 ORM 入 metadata（FK 解析）
from services.platform.deps import Principal

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# BM25 'simple' 配置按空格分词（中文分词随 M3+；tests/kb 同口径）：ERP 内容查询词频更高
# → ts_rank 词频主导 → 相关度更高（source_context=None 的基线序）；PMS 弱命中可被软路由翻转
QUERY = "feeder outage"
SEEDS = {
    "pms": "feeder outage inspection note for the monthly review",  # feeder×1 outage×1（弱命中）
    "erp": "outage feeder dispatch ticket outage feeder workflow outage",  # outage×3 feeder×2（强命中）
}


class _RejectingSession:
    """execute 即失败的桩：source_context 为空时重排助手不得下发任何 SQL（零行为变化断言口）。"""

    async def execute(self, clause: object, params: dict | None = None) -> object:
        raise AssertionError(f"source_context 为空不应执行 SQL: {clause}")


# ── 纯函数 / 桩 / 请求模型用例（零外部依赖）───────────────────────────────


def test_纯函数_匹配加权排前_异源降权不剔除_未标注中性() -> None:
    pms_doc, erp_doc, plain_doc = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    hits = [  # 初始相关度序：erp(0.030) > pms(0.020) > plain(0.010)
        SearchHit(chunk_id=uuid.uuid4(), document_id=erp_doc, content="erp", score=0.030),
        SearchHit(chunk_id=uuid.uuid4(), document_id=pms_doc, content="pms", score=0.020),
        SearchHit(chunk_id=uuid.uuid4(), document_id=plain_doc, content="plain", score=0.010),
    ]
    systems = {pms_doc: "PMS", erp_doc: "ERP", plain_doc: None}
    out = apply_source_context_weights(hits, systems, source_context="PMS")
    assert [hit.document_id for hit in out] == [pms_doc, erp_doc, plain_doc]  # pms×1.5=0.03 翻转登顶
    assert out[0].score == pytest.approx(0.030)
    assert len(out) == 3  # 软路由不硬过滤：异源与未标注文档全部保留


async def test_source_context为空_原样返回_零SQL零行为变化() -> None:
    hits = [SearchHit(chunk_id=uuid.uuid4(), document_id=uuid.uuid4(), content="x", score=0.1)]
    out = await rerank_hits_by_source_context(_RejectingSession(), hits, source_context=None)
    assert out == hits  # 引用序与 score 均不变（向后兼容红线）


def test_请求模型_source_context可选_缺省None() -> None:
    assert KbSearchIn.model_validate({"query": "x"}).source_context is None
    assert KbSearchIn.model_validate({"query": "x", "source_context": "PMS"}).source_context == "PMS"


# ── 集成用例：本地 PG（不可达即跳过；夹具纪律同 tests/kb/test_acl_prefilter.py）─────────


@pytest.fixture
async def sc_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """本地 PG 会话工厂；不可达即跳过（同 tests/kb 夹具纪律）。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过 source_context 软路由集成用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def sc_seeded(sc_pg: async_sessionmaker[AsyncSession]) -> AsyncIterator[dict]:
    """独立租户 + 单库 + 双源文档（meta.source_system=PMS/ERP，上传方写入口径）各单 chunk。"""
    async with sc_pg() as db, db.begin():
        tenant = TenantORM(name="sc-it-租户", slug=f"sc-it-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="sc-it-库", embedding_model="bge-m3")
        db.add(collection)
        await db.flush()
        doc_ids: dict[str, uuid.UUID] = {}
        for key, content in SEEDS.items():
            doc = DocumentORM(
                tenant_id=tenant.id,
                kb_collection_id=collection.id,
                title=f"sc-it-{key}",
                source_type="upload",
                size_bytes=len(content.encode()),
                minio_key=f"raw-docs/{tenant.id}/{collection.id}/{uuid.uuid4()}/source.md",
                checksum_sha256=hashlib.sha256(content.encode()).hexdigest(),
                meta={"content": content, "source_system": key.upper()},
                status="indexed",
            )
            db.add(doc)
            await db.flush()
            doc_ids[key] = doc.id
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
    async with sc_pg() as db, db.begin():  # FK 逆序清理
        for stmt in (
            delete(DocumentChunkORM).where(DocumentChunkORM.tenant_id == env["tenant_id"]),
            delete(DocumentORM).where(DocumentORM.tenant_id == env["tenant_id"]),
            delete(KbCollectionORM).where(KbCollectionORM.id == env["collection_id"]),
            delete(TenantORM).where(TenantORM.id == env["tenant_id"]),
        ):
            await db.execute(stmt)


async def test_source_contextPMS_PMS文档引用排前_异源不剔除(
    sc_pg: async_sessionmaker[AsyncSession], sc_seeded: dict
) -> None:
    """软路由正例：source_context="PMS" → PMS 引用排前；ERP 降序保留（不硬过滤）。"""
    service = KnowledgeSearchService(sc_pg, ollama_base_url="http://localhost:9")  # 向量路降级 BM25-only
    result = await service.search(
        tenant_id=sc_seeded["tenant_id"],
        query=QUERY,
        kb_id=sc_seeded["collection_id"],
        top_k=10,
        source_context="PMS",
    )
    doc_ids = {key: str(doc_id) for key, doc_id in sc_seeded["doc_ids"].items()}
    assert len(result.citations) == 2  # 双文档同查询命中且异源不剔除
    assert result.citations[0].doc_id == uuid.UUID(doc_ids["pms"])  # 匹配源排前
    assert {c.doc_id for c in result.citations} == {uuid.UUID(doc_ids["pms"]), uuid.UUID(doc_ids["erp"])}


async def test_source_context为空_维持相关度序_ERP排前(
    sc_pg: async_sessionmaker[AsyncSession], sc_seeded: dict
) -> None:
    """零行为变化基线：不传 source_context → 引用按 BM25 相关度原序（ERP 词频高排前）。"""
    service = KnowledgeSearchService(sc_pg, ollama_base_url="http://localhost:9")
    result = await service.search(
        tenant_id=sc_seeded["tenant_id"], query=QUERY, kb_id=sc_seeded["collection_id"], top_k=10
    )
    doc_ids = {key: str(doc_id) for key, doc_id in sc_seeded["doc_ids"].items()}
    assert [c.doc_id for c in result.citations] == [uuid.UUID(doc_ids["erp"]), uuid.UUID(doc_ids["pms"])]


def _principal(tenant_id: uuid.UUID) -> Principal:
    return Principal({"sub": str(uuid.uuid4()), "tenant_id": str(tenant_id), "typ": "user", "jti": "test-jti"})


def _fake_request() -> Request:
    """最小 Request 桩：app.state 挂 settings（Ollama 9 端口不可达 → 端点向量路自动降级）。"""
    state = SimpleNamespace(
        settings=SimpleNamespace(ollama_base_url="http://localhost:9", kb_acl_filter_enabled=False)
    )  # kb_acl_filter_enabled=False：本用例不测 ACL，开关关闭=零行为变化（develop 端点融合后新增读取）
    return Request({"type": "http", "app": SimpleNamespace(state=state)})


async def test_端点层_source_context透传冒烟_PMS排前(
    sc_pg: async_sessionmaker[AsyncSession], sc_seeded: dict
) -> None:
    """直调 POST /kb/search 路由函数：KbSearchIn.source_context → 软路由重排 → citations 序。"""
    async with sc_pg() as session:
        body = KbSearchIn(query=QUERY, kb_id=sc_seeded["collection_id"], top_k=10, source_context="PMS")
        out = await kb_search_route(
            body=body, principal=_principal(sc_seeded["tenant_id"]), request=_fake_request(), session=session
        )
    doc_ids = {key: str(doc_id) for key, doc_id in sc_seeded["doc_ids"].items()}
    assert len(out.citations) == 2  # 异源不剔除（软路由）
    assert out.citations[0].doc_id == uuid.UUID(doc_ids["pms"])  # 参数已透传至重排

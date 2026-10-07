"""K16 G-14 pgvector L0 摘要零读直出（docs/Agent/13 §22；openviking@23 §3）。

断言目标：
- 写侧：embed 步同点生成确定性前缀摘要（min(200, len) 前缀 + strip；空内容 → None），
  随嵌入同 UPDATE 落列（set_chunk_embeddings 三元组）；存量行不回填——已嵌入行不触碰、
  重嵌入（向量缺失再补）时自然生成；
- 读侧：_CHUNK_FIELDS 同一条 SQL 带回（零额外查询），hit 结构（SearchHit）summary 可空
  透出（默认 None 向后兼容：glossary/graph 等其他路命中不带）；旧行 NULL 兼容不崩。

环境纪律：纯函数用例零外部依赖；集成用例直连本地 PG（不可达即跳过，tests/kb 同款夹具纪律；
pgvector 不可用则写侧/向量读侧跳过——bm25 读侧仅依赖 summary 列随迁移在场）。
BM25 种子按空格分词（'simple' 配置，tests/kb/test_acl_prefilter.py 同口径）。
psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略。
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.kb.api.kb import _dict_to_hit as api_dict_to_hit
from services.kb.business.kb_pipeline import run_pipeline
from services.kb.business.search_service import _dict_to_hit as svc_dict_to_hit
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.orm import KbPipelineStep as KbPipelineStepORM
from services.kb.retrieval.embed import (
    EMBED_DIM,
    OllamaEmbedder,
    bm25_search,
    chunk_summary,
    set_chunk_embeddings,
    vector_ready,
    vector_search,
)
from services.kb.retrieval.retrieve import SearchHit
from services.platform.config import Settings
from services.platform.db import registry as orm_registry  # noqa: F401  全模块 ORM 入 metadata（FK 解析）

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ---------------------------------------------------------------- 纯函数（零外部依赖）


def test_写侧_前缀摘要生成与200截断() -> None:
    """确定性前缀摘要：min(200, len) 截断 + strip；恰好 200 不截。"""
    assert chunk_summary("A" * 250) == "A" * 200  # 超长 → 前 200 字符
    assert chunk_summary("B" * 200) == "B" * 200  # 恰好 200 → 原样
    assert chunk_summary("  停电处置手册  ") == "停电处置手册"  # 首尾空白 strip
    assert chunk_summary(" " * 250 + "尾巴") is None  # 前 200 全空白 → strip 后为空 → None
    assert chunk_summary("短内容") == "短内容"


def test_写侧_空内容_摘要None() -> None:
    """空内容契约：None / 空串 / 全空白 → None（空摘要落 NULL 列）。"""
    assert chunk_summary(None) is None
    assert chunk_summary("") is None
    assert chunk_summary("   \n\t  ") is None


def test_SearchHit缺省_summaryNone_其他路向后兼容() -> None:
    """DTO 向后兼容：glossary/graph 等其他路命中不带 summary（默认 None 不破坏构造面）。"""
    hit = SearchHit(chunk_id=uuid.uuid4(), document_id=uuid.uuid4(), content="graph-hit")
    assert hit.summary is None
    assert hit.channels == []


# ---------------------------------------------------------------- 集成（本地 PG；不可达即跳过）


@pytest.fixture
async def kb_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """本地 PG 会话工厂；不可达即跳过整用例（tests/kb/test_kb.py 同款夹具）。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过 chunk summary 集成用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def kb_doc(kb_pg: async_sessionmaker[AsyncSession]) -> AsyncIterator[dict]:
    """每用例独立租户/集合/文档 + 手工 chunk 行（绕过 chunk 步，内容全可控），FK 逆序清理。

    种子统一带空格 token（'feeder ...'）：BM25 'simple' 按空格分词，共用 token 保证三行同查可召回。
    """
    content = "手工播种内容（本用例不走 chunk 步，直接插 chunk 行）"
    async with kb_pg() as db, db.begin():
        tenant = TenantORM(name="k16-租户", slug=f"k16-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="k16-库", embedding_model="bge-m3")
        db.add(collection)
        await db.flush()
        doc = DocumentORM(
            tenant_id=tenant.id,
            kb_collection_id=collection.id,
            title="k16-文档",
            source_type="upload",
            size_bytes=len(content.encode()),
            minio_key=f"raw-docs/{tenant.id}/{collection.id}/{uuid.uuid4()}/source.md",
            checksum_sha256=hashlib.sha256(content.encode()).hexdigest(),
            meta={"content": content},
            status="preprocessed",
        )
        db.add(doc)
        await db.flush()
        seeds = [
            ("feeder " + "A" * 250, 0),  # 超长：摘要应截到前 200 字符
            ("feeder outage manual 停电处置手册", 1),  # 短文本：全量前缀（len < 200）
            ("feeder 前导空白\n", 2),  # strip 边界（尾换行剥离）
        ]
        for chunk_content, seq in seeds:
            db.add(
                DocumentChunkORM(
                    tenant_id=tenant.id,
                    document_id=doc.id,
                    seq=seq,
                    content=chunk_content,
                    token_count=len(chunk_content) // 2,
                    meta={"span": [0, len(chunk_content)]},
                )
            )
    ids = {"tenant_id": tenant.id, "collection_id": collection.id, "document_id": doc.id}
    yield ids
    async with kb_pg() as db, db.begin():  # FK 逆序清理（pipeline checkpoint 行先于 documents）
        for stmt in (
            delete(DocumentChunkORM).where(DocumentChunkORM.tenant_id == ids["tenant_id"]),
            delete(KbPipelineStepORM).where(KbPipelineStepORM.tenant_id == ids["tenant_id"]),
            delete(DocumentORM).where(DocumentORM.tenant_id == ids["tenant_id"]),
            delete(KbCollectionORM).where(KbCollectionORM.tenant_id == ids["tenant_id"]),
            delete(TenantORM).where(TenantORM.id == ids["tenant_id"]),
        ):
            await db.execute(stmt)


def _stub_embedder() -> OllamaEmbedder:
    """确定性桩嵌入：恒返同向 1024 维（真 UPDATE pgvector 列；不做 HTTP）。"""
    embedder = OllamaEmbedder("http://localhost:9", timeout=0.1)

    async def _embed(texts: list[str]) -> list[list[float]]:
        return [[0.05] * EMBED_DIM for _ in texts]

    embedder.embed = _embed  # type: ignore[method-assign]
    return embedder


async def _require_vector_ready(kb_pg: async_sessionmaker[AsyncSession]) -> None:
    """pgvector 扩展 + embedding 列任一缺失 → 跳过（写侧与向量读侧前提）。"""
    async with kb_pg() as db:
        if not await vector_ready(db):
            pytest.skip("pgvector/embedding 列不可用，跳过写侧用例")


async def _chunk_rows(kb_pg: async_sessionmaker[AsyncSession], document_id: uuid.UUID) -> list[dict]:
    """raw SQL 读回 (seq, content, summary, has_vec)——embedding 列不在 ORM 映射（raw SQL 纪律）。"""
    async with kb_pg() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT seq, content, summary, (embedding IS NOT NULL) AS has_vec "
                        "FROM document_chunks WHERE document_id = :document_id ORDER BY seq"
                    ),
                    {"document_id": document_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def test_embed步_摘要随向量同UPDATE落列(kb_pg: async_sessionmaker[AsyncSession], kb_doc: dict) -> None:
    """写侧主链路：embed 步对每个缺失向量 chunk 同 UPDATE 落 summary 列（向量+摘要原子同写）。"""
    await _require_vector_ready(kb_pg)
    report = await run_pipeline(
        kb_pg,
        tenant_id=kb_doc["tenant_id"],
        document_id=kb_doc["document_id"],
        embedder=_stub_embedder(),
        steps=("embed",),
    )
    assert [s.status for s in report.steps] == ["done"]
    rows = await _chunk_rows(kb_pg, kb_doc["document_id"])
    by_seq = {r["seq"]: r for r in rows}
    assert len(rows) == 3
    assert all(r["has_vec"] for r in rows), "向量与摘要应同 UPDATE 落列"
    assert by_seq[0]["summary"] == "feeder " + "A" * 193  # min(200, len) 截断（前 200 字符）
    assert by_seq[1]["summary"] == "feeder outage manual 停电处置手册"  # 短文本全量前缀
    assert by_seq[2]["summary"] == "feeder 前导空白"  # strip 首尾空白（含换行）


async def test_存量行_步级跳过不回填_重嵌入自然生成(kb_pg: async_sessionmaker[AsyncSession], kb_doc: dict) -> None:
    """存量行边界（13 篇 §22 K16-b「存量行不回填，重嵌入时自然生成」的真实机制）：
    - 已 indexed 文档 embed 步被 checkpoint 跳过（断点续跑 done 跳过）→ 行级不被触碰（不回填）；
    - checkpoint 撤销后的真重嵌 → 该文档全部 live 行摘要自然回填（embed 步执行即全量重嵌）。"""
    await _require_vector_ready(kb_pg)
    await run_pipeline(
        kb_pg,
        tenant_id=kb_doc["tenant_id"],
        document_id=kb_doc["document_id"],
        embedder=_stub_embedder(),
        steps=("embed",),
    )
    async with kb_pg() as db, db.begin():
        # 旧行 A：embedding+summary 双 NULL（模拟 K16 前迁移期行）；旧行 B：仅 summary NULL（已嵌入存量行）
        await db.execute(
            text(
                "UPDATE document_chunks SET embedding = NULL, summary = NULL "
                "WHERE document_id = :document_id AND seq = 0"
            ),
            {"document_id": kb_doc["document_id"]},
        )
        await db.execute(
            text("UPDATE document_chunks SET summary = NULL WHERE document_id = :document_id AND seq = 1"),
            {"document_id": kb_doc["document_id"]},
        )
    # rerun 不撤 checkpoint：done 步跳过 → 行零触碰（存量行不回填）
    skipped_run = await run_pipeline(
        kb_pg,
        tenant_id=kb_doc["tenant_id"],
        document_id=kb_doc["document_id"],
        embedder=_stub_embedder(),
        steps=("embed",),
    )
    assert all(s.skipped for s in skipped_run.steps)
    by_seq = {r["seq"]: r for r in await _chunk_rows(kb_pg, kb_doc["document_id"])}
    assert by_seq[0]["summary"] is None and not by_seq[0]["has_vec"]  # 步级跳过：零触碰
    assert by_seq[1]["summary"] is None and by_seq[1]["has_vec"]
    # 撤 embed 步 checkpoint → 真重嵌：全部 live 行摘要自然回填
    async with kb_pg() as db, db.begin():
        await db.execute(
            text("DELETE FROM kb_pipeline_step WHERE document_id = :document_id AND step = 'embed'"),
            {"document_id": kb_doc["document_id"]},
        )
    rerun = await run_pipeline(
        kb_pg,
        tenant_id=kb_doc["tenant_id"],
        document_id=kb_doc["document_id"],
        embedder=_stub_embedder(),
        steps=("embed",),
    )
    assert [s.status for s in rerun.steps] == ["done"]
    by_seq = {r["seq"]: r for r in await _chunk_rows(kb_pg, kb_doc["document_id"])}
    assert by_seq[0]["summary"] == "feeder " + "A" * 193 and by_seq[0]["has_vec"]  # 旧行重嵌入 → 摘要自然生成
    assert by_seq[1]["summary"] == "feeder outage manual 停电处置手册"  # 存量行随重嵌一并回填
    assert by_seq[2]["summary"] == "feeder 前导空白"


async def test_读侧_同SQL带回hit透出_旧行NULL兼容(kb_pg: async_sessionmaker[AsyncSession], kb_doc: dict) -> None:
    """读侧零读直出：bm25/vector 同一条 SQL 带回 summary → hit 透出；旧行 NULL → None 不崩。"""
    await _require_vector_ready(kb_pg)
    async with kb_pg() as db, db.begin():
        chunk_ids = (
            (await db.execute(select(DocumentChunkORM.id).where(DocumentChunkORM.document_id == kb_doc["document_id"])))
            .scalars()
            .all()
        )
        # 播种向量（向量臂前提）+ 摘要显式 NULL = 旧行基线；仅 seq=1 行带摘要（读侧数据面）
        await set_chunk_embeddings(db, [(cid, [0.05] * EMBED_DIM, None) for cid in chunk_ids])
        await db.execute(
            text("UPDATE document_chunks SET summary = :summary WHERE document_id = :document_id AND seq = 1"),
            {
                "summary": "feeder outage manual 停电处置手册",
                "document_id": kb_doc["document_id"],
            },
        )
    # BM25 路：同一条 SQL 带回（零额外查询）；旧行（seq=0/2）summary=None 不崩
    async with kb_pg() as db:
        hits = await bm25_search(db, tenant_id=kb_doc["tenant_id"], query="feeder", top_k=8)
    assert len(hits) == 3
    by_head = {h["content"][:9]: h for h in hits}
    seq1 = by_head["feeder ou"]
    assert seq1["summary"] == "feeder outage manual 停电处置手册"
    assert by_head["feeder AA"]["summary"] is None  # 旧行 NULL 兼容
    assert by_head["feeder 前导"]["summary"] is None
    # 行 dict → SearchHit 透出（api 层与业务层同构投影）
    hit_obj = api_dict_to_hit(seq1)
    assert isinstance(hit_obj, SearchHit) and hit_obj.summary == seq1["summary"]
    assert svc_dict_to_hit(hits[0]).summary == hits[0]["summary"]
    # 向量路：pgvector 余弦真跑（同 _CHUNK_FIELDS 带回）
    async with kb_pg() as db:
        vhits = await vector_search(db, tenant_id=kb_doc["tenant_id"], query_embedding=[0.05] * EMBED_DIM, top_k=8)
    assert len(vhits) == 3
    v_by_head = {h["content"][:9]: h for h in vhits}
    assert v_by_head["feeder ou"]["summary"] == "feeder outage manual 停电处置手册"
    assert v_by_head["feeder AA"]["summary"] is None

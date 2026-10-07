# tests/kb/test_demo_m2_loop.py
"""M2 验收口径 demo：建模 → 抽取 → 终审 → 检索 端到端（锚点 §7「建模→抽取→检索 demo 跑通」）。

链路（全部真实组件，仅两处按底线口径模拟）：
- 建模：种子本体资产 services/seeds/power_seed.ttl（load_seed_catalog；本体引导清单注入抽取提示词）；
- 抽取：真实三步 run_extract（种子目录进 user prompt）→ run_align（术语对齐）→
  run_validate（SHACL 门禁，subClassOf 闭包由 _gate_candidate 内置）；
- 终审：模拟人工（candidate → authoritative；底线 1：candidate 不参与检索）；
- 检索：KnowledgeSearchService.search 命中引用（citations 带 minio_key+span 出处指针；
  Ollama 不可达 → BM25-only 降级链，degraded 口径同 acl 用例）。

环境纪律：直连本地 PG（不可达即跳过，同 tests/kb 夹具纪律）；LLM 用 FakeModelPort 确定性输出。
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.kb.business.kb_extraction import SEED_TTL_PATH, load_seed_catalog, run_align, run_extract, run_validate
from services.kb.business.kb_pipeline import run_pipeline
from services.kb.business.search_service import KnowledgeSearchService
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.orm import KbFact as KbFactORM
from services.kb.data.orm import KbPipelineStep as KbPipelineStepORM
from services.platform.config import Settings
from services.platform.db import registry as orm_registry  # noqa: F401  # 全模块 ORM 入 metadata
from services.platform.llm.gateway import FakeModelPort
from services.review.business.candidates import ReviewTicketService
from services.review.data.orm import ReviewTicket as ReviewTicketORM

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

PW = "http://ontology-agent.local/o/t1/power#"


def _seed_class_index(iri: str) -> int:
    """K24 序号口径（docs/Agent/13 §30）：种子类在声明序（=extract_v3 目录渲染序=映射序）中的 1 起序号。"""
    return next(i for i, (c_iri, _, _) in enumerate(load_seed_catalog().classes, start=1) if c_iri == iri)


# 双形态内容：空格分词句喂 BM25（'simple' 配置）；连写关键词喂 FakeModelPort 抽取匹配。
CONTENT = (
    "# 停电抽取联调\n"
    "馈线 F001 由 城东变电站 供电，保护动作 后 完成 故障隔离。\n"
    "\n## 抢修工单\n"
    "馈线F001 工单OO-123456 已创建，工单状态为 created。\n"
)
# K24：extract_v3 编号目录制下模型只回类序号（整数），解析侧映射回 IRI——IRI 直出会被硬幻觉门禁误剪。
MODEL_KEYWORDS = {"馈线F001": _seed_class_index(f"{PW}Feeder"), "工单OO-123456": _seed_class_index(f"{PW}OutageOrder")}
MODEL_PROPERTIES = {"工单OO-123456": {"orderNo": "OO-123456", "hasStatus": "created"}}


@pytest.fixture
async def kb_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过 M2 demo 用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def demo_env(
    kb_pg: async_sessionmaker[AsyncSession],
) -> AsyncIterator[dict]:
    """建模资产在场 + 独立租户/集合/文档（走真实 preprocess+chunk）；结束 FK 逆序清理。"""
    assert SEED_TTL_PATH.exists(), "种子本体资产缺失（M2 出口条件，禁空工作台冷启动）"
    async with kb_pg() as db, db.begin():
        tenant = TenantORM(name="m2-demo-租户", slug=f"m2-demo-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="m2-demo-库", embedding_model="bge-m3")
        db.add(collection)
        await db.flush()
        doc = DocumentORM(
            tenant_id=tenant.id,
            kb_collection_id=collection.id,
            title="停电抽取联调",
            source_type="upload",
            size_bytes=len(CONTENT.encode()),
            minio_key=f"raw-docs/{tenant.id}/{collection.id}/{uuid.uuid4()}/source.md",
            checksum_sha256=hashlib.sha256(CONTENT.encode()).hexdigest(),
            meta={"content": CONTENT},
            status="uploaded",
        )
        db.add(doc)
    await run_pipeline(
        kb_pg,
        tenant_id=tenant.id,
        document_id=doc.id,
        steps=("preprocess", "chunk"),
        backoff=lambda attempt: asyncio.sleep(0),
    )
    env = {
        "tenant_id": tenant.id,
        "collection_id": collection.id,
        "document_id": doc.id,
        "model": FakeModelPort(keyword_classes=MODEL_KEYWORDS, keyword_properties=MODEL_PROPERTIES),
        "review": ReviewTicketService(kb_pg),
    }
    yield env
    async with kb_pg() as db, db.begin():
        for stmt in (
            delete(ReviewTicketORM).where(ReviewTicketORM.tenant_id == env["tenant_id"]),
            delete(KbFactORM).where(KbFactORM.tenant_id == env["tenant_id"]),
            delete(DocumentChunkORM).where(DocumentChunkORM.tenant_id == env["tenant_id"]),
            delete(KbPipelineStepORM).where(KbPipelineStepORM.tenant_id == env["tenant_id"]),
            delete(DocumentORM).where(DocumentORM.id == env["document_id"]),
            delete(KbCollectionORM).where(KbCollectionORM.id == env["collection_id"]),
            delete(TenantORM).where(TenantORM.id == env["tenant_id"]),
        ):
            await db.execute(stmt)


def _ctx(kb_pg: async_sessionmaker[AsyncSession], env: dict) -> object:
    from services.kb.business.kb_pipeline import StepContext

    return StepContext(
        session_factory=kb_pg,
        tenant_id=env["tenant_id"],
        document_id=env["document_id"],
        embedder=None,
        model=env["model"],  # type: ignore[arg-type]
        review=env["review"],
    )


async def test_m2_demo_建模_抽取_终审_检索_端到端(kb_pg: async_sessionmaker[AsyncSession], demo_env: dict) -> None:
    """建模（种子目录）→ 抽取（引导+对齐+门禁）→ 终审（candidate→authoritative）→ 检索（引用命中）。"""
    catalog = load_seed_catalog()  # 建模资产：类目表即抽取提示词的本体引导清单
    assert catalog.classes, "种子类目为空"

    ctx = _ctx(kb_pg, demo_env)
    await run_extract(ctx)  # 种子目录注入 user prompt（本体引导清单）
    await run_align(ctx)  # 术语对齐：subject_type 归一到种子 IRI
    await run_validate(ctx)  # SHACL 门禁（subClassOf 闭包内置）

    doc_filter = KbFactORM.document_id == demo_env["document_id"]
    ticket_filter = ReviewTicketORM.tenant_id == demo_env["tenant_id"]
    async with kb_pg() as db:
        facts = (await db.execute(select(KbFactORM).where(doc_filter))).scalars().all()
        tickets = (await db.execute(select(ReviewTicketORM).where(ticket_filter))).scalars().all()
    assert facts, "抽取未产出候选事实"
    assert all(f.subject_type in catalog.class_iris for f in facts), "存在未对齐到种子本体的候选类型"
    assert all(f.status == "candidate" for f in facts), "候选态被越权写入权威态"
    assert all(f.violations == [] for f in facts), "合规候选不应带 violations"
    assert tickets and all(t.payload["gate_result"]["conforms"] is True for t in tickets)

    # 模拟人工终审（底线 1：candidate 不参与检索；此处按 citation_baseline 同款口径置权威态）
    async with kb_pg() as db, db.begin():
        for fact in (await db.execute(select(KbFactORM).where(doc_filter))).scalars().all():
            fact.status = "authoritative"

    # 检索：同一租户/库内查询，引用必须命中 demo 文档并带出处指针
    service = KnowledgeSearchService(kb_pg, ollama_base_url="http://localhost:9")  # 9 端口不可达 → BM25-only 降级
    result = await service.search(
        tenant_id=demo_env["tenant_id"], query="馈线 F001", kb_id=demo_env["collection_id"], top_k=5
    )
    assert result.citations, "检索未命中任何引用"
    hit_docs = {str(c.doc_id) for c in result.citations}
    assert str(demo_env["document_id"]) in hit_docs, "引用未命中 demo 文档"
    for citation in result.citations:  # §5 出处指针契约
        assert citation.minio_key, "引用缺 minio_key 出处指针"
        assert citation.quote, "引用缺原文截片"
    async with kb_pg() as db:  # 终审后权威态不被检索路径越权改动
        after = (await db.execute(select(KbFactORM).where(doc_filter))).scalars().all()
    assert all(f.status == "authoritative" for f in after)

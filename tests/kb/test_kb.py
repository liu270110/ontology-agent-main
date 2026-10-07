"""M2 知识库基线用例（D 批次）：分块边界×2 + RRF 融合×1 + 流水线状态推进×1 + 检索降级×1。

- 纯函数用例（分块/RRF/降级编排）零外部依赖；
- 流水线用例直连本地 PG（不可达即跳过，同 tests/gateway 夹具纪律），走默认步执行器
  （preprocess/chunk/bm25_index）+ 注入式降级嵌入（fake embedder 调用必抛
  EmbeddingUnavailableError，覆盖「模型不可用 → BM25-only 软降级、文档照常 indexed」
  契约；不做成功向量 mock）。
psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略。
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.kb.business.kb_pipeline import (
    MAX_STEP_ATTEMPTS,
    PipelineError,
    assert_document_transition,
    run_pipeline,
)
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.orm import KbPipelineStep as KbPipelineStepORM
from services.kb.retrieval.chunking import chunk_document
from services.kb.retrieval.embed import EmbeddingUnavailableError, OllamaEmbedder
from services.kb.retrieval.retrieve import (
    RRF_K,
    GraphExpansion,
    SearchHit,
    build_extractive_answer,
    hybrid_search,
    resolve_mode,
    rrf_fuse,
)
from services.platform.config import Settings

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ---------------------------------------------------------------- 分块（OntRAG §2.1）


def test_chunk_respects_heading_and_paragraph_boundaries_with_overlap():
    """标题开新节、段落不硬切；span 精确指向原文；相邻块按预算整块重叠。"""
    doc = (
        "# 第一章 概述\n"
        "段落甲：停电分析需要台账与工单两类数据源支撑。\n"
        "\n"
        "## 1.1 数据来源\n"
        "段落乙：台账提供设备拓扑，工单提供故障记录。\n"
        "\n"
        "# 第二章 检索基线\n"
        "段落丙：混合检索融合向量与词法两路召回。\n"
    )
    # ① 边界：预算压小且关闭重叠 → 节内聚攒、跨节不粘连，span 与原文逐字对齐
    chunks = chunk_document(doc, target_tokens=16, tolerance=0, overlap_ratio=0.0)
    assert len(chunks) >= 3
    assert chunks[0].content.startswith("# 第一章 概述\n")  # 标题随节首片落块
    assert "段落甲" in chunks[0].content and "段落乙" not in chunks[0].content  # 不跨节粘连
    for chunk in chunks:  # 无重叠片：content == 原文 [span) 逐字切片（出处指针门禁）
        start, end = chunk.meta["span"]
        assert doc[start:end] == chunk.content, f"span 出错: {chunk.meta['span']}"
    section_b = next(c for c in chunks if "段落乙" in c.content)
    assert section_b.meta["heading"] == "第一章 概述 > 1.1 数据来源"  # 标题层级路径
    section_c = next(c for c in chunks if "段落丙" in c.content)
    assert section_c.meta["heading"] == "第二章 检索基线"

    # ② 重叠：等长段落 + 小预算 + 10% 档放大 → 下片以上片末段开头（整块搬运不打断句子）
    paras = ["aa bb\n", "cc dd\n", "ee ff\n", "gg hh\n"]  # 各 6 字符 ≈ 3 token（粗估 len//2）；空行分段
    overlap_chunks = chunk_document("".join(p + "\n" for p in paras), target_tokens=6, tolerance=0, overlap_ratio=0.5)
    assert overlap_chunks[0].content == "".join(paras[:3])
    assert overlap_chunks[1].content == "ee ff\ngg hh\n"  # 首块 = 上片末段（重叠搬入）


def test_chunk_keeps_table_whole_or_splits_by_row_groups_with_header():
    """表格整块不切；超长表按行组切分且每段重复表头；行不重不漏、不与段落混块。"""
    header = "| 馈线 | 容量 |\n"
    separator = "| --- | --- |\n"
    rows = [f"| F{i:03d} | {i}MW |\n" for i in range(1, 41)]

    # ① 短表：整块保留（不切，与前后文同片共存但表体逐字完整）
    small_doc = "# 台账\n" + header + separator + rows[0] + "\n表后段落。\n"
    small_chunks = chunk_document(small_doc, target_tokens=512, tolerance=128)
    assert len(small_chunks) == 1
    assert header + separator + rows[0] in small_chunks[0].content
    assert "表后段落" in small_chunks[0].content and "F002" not in small_chunks[0].content

    # ② 超长表：行组切分，每段重复表头（块脱离原文自解释），行集合守恒
    big_doc = header + separator + "".join(rows)
    big_chunks = chunk_document(big_doc, target_tokens=24, tolerance=0)
    groups = [c for c in big_chunks if c.meta.get("kind") == "table"]
    assert len(groups) >= 2, "超长表应按行组切分"
    assert all(c.content.startswith(header) for c in groups)  # 每段重复表头
    assert all(c.meta.get("row_group") for c in groups)
    emitted_rows: list[str] = []
    for group in groups:
        body = group.content.replace(header, "").replace(separator, "")
        emitted_rows.extend(line + "\n" for line in body.splitlines() if line.strip())
    assert emitted_rows == rows  # 不重不漏


# ---------------------------------------------------------------- RRF 融合（OntRAG §4.2）


def test_rrf_fusion_merges_channels_with_k60_weights():
    """score = Σ w/(k+rank)：bm25=[A,B,C]、vector=[C,A,D] → A > C > B > D；多路命中合并 channels。"""
    ids = {name: uuid.uuid4() for name in "ABCD"}

    def hit(name: str) -> SearchHit:
        return SearchHit(chunk_id=ids[name], document_id=uuid.uuid4(), content=f"chunk-{name}")

    fused = rrf_fuse({"bm25": [hit("A"), hit("B"), hit("C")], "vector": [hit("C"), hit("A"), hit("D")]})
    assert [h.chunk_id for h in fused] == [ids["A"], ids["C"], ids["B"], ids["D"]]
    first, second, third, fourth = fused
    assert first.channels == ["bm25", "vector"]  # 双路命中合并为一条
    assert second.channels == ["bm25", "vector"]
    assert third.channels == ["bm25"] and fourth.channels == ["vector"]
    expected_a = 0.5 / (RRF_K + 1) + 0.5 / (RRF_K + 2)  # A：bm25#1 + vector#2
    expected_c = 0.5 / (RRF_K + 3) + 0.5 / (RRF_K + 1)  # C：bm25#3 + vector#1
    expected_b = 0.5 / (RRF_K + 2)
    assert first.score == pytest.approx(expected_a)
    assert second.score == pytest.approx(expected_c)
    assert third.score == pytest.approx(expected_b)
    assert first.score > second.score > third.score > fourth.score
    # 空通道跳过（通道缺失即跳过纪律）
    assert [h.chunk_id for h in rrf_fuse({"bm25": [], "vector": [hit("A")]})] == [ids["A"]]


# ---------------------------------------------------------------- 检索降级（在线四率口径）


async def test_hybrid_search_degrades_to_bm25_when_embedding_unavailable():
    """嵌入路抛 EmbeddingUnavailableError → BM25-only 继续，degraded=true（降级不阻断）。"""
    hit_a = SearchHit(chunk_id=uuid.uuid4(), document_id=uuid.uuid4(), content="bm25-hit", score=2.0)
    hit_b = SearchHit(chunk_id=uuid.uuid4(), document_id=uuid.uuid4(), content="bm25-hit-2", score=1.0)

    async def bm25_fn(query: str, top_k: int) -> list[SearchHit]:
        assert top_k >= 8  # 召回池 ≥ top_k（RECALL_POOL=50 池化）
        return [hit_a, hit_b]

    async def vector_fn(query: str, top_k: int) -> list[SearchHit]:
        raise EmbeddingUnavailableError("ollama down")

    result = await hybrid_search("停电处置", bm25=bm25_fn, vector=vector_fn, top_k=8, mode="local")
    assert result.degraded is True
    assert result.channels == ["bm25"]
    assert [h.chunk_id for h in result.hits] == [hit_a.chunk_id, hit_b.chunk_id]  # BM25 原序保留
    assert all(h.channels == ["bm25"] for h in result.hits)

    # 对照：两路齐备 → 不降级；双路命中者得分叠加胜出（B: 1/62+1/61 > A: 1/61）
    async def vector_ok(query: str, top_k: int) -> list[SearchHit]:
        return [SearchHit(chunk_id=hit_b.chunk_id, document_id=hit_b.document_id, content="vec-hit")]

    both = await hybrid_search("停电处置", bm25=bm25_fn, vector=vector_ok, top_k=8)
    assert both.degraded is False and set(both.channels) == {"bm25", "vector"}
    assert [h.chunk_id for h in both.hits] == [hit_b.chunk_id, hit_a.chunk_id]
    assert both.hits[0].channels == ["bm25", "vector"]


# ---------------------------------------------------------------- 流水线状态推进（03 §4 八态）


def test_pipeline_state_machine_transitions():
    """状态机断言：M2-lite 捷径 preprocessed→indexed 合法；跨越式迁移拒绝。"""
    assert_document_transition("uploaded", "preprocessed")
    assert_document_transition("preprocessed", "indexed")  # M2-lite 捷径（extract/align/validate 随 M2.5 插回）
    assert_document_transition("failed", "preprocessed")  # 修复后按 checkpoint 重跑
    assert_document_transition("pending_review", "indexed")
    assert_document_transition("indexed", "indexed")  # 重跑补向量（幂等登记边）
    with pytest.raises(PipelineError):
        assert_document_transition("uploaded", "indexed")  # 未预处理不得直取终态
    with pytest.raises(PipelineError):
        assert_document_transition("indexed", "failed")  # 终态不再翻失败


@pytest.fixture
async def kb_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """本地 PG 会话工厂；不可达即跳过整用例（同 tests/gateway 夹具纪律）。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过 kb 流水线集成用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def kb_doc(kb_pg: async_sessionmaker[AsyncSession]) -> AsyncIterator[dict]:
    """每用例独立租户/集合/文档（uploaded + 内联 markdown），结束按 FK 逆序清理。"""
    content = (
        "# 停电处置手册\n"
        "故障定位先看馈线开关与保护动作记录，再核对抢修工单。\n"
        "\n"
        "## 抢修工单要点\n"
        "工单须记录停电时间、影响台区与恢复送电时间。\n"
    )
    async with kb_pg() as db, db.begin():
        tenant = TenantORM(name="kb-it-租户", slug=f"kb-it-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="kb-it-库", embedding_model="bge-m3")
        db.add(collection)
        await db.flush()
        doc = DocumentORM(
            tenant_id=tenant.id,
            kb_collection_id=collection.id,
            title="停电处置手册",
            source_type="upload",
            size_bytes=len(content.encode()),
            minio_key=f"raw-docs/{tenant.id}/{collection.id}/{uuid.uuid4()}/source.md",
            checksum_sha256=hashlib.sha256(content.encode()).hexdigest(),
            meta={"content": content},
            status="uploaded",
        )
        db.add(doc)
    ids = {"tenant_id": tenant.id, "collection_id": collection.id, "document_id": doc.id}
    yield ids
    async with kb_pg() as db, db.begin():  # FK 逆序清理
        for stmt in (
            delete(DocumentChunkORM).where(DocumentChunkORM.tenant_id == ids["tenant_id"]),
            delete(KbPipelineStepORM).where(KbPipelineStepORM.tenant_id == ids["tenant_id"]),
            delete(DocumentORM).where(DocumentORM.tenant_id == ids["tenant_id"]),
            delete(KbCollectionORM).where(KbCollectionORM.tenant_id == ids["tenant_id"]),
            delete(TenantORM).where(TenantORM.id == ids["tenant_id"]),
        ):
            await db.execute(stmt)


def _degraded_embedder(error: str) -> OllamaEmbedder:
    """注入式降级嵌入：调用必抛（不做成功向量 mock——降级契约的真值路径）。"""
    embedder = OllamaEmbedder("http://localhost:9", timeout=0.1)

    async def _raise(texts):  # noqa: ANN001
        raise EmbeddingUnavailableError(error)

    embedder.embed = _raise  # type: ignore[method-assign]
    return embedder


async def test_pipeline_progression_indexes_document_with_degraded_embed(
    kb_pg: async_sessionmaker[AsyncSession], kb_doc: dict
) -> None:
    """uploaded →（preprocess/chunk/embed/bm25_index）→ indexed；embed 降级不阻断索引。

    断点续跑：重跑时 done 步跳过（attempt 不再增长）、耗尽的 failed 步保留原错误。
    """
    tenant_id: uuid.UUID = kb_doc["tenant_id"]
    document_id: uuid.UUID = kb_doc["document_id"]
    backoff_calls: list[int] = []

    async def instant_backoff(attempt: int) -> None:  # 测试不等待真实退避（30s 起）
        backoff_calls.append(attempt)

    report = await run_pipeline(
        kb_pg,
        tenant_id=tenant_id,
        document_id=document_id,
        embedder=_degraded_embedder("模拟：Ollama bge-m3 不可用"),
        backoff=instant_backoff,
    )
    assert [s.step for s in report.steps] == ["preprocess", "chunk", "embed", "bm25_index"]
    assert [s.status for s in report.steps] == ["done", "done", "failed", "done"]
    assert report.degraded is True
    assert report.document_status == "indexed"  # M2-lite：BM25-only 照常可检索
    assert len(backoff_calls) == MAX_STEP_ATTEMPTS - 1  # embed 步内重试 ≤3（耗尽即止）

    async with kb_pg() as db:
        doc = (await db.execute(select(DocumentORM).where(DocumentORM.id == document_id))).scalar_one()
        assert doc.status == "indexed" and "embed" in (doc.meta.get("degraded") or [])
        chunks = (
            (
                await db.execute(
                    select(DocumentChunkORM)
                    .where(DocumentChunkORM.document_id == document_id)
                    .order_by(DocumentChunkORM.seq)
                )
            )
            .scalars()
            .all()
        )
        assert len(chunks) >= 2
        assert all(c.meta.get("span") for c in chunks), "门禁：chunk 必须带原文出处指针"
        steps = (
            (
                await db.execute(
                    select(KbPipelineStepORM)
                    .where(KbPipelineStepORM.document_id == document_id)
                    .order_by(KbPipelineStepORM.created_at)
                )
            )
            .scalars()
            .all()
        )
        step_status = {s.step: (s.status, s.attempt) for s in steps}
    assert step_status["preprocess"] == ("done", 1)
    assert step_status["embed"][0] == "failed" and step_status["embed"][1] == MAX_STEP_ATTEMPTS

    # 断点续跑：done 步跳过（attempt 冻结）；耗尽步保持 failed 且错误不被清空；终态幂等
    rerun = await run_pipeline(
        kb_pg,
        tenant_id=tenant_id,
        document_id=document_id,
        embedder=_degraded_embedder("仍不可用"),
        backoff=instant_backoff,
    )
    assert {s.step for s in rerun.steps if s.skipped} == {"preprocess", "chunk", "bm25_index"}
    assert rerun.document_status == "indexed"
    async with kb_pg() as db:
        embed_row = (
            await db.execute(
                select(KbPipelineStepORM).where(
                    KbPipelineStepORM.document_id == document_id, KbPipelineStepORM.step == "embed"
                )
            )
        ).scalar_one()
    assert embed_row.status == "failed" and embed_row.attempt == MAX_STEP_ATTEMPTS
    assert embed_row.error and "embedding-unavailable" in embed_row.error


# ---------------------------------------------------------------- knowledge.search lite 契约（任务 2.3）


def test_resolve_mode_downgrades_global_and_drift_to_local():
    """§4.0 lite 口径：auto→local 纯路由；global/drift→local 且带降级原因（community report 二期）。"""
    assert resolve_mode("auto") == ("local", None)
    assert resolve_mode("local") == ("local", None)
    assert resolve_mode("global") == ("local", "mode_downgraded:global")
    assert resolve_mode("drift") == ("local", "mode_downgraded:drift")


def test_build_extractive_answer_sentences_carry_citations():
    """抽取式摘要：句级拼装挂来源 chunk 引用；重叠块重复句按首现去重；空命中返回 None。"""
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    hits = [
        SearchHit(chunk_id=id_a, document_id=uuid.uuid4(), content="馈线 F001 故障隔离。保护动作记录完整。\n"),
        SearchHit(chunk_id=id_b, document_id=uuid.uuid4(), content="馈线 F001 故障隔离。次句不进摘要。"),
    ]
    answer = build_extractive_answer(hits, confidence=0.8)
    assert answer is not None
    assert answer.confidence == 0.8
    assert [s.text for s in answer.sentences] == [
        "馈线 F001 故障隔离。",
        "保护动作记录完整。",
        "次句不进摘要。",
    ]
    # 重叠句「馈线 F001 故障隔离。」在第二命中中按首现去重；各句挂各自来源 chunk
    assert [s.citations for s in answer.sentences] == [[id_a], [id_a], [id_b]]
    assert answer.citations == [id_a, id_a, id_b]
    assert build_extractive_answer([], confidence=0.8) is None


async def test_hybrid_search_graph_channel_uses_graph_weights():
    """图路命中：RRF 权重切换为图 0.6/向量 0.4/bm25 0.4（图扩展 chunk 胜出）；答案随结果返回。"""
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    bm25_hit = SearchHit(chunk_id=id_a, document_id=uuid.uuid4(), content="bm25 命中", score=1.0)

    async def bm25_fn(query: str, top_k: int) -> list[SearchHit]:
        return [bm25_hit]

    async def graph_fn(seeds: Sequence[SearchHit]) -> GraphExpansion:
        assert [s.chunk_id for s in seeds] == [id_a]  # 种子 = bm25 命中
        return GraphExpansion(hits=[SearchHit(chunk_id=id_b, document_id=uuid.uuid4(), content="图扩展命中")])

    result = await hybrid_search("停电", bm25=bm25_fn, graph=graph_fn, top_k=5, mode="local")
    assert set(result.channels) == {"bm25", "graph"}
    # 图权 0.6 生效的直接证据：graph#1(0.6/61) > bm25#1(0.4/61)，图扩展 chunk 胜出
    assert [h.chunk_id for h in result.hits] == [id_b, id_a]
    assert result.hits[0].channels == ["graph"]
    assert result.answer is not None and 0 < result.answer.confidence <= 1.0

    # 对照：图路无命中 → 维持 bm25/vector 各 0.5 既有基线（不切图权重）
    async def graph_empty(seeds: Sequence[SearchHit]) -> GraphExpansion:
        return GraphExpansion()

    baseline_result = await hybrid_search("停电", bm25=bm25_fn, graph=graph_empty, top_k=5, mode="local")
    assert baseline_result.channels == ["bm25"]
    assert [h.chunk_id for h in baseline_result.hits] == [id_a]


async def test_hybrid_search_entity_filter_requires_verifiable_membership():
    """entity_type_filter：图路可用→按匹配集过滤；图路不可用→空结果（不可校验即不返回）。"""
    id_a, id_b = uuid.uuid4(), uuid.uuid4()

    async def bm25_fn(query: str, top_k: int) -> list[SearchHit]:
        return [
            SearchHit(chunk_id=id_a, document_id=uuid.uuid4(), content="有类 chunk"),
            SearchHit(chunk_id=id_b, document_id=uuid.uuid4(), content="无类 chunk"),
        ]

    async def graph_fn(seeds: Sequence[SearchHit]) -> GraphExpansion:
        return GraphExpansion(hits=[], matched_chunk_ids={id_a})

    filtered = await hybrid_search("q", bm25=bm25_fn, graph=graph_fn, entity_type_filter=["pw:Feeder"])
    assert [h.chunk_id for h in filtered.hits] == [id_a]  # 仅类匹配命中的 chunk 保留
    unresolved = await hybrid_search("q", bm25=bm25_fn, entity_type_filter=["pw:Feeder"])
    assert unresolved.hits == []  # 无图路可校验 → 空（防过滤静默失效）
    unfiltered = await hybrid_search("q", bm25=bm25_fn)
    assert len(unfiltered.hits) == 2

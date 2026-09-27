"""knowledge.search 引用率基线（OntRAG §10 检索引用率 + §5 契约的 M2.5 基线评估；integration）。

- 小样例库：4 个电力域黄金文档（7 chunk）+ 3 个干扰文档（9 chunk，占位防 top-k 恒中）；
  chunk 与权威事实（kb_facts.status='authoritative'，模拟人工终审后状态）直接落库，绕过流水线
  （确定性）；本体读模型落 Ontology/OntologyVersion/OntoClass（Feeder/Transformer ⊂ PowerDevice）；
- golden QA 12 条：query → 期望引用 chunk（词形按 'simple' 分词口径以空格预切，
  中文分词随 M3+，见 embed.py）；跑 knowledge.search lite 全链（embed.bm25_search 真实 SQL +
  retrieval/graph.py 图路 + retrieve.hybrid_search 编排 + ontology.api.get_class_hierarchy 真实
  跨模块查询）；向量路注入降级（不做成功向量 mock——评估不依赖 Ollama，同 tests/kb/test_kb.py
  纪律），故基线口径 = BM25+图 两路（向量路随 PoC ③ 补测）；
- 断言：citation hit@5 ≥ 0.6（首跑即基线）；图路契约（channels 含 graph、graph_paths 类 IRI 链
  含层次扩展端点与 subclass_of 边）；结果写 evaluation_runs/evaluation_results
  （benchmark_type='retrieval_qa'，08 §7.3 契约 ORM）；基线数字（引用率/延迟 P50）随用例输出留档。

psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略。
"""

from __future__ import annotations

import asyncio
import hashlib
import statistics
import sys
import time
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import EvaluationResult as EvaluationResultORM
from services.kb.data.orm import EvaluationRun as EvaluationRunORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.orm import KbFact as KbFactORM
from services.kb.retrieval.embed import EmbeddingUnavailableError, bm25_search
from services.kb.retrieval.graph import build_class_hierarchy, expand_graph
from services.kb.retrieval.retrieve import (
    GraphExpansion,
    HybridSearchResult,
    SearchHit,
    hybrid_search,
)
from services.ontology.business.hierarchy_service import get_class_hierarchy
from services.ontology.data.orm import OntoClass as OntoClassORM
from services.ontology.data.orm import Ontology as OntologyORM
from services.ontology.data.orm import OntologyVersion as OntologyVersionORM
from services.platform.config import Settings

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pytestmark = [pytest.mark.integration]

PW_NS = "http://ontology-agent.local/o/t1/power#"
OB2_OBJECT = "https://ontology-agent.dev/ns/ob2#Object"
BASELINE_VERSION = "power-lite-v1"
HIT_AT_K = 5
BASELINE_FLOOR = 0.6  # 任务口径：首跑即基线，断言下限（实测值随 evaluation_runs 留档）

# 黄金语料（chunk_key → (文档标题, chunk 正文)；空格分词适配 'simple' 配置，见模块 docstring）
GOLDEN_DOCS: dict[str, tuple[str, str]] = {
    "feeder_f001": (
        "馈线故障处置手册",
        "馈线 F001 故障隔离流程 保护动作后 调度员 执行 故障隔离 并 记录 保护动作 情况",
    ),
    "feeder_f002": (
        "馈线故障处置手册",
        "馈线 F002 抢修工单 抢修班组 到场 恢复送电 后 归档 抢修工单",
    ),
    "transformer_oil": (
        "变电站巡检规程",
        "变电站 变压器 T01 油温 巡检 油温超限 时 通知 运维 安排 停电检修",
    ),
    "breather_gel": (
        "变电站巡检规程",
        "变电站 呼吸器 巡检 发现 硅胶 变色 更换 硅胶 并 记录",
    ),
    "outage_order": (
        "停电工单管理规范",
        "停电 工单 OID2024101 由 调度员 录入 停电时间 影响 台区 与 恢复送电时间",
    ),
    "repair_audit": (
        "停电工单管理规范",
        "抢修 工单 审核 归档 后 派发 记录 纳入 月度 考核",
    ),
    "device_ledger": (
        "配网设备台账",
        "配网 设备 台账 涵盖 馈线 变压器 开关 计量表 等 电力 设备",
    ),
}

# chunk_key → 权威事实（subject, 本体类 IRI）；层次：Feeder/Transformer ⊂ PowerDevice（读模型投影）
GOLDEN_FACTS: dict[str, tuple[str, str]] = {
    "feeder_f001": ("馈线F001", f"{PW_NS}Feeder"),
    "feeder_f002": ("馈线F002", f"{PW_NS}Feeder"),
    "transformer_oil": ("变压器T01", f"{PW_NS}Transformer"),
    "breather_gel": ("呼吸器", f"{PW_NS}Transformer"),
    "outage_order": ("停电工单OID2024101", f"{PW_NS}OutageOrder"),
    "repair_audit": ("抢修工单", f"{PW_NS}OutageOrder"),
    "device_ledger": ("配网设备台账", f"{PW_NS}PowerDevice"),
}

GOLDEN_QA: list[tuple[str, str]] = [
    ("馈线 F001 故障隔离", "feeder_f001"),
    ("馈线 F002 抢修工单", "feeder_f002"),
    ("变压器 油温 巡检", "transformer_oil"),
    ("硅胶 变色 更换", "breather_gel"),
    ("停电 工单 录入 台区", "outage_order"),
    ("抢修 工单 审核 考核", "repair_audit"),
    ("故障隔离 保护动作", "feeder_f001"),
    ("恢复送电 归档", "feeder_f002"),
    ("油温超限 停电检修", "transformer_oil"),
    ("OID2024101 调度员", "outage_order"),
    ("派发 记录 考核", "repair_audit"),
    ("配网 设备 台账", "device_ledger"),
]

# 类层次投影（ontology 读模型；PowerDevice 挂平台顶类 ob2:Object）
CLASS_HIERARCHY: dict[str, list[str]] = {
    f"{PW_NS}PowerDevice": [OB2_OBJECT],
    f"{PW_NS}Feeder": [f"{PW_NS}PowerDevice"],
    f"{PW_NS}Transformer": [f"{PW_NS}PowerDevice"],
    f"{PW_NS}Switch": [f"{PW_NS}PowerDevice"],
    f"{PW_NS}OutageOrder": [OB2_OBJECT],
}

_FILLER_DOCS: list[tuple[str, list[str]]] = [
    (
        "平台运维公告",
        [
            "本周 例行 维护 完成 数据 备份 一致性 校验 与 组件 版本 发布 事项",
            "值班 安排 调整 请 各 组 提前 报备 节假日 值守 名单 与 联系 方式",
            "办公 终端 安全 检查 将于 下周 启动 请 及时 更换 弱 口令 并 锁屏",
        ],
    ),
    (
        "会议纪要汇编",
        [
            "季度 复盘 会议 确定 下 阶段 试点 范围 与 验收 材料 清单 责任 人",
            "跨 组 协同 事项 汇总 接口 文档 评审 时间 待 定 请 关注 邮件 通知",
            "培训 计划 更新 新 员工 入职 课程 与 内部 分享 主题 征集 开放",
        ],
    ),
    (
        "行政管理制度",
        [
            "差旅 报销 流程 调整 发票 张贴 与 审批 链路 以 新 版 指引 为准",
            "固定资产 盘点 启动 各 部门 核对 台账 信息 并 确认 领用 记录",
            "会议室 预约 规则 优化 长时 占用 需 附 说明 释放 空闲 时段",
        ],
    ),
]


@pytest.fixture
async def kb_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """本地 PG 会话工厂；不可达即跳过整用例（同 tests/kb/test_kb.py 夹具纪律）。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过引用率基线评估")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def baseline(kb_pg: async_sessionmaker[AsyncSession]) -> AsyncIterator[dict]:
    """样例库：租户/集合/黄金+干扰文档/chunks/权威事实 + 本体读模型（层次投影）。"""
    async with kb_pg() as db, db.begin():
        tenant = TenantORM(name="kb-baseline-租户", slug=f"kb-bl-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="kb-bl-库", embedding_model="bge-m3")
        db.add(collection)
        await db.flush()

        chunk_ids: dict[str, uuid.UUID] = {}
        doc_ids: dict[str, uuid.UUID] = {}
        for key, (title, content) in GOLDEN_DOCS.items():
            doc_id, chunk_id = await _seed_doc(
                db, tenant_id=tenant.id, collection_id=collection.id, title=title, content=content
            )
            doc_ids[key], chunk_ids[key] = doc_id, chunk_id
        filler_no = 0
        for title, paragraphs in _FILLER_DOCS:
            for paragraph in paragraphs:
                filler_no += 1
                await _seed_doc(
                    db,
                    tenant_id=tenant.id,
                    collection_id=collection.id,
                    title=f"{title}·{filler_no}",
                    content=paragraph,
                )

        for key, (subject, class_iri) in GOLDEN_FACTS.items():
            db.add(
                KbFactORM(
                    tenant_id=tenant.id,
                    document_id=doc_ids[key],
                    chunk_id=chunk_ids[key],
                    fact_type="entity",
                    subject=subject,
                    subject_type=class_iri,
                    canonical_name=subject,
                    aliases=[class_iri],  # align 步口径：类 IRI 入 aliases（检索图路同源素材）
                    confidence=0.900,
                    status="authoritative",  # 模拟终审后权威态（底线 1：candidate 不参与检索）
                    evidence={"source_ref": {"note": "baseline-seed"}},
                )
            )

        # 本体读模型（ontology 模块表；层次投影 = CLASS_HIERARCHY；ontology 先行满足 FK）
        ontology = OntologyORM(
            tenant_id=tenant.id,
            iri_base=PW_NS.rstrip("#"),
            name=f"power-seed-{uuid.uuid4().hex[:8]}",
            status="published",
        )
        db.add(ontology)
        await db.flush()
        version = OntologyVersionORM(
            tenant_id=tenant.id,
            ontology_id=ontology.id,
            version="v1",
            version_no=1,
            artifact_key=f"test://{BASELINE_VERSION}/power.ttl",
            checksum="0" * 64,
        )
        db.add(version)
        await db.flush()
        ontology.current_version_id = version.id  # 发布指针（本表 FK 迁移后置，无约束阻塞）
        for iri, supers in CLASS_HIERARCHY.items():
            db.add(
                OntoClassORM(
                    tenant_id=tenant.id,
                    ontology_id=ontology.id,
                    version_id=version.id,
                    iri=iri,
                    name=iri.rsplit("#", 1)[-1],
                    subclass_of=supers,
                )
            )
    ids: dict[str, object] = {"tenant_id": tenant.id, "collection_id": collection.id, "chunk_ids": chunk_ids}
    yield ids
    async with kb_pg() as db, db.begin():  # FK 逆序清理
        for stmt in (
            delete(EvaluationResultORM).where(EvaluationResultORM.tenant_id == ids["tenant_id"]),
            delete(EvaluationRunORM).where(EvaluationRunORM.tenant_id == ids["tenant_id"]),
            delete(KbFactORM).where(KbFactORM.tenant_id == ids["tenant_id"]),
            delete(OntoClassORM).where(OntoClassORM.tenant_id == ids["tenant_id"]),
            delete(OntologyVersionORM).where(OntologyVersionORM.tenant_id == ids["tenant_id"]),
            delete(OntologyORM).where(OntologyORM.tenant_id == ids["tenant_id"]),
            delete(DocumentChunkORM).where(DocumentChunkORM.tenant_id == ids["tenant_id"]),
            delete(DocumentORM).where(DocumentORM.tenant_id == ids["tenant_id"]),
            delete(KbCollectionORM).where(KbCollectionORM.tenant_id == ids["tenant_id"]),
            delete(TenantORM).where(TenantORM.id == ids["tenant_id"]),
        ):
            await db.execute(stmt)


async def _seed_doc(
    db: AsyncSession, *, tenant_id: uuid.UUID, collection_id: uuid.UUID, title: str, content: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """单文档单 chunk 直插（确定性，绕过流水线）；span=[0, len) 满足出处指针门禁。"""
    doc = DocumentORM(
        tenant_id=tenant_id,
        kb_collection_id=collection_id,
        title=title,
        source_type="upload",
        size_bytes=len(content.encode()),
        minio_key=f"raw-docs/{tenant_id}/{collection_id}/{uuid.uuid4()}/source.md",
        checksum_sha256=hashlib.sha256(f"{title}|{content}".encode()).hexdigest(),
        meta={"content": content},
        status="indexed",
    )
    db.add(doc)
    await db.flush()
    chunk = DocumentChunkORM(
        tenant_id=tenant_id,
        document_id=doc.id,
        seq=0,
        content=content,
        token_count=len(content) // 2,
        meta={"span": [0, len(content)]},
    )
    db.add(chunk)
    await db.flush()
    return doc.id, chunk.id


async def lite_search(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    collection_id: uuid.UUID,
    query: str,
    top_k: int = HIT_AT_K,
    max_hops: int = 2,
) -> HybridSearchResult:
    """knowledge.search lite 服务级组合（镜像 api/kb.py search 装配，绕过 HTTP 层）。"""
    rows = await get_class_hierarchy(db, tenant_id=tenant_id)
    hierarchy = build_class_hierarchy((row.iri, row.name, row.subclass_of) for row in rows)

    async def bm25_fn(q: str, k: int) -> list[SearchHit]:
        found = await bm25_search(db, tenant_id=tenant_id, query=q, top_k=k, collection_id=collection_id)
        return [
            SearchHit(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                content=row["content"],
                score=row["score"],
                doc_name=row["doc_name"],
                minio_key=row.get("minio_key"),
                span=row.get("span"),
            )
            for row in found
        ]

    async def vector_fn(q: str, k: int) -> list[SearchHit]:
        raise EmbeddingUnavailableError("baseline：向量路注入降级（评估不依赖 Ollama）")

    async def graph_fn(seeds: Sequence[SearchHit]) -> GraphExpansion:
        return await expand_graph(
            db,
            tenant_id=tenant_id,
            collection_id=collection_id,
            seeds=seeds,
            hierarchy=hierarchy,
            max_hops=max_hops,
        )

    return await hybrid_search(query, bm25=bm25_fn, vector=vector_fn, graph=graph_fn, top_k=top_k, mode="local")


async def test_citation_baseline_hit_at_5(kb_pg: async_sessionmaker[AsyncSession], baseline: dict) -> None:
    """golden QA 全量跑 knowledge.search lite：hit@5 ≥ 0.6（首跑即基线）+ 结果落 evaluation 契约。"""
    tenant_id: uuid.UUID = baseline["tenant_id"]
    collection_id: uuid.UUID = baseline["collection_id"]
    chunk_ids: dict[str, uuid.UUID] = baseline["chunk_ids"]
    key_by_id = {cid: key for key, cid in chunk_ids.items()}

    async with kb_pg() as db:
        # 图路契约专查：Feeder 种子 → 层次闭包扩展出 PowerDevice 端点（device_ledger）+ subclass_of 边
        graph_result = await lite_search(
            db, tenant_id=tenant_id, collection_id=collection_id, query="馈线 F001 故障隔离"
        )
        assert "graph" in graph_result.channels, "图路未生效：channels 缺 graph"
        path_chunk_ids = {cid for path in graph_result.graph_paths for cid in path.chunk_ids}
        assert chunk_ids["device_ledger"] in path_chunk_ids, "层次扩展未命中 PowerDevice 端点"
        edge_types = {rel.type for path in graph_result.graph_paths for rel in path.rels}
        assert "subclass_of" in edge_types, "类 IRI 链缺 subclass_of 边"

        latencies: list[float] = []
        case_rows: list[dict] = []
        for index, (query, expected_key) in enumerate(GOLDEN_QA):
            started = time.perf_counter()
            result = await lite_search(db, tenant_id=tenant_id, collection_id=collection_id, query=query)
            latencies.append((time.perf_counter() - started) * 1000)
            # 契约形状：degraded 标注 + citations 全字段 + answers 挂引用
            assert result.degraded is True and "vector_unavailable" in result.degraded_reasons
            assert result.mode_used == "local"
            rank = next(
                (i for i, hit in enumerate(result.hits, start=1) if hit.chunk_id == chunk_ids[expected_key]),
                None,
            )
            case_rows.append(
                {
                    "case_id": f"qa-{index + 1:02d}",
                    "query": query,
                    "expected": expected_key,
                    "rank": rank,
                    "latency_ms": latencies[-1],
                    "top_k_keys": [key_by_id.get(h.chunk_id, str(h.chunk_id)) for h in result.hits[:HIT_AT_K]],
                }
            )
        # 末例契约形状断言（citations/answers 全字段）
        final = await lite_search(db, tenant_id=tenant_id, collection_id=collection_id, query=GOLDEN_QA[0][0])
        assert final.hits and final.answer is not None
        citation = final.answer.citations
        assert citation and all(cid in chunk_ids.values() for cid in citation)
        assert final.answer.sentences and all(s.citations for s in final.answer.sentences)
        assert 0.0 < final.answer.confidence <= 1.0

    hit_total = sum(1 for row in case_rows if row["rank"] is not None)
    hit_at_5 = round(hit_total / len(GOLDEN_QA), 4)
    p50 = round(statistics.median(latencies), 1)
    print(
        f"\n[citation-baseline] hit@{HIT_AT_K}={hit_at_5} ({hit_total}/{len(GOLDEN_QA)}) "
        f"p50_latency_ms={p50} max_latency_ms={round(max(latencies), 1)} version={BASELINE_VERSION}"
    )
    for row in case_rows:
        print(
            f"[case {row['case_id']}] rank={row['rank']} latency_ms={round(row['latency_ms'], 1)} "
            f"query={row['query']!r} expected={row['expected']} top{HIT_AT_K}={row['top_k_keys']}"
        )
    assert hit_at_5 >= BASELINE_FLOOR, f"hit@{HIT_AT_K}={hit_at_5} 低于基线下限 {BASELINE_FLOOR}"

    # 结果落 evaluation 契约（08 §7.3：run 主指标 + 逐 case result）
    async with kb_pg() as db, db.begin():
        run = EvaluationRunORM(
            tenant_id=tenant_id,
            benchmark_type="retrieval_qa",
            benchmark_version=BASELINE_VERSION,
            trigger_ref={"source": "tests/kb/test_citation_baseline.py", "note": "首跑即基线（BM25+图，向量路降级）"},
            metrics={
                f"hit_at_{HIT_AT_K}": hit_at_5,
                "hit_total": hit_total,
                "qa_total": len(GOLDEN_QA),
                "p50_latency_ms": p50,
                "max_latency_ms": round(max(latencies), 1),
                "channels": sorted(graph_result.channels),
            },
            passed=hit_at_5 >= BASELINE_FLOOR,
        )
        db.add(run)
        await db.flush()
        for row in case_rows:
            db.add(
                EvaluationResultORM(
                    tenant_id=tenant_id,
                    run_id=run.id,
                    case_id=row["case_id"],
                    metrics={
                        "rank": row["rank"],
                        f"hit_at_{HIT_AT_K}": row["rank"] is not None,
                        "latency_ms": round(row["latency_ms"], 1),
                    },
                    verdict="pass" if row["rank"] is not None else "fail",
                    detail={"query": row["query"], "expected": row["expected"], "top_k": row["top_k_keys"]},
                )
            )
        saved = (await db.execute(select(EvaluationRunORM).where(EvaluationRunORM.id == run.id))).scalar_one()
    assert saved.passed is True
    assert saved.metrics[f"hit_at_{HIT_AT_K}"] == hit_at_5

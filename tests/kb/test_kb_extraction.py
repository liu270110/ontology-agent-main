"""M2.5 三步执行器用例（extract/align/validate）：候选双写幂等 + 三级对齐 + SHACL/证据双门禁。

- 纯函数用例（术语对齐规范化匹配）零外部依赖；
- 执行器用例直连本地 PG（不可达即跳过，同 tests/kb/test_kb.py 夹具纪律），模型用
  FakeModelPort/脚本化桩确定性注入（无网络；真实 LLM 不做 mock），审核票据用 ReviewTicketService 真表。
psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sys
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.kb.api.kb import get_document
from services.kb.business.kb_extraction import (
    SEED_TTL_PATH,
    PruningReason,
    StepContext,
    load_seed_catalog,
    match_seed_class,
    prune_extraction,
    run_align,
    run_extract,
    run_validate,
)
from services.kb.business.kb_pipeline import M2_FULL_STEPS, run_pipeline
from services.kb.business.prompts import extract_v3
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.orm import KbFact as KbFactORM
from services.kb.data.orm import KbPipelineStep as KbPipelineStepORM
from services.kb.retrieval.embed import EmbeddingUnavailableError
from services.platform.config import Settings
from services.platform.db import registry as orm_registry  # noqa: F401  # 全模块 ORM 入 metadata（ontologies FK 解析）
from services.platform.deps import Principal
from services.platform.llm.gateway import FakeModelPort
from services.platform.ports.model_port import ModelUnavailableError
from services.review.business.candidates import ReviewTicketService
from services.review.data.orm import ReviewTicket as ReviewTicketORM

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

PW = "http://ontology-agent.local/o/t1/power#"
CONTENT = "# 停电抽取联调\n馈线F001 由城东变电站供电。\n\n## 抢修工单\n工单OO-123456 已创建，工单状态为 created。\n"


def _seed_class_index(iri: str) -> int:
    """K24 序号口径（docs/Agent/13 §30）：种子类在声明序（=v3 目录渲染序=映射序）中的 1 起序号。"""
    return next(i for i, (c_iri, _, _) in enumerate(load_seed_catalog().classes, start=1) if c_iri == iri)


IDX_FEEDER = _seed_class_index(f"{PW}Feeder")
IDX_OUTAGE_ORDER = _seed_class_index(f"{PW}OutageOrder")
IDX_SUBSTATION = _seed_class_index(f"{PW}Substation")
IDX_POWER_DEVICE = _seed_class_index(f"{PW}PowerDevice")
IDX_TRANSFORMER = _seed_class_index(f"{PW}Transformer")
# FakeModelPort 序号桩（K24 适配）：v3 编号目录制下模型只回类序号（整数）——集成用例传序号，
# 经解析侧映射回 IRI 走既有链（传 IRI 文本会被硬幻觉门禁剪除=全池误剪）。
MODEL_KEYWORDS = {"馈线F001": IDX_FEEDER, "工单OO-123456": IDX_OUTAGE_ORDER}
MODEL_PROPERTIES = {"工单OO-123456": {"orderNo": "OO-123456", "hasStatus": "created"}}


# ---------------------------------------------------------------- 纯函数：术语对齐规范化匹配


def test_match_seed_class_exact_and_contains_and_miss():
    catalog = load_seed_catalog()
    assert SEED_TTL_PATH.exists()
    assert match_seed_class("馈线", catalog) == (f"{PW}Feeder", "exact")  # 中文标签精确
    assert match_seed_class("feeder", catalog) == (f"{PW}Feeder", "exact")  # 本地名小写精确
    assert match_seed_class("馈线 F001", catalog) == (f"{PW}Feeder", "contains")  # 去空格小写包含
    assert match_seed_class("恢复送电操作单", catalog) == (f"{PW}RestorePower", "contains")
    assert match_seed_class("工单OO-123456", catalog) is None  # 未命中 → 保留待审（不引向量）
    assert match_seed_class("   ", catalog) is None


# ---------------------------------------------------------------- PG 夹具（同 test_kb.py 纪律）


@pytest.fixture
async def kb_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过 kb 三步执行器用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def extract_env(
    kb_pg: async_sessionmaker[AsyncSession],
) -> AsyncIterator[dict]:
    """独立租户/集合/文档 + 确定性模型与审核服务；结束按 FK 逆序清理。"""
    async with kb_pg() as db, db.begin():
        tenant = TenantORM(name="kb-ext-租户", slug=f"kb-ext-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="kb-ext-库", embedding_model="bge-m3")
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
    # extract 前置：preprocess + chunk 就绪（走真实编排器与分块器）
    await run_pipeline(
        kb_pg,
        tenant_id=tenant.id,
        document_id=doc.id,
        steps=("preprocess", "chunk"),
        backoff=_instant_backoff,
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


def _ctx(
    kb_pg: async_sessionmaker[AsyncSession], env: dict, *, model: object = "default", embedder: object | None = None
) -> StepContext:
    return StepContext(
        session_factory=kb_pg,
        tenant_id=env["tenant_id"],
        document_id=env["document_id"],
        embedder=embedder,  # type: ignore[arg-type]
        model=env["model"] if model == "default" else model,  # type: ignore[arg-type]
        review=env["review"],
    )


class ScriptedModelPort:
    """脚本化模型桩：按 system 提示分派抽取/对齐响应（对齐/越界用例的确定性注入，零网络）。"""

    def __init__(self, extract: dict, align: dict | None = None) -> None:
        self._extract = extract
        self._align = align
        self.calls = 0

    async def complete_structured(self, *, system: str, user: str, json_schema: dict, **_: object) -> dict:
        self.calls += 1
        if "术语对齐" in system:
            return self._align if self._align is not None else {"mappings": []}
        return self._extract


class _ScriptedEmbedder:
    """脚本化嵌入桩：查表返回预设向量（表须覆盖将被嵌入的全部文本，嵌入顺序契约=输入序）。"""

    def __init__(self, table: dict[str, list[float]]) -> None:
        self._table = table
        self.batches: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [self._table[text] for text in texts]


class _FailingEmbedder:
    """降级嵌入桩：调用必抛 EmbeddingUnavailableError（降级契约真值路径，同参照 worktree 纪律）。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailableError("模拟：Ollama bge-m3 不可用")


async def _instant_backoff(attempt: int) -> None:
    return None  # 测试不等待真实退避（30s 起）


# ---------------------------------------------------------------- extract（§2.3）


async def test_extract_writes_candidate_facts_and_tickets_idempotent(
    kb_pg: async_sessionmaker[AsyncSession], extract_env: dict
) -> None:
    """候选双写：kb_facts(candidate)+evidence 信封 与 review_tickets(pending_review)+统一信封；
    重放（断点续跑语义）不重不漏：fact_key 幂等 + uk_review_one_open 幂等。"""
    ctx = _ctx(kb_pg, extract_env)
    await run_extract(ctx)
    model: FakeModelPort = extract_env["model"]

    async with kb_pg() as db:
        facts = (
            (
                await db.execute(
                    select(KbFactORM)
                    .where(KbFactORM.document_id == extract_env["document_id"])
                    .order_by(KbFactORM.subject)
                )
            )
            .scalars()
            .all()
        )
        tickets = (
            (
                await db.execute(
                    select(ReviewTicketORM)
                    .where(ReviewTicketORM.tenant_id == extract_env["tenant_id"])
                    .order_by(ReviewTicketORM.target_id)
                )
            )
            .scalars()
            .all()
        )
        chunk_rows = (await db.execute(select(DocumentChunkORM).order_by(DocumentChunkORM.seq))).scalars().all()
    assert len(chunk_rows) >= 1
    assert {f.subject for f in facts} == {"馈线F001", "工单OO-123456"}
    assert all(f.status == "candidate" and f.fact_type == "entity" for f in facts)
    assert all(f.violations == [] for f in facts)
    for fact in facts:  # 候选非成品 + 出处信封（source_ref 四元组；候选间不得互为证据）
        source_ref = fact.evidence["source_ref"]
        assert source_ref["document_id"] == str(extract_env["document_id"])
        assert source_ref["chunk_id"] in {str(c.id) for c in chunk_rows}
        assert source_ref["doc_version"] == 1 and len(source_ref["span"]) == 2
        assert fact.evidence["quote"] == fact.subject  # 引语=关键词本身（逐字命中，extract 只保留不裁决）
        assert fact.evidence["span"] and len(fact.evidence["span"]) == 2  # chunk 内定位可回指
        assert fact.meta["fact_key"] and fact.meta["template_ref"] == "kb_extract@v3"
        assert fact.meta["properties"] == MODEL_PROPERTIES.get(fact.subject, {})
    assert len(tickets) == len(facts)  # 一候选一 open 单
    assert all(t.target_type == "knowledge_instance" and t.status == "pending_review" for t in tickets)
    for ticket in tickets:  # 统一信封（standards/01 §5.3）
        assert ticket.payload["envelope_version"] == "v1"
        assert ticket.payload["template_ref"] == "kb_extract@v3"
        assert ticket.payload["payload"]["source_ref"]["chunk_id"]
        assert ticket.payload["payload"]["quote"]  # 引语随单透出（终审可直接对回原文）
        assert ticket.payload["payload"]["fact"]["violations"] == []  # 干净候选：fact 面零留痕（K21 P2-3）
        assert ticket.payload["confidence"] == 0.9
        assert ticket.payload["review"] == {"state": "pending_review"}
    calls_after_first = model.calls

    await run_extract(ctx)  # 重放：同输入恒同输出 → 不重不漏
    async with kb_pg() as db:
        facts_again = (
            await db.execute(
                select(func.count()).select_from(KbFactORM).where(KbFactORM.document_id == extract_env["document_id"])
            )
        ).scalar_one()
        tickets_again = (
            await db.execute(
                select(func.count())
                .select_from(ReviewTicketORM)
                .where(ReviewTicketORM.tenant_id == extract_env["tenant_id"])
            )
        ).scalar_one()
    assert (facts_again, tickets_again) == (len(facts), len(tickets))
    assert model.calls > calls_after_first  # LLM 确实重新调用（幂等在落库侧）


async def test_extract_fails_without_model_port(kb_pg: async_sessionmaker[AsyncSession], extract_env: dict) -> None:
    """无模型端口 → 5002 ModelUnavailableError（步级重试耗尽冻结，配置后可重跑，不做 mock）。"""
    with pytest.raises(ModelUnavailableError):
        await run_extract(_ctx(kb_pg, extract_env, model=None))


# ---------------------------------------------------------------- align（§2.4）+ validate（§2.6）


async def test_align_matches_seed_classes_and_keeps_miss_pending(
    kb_pg: async_sessionmaker[AsyncSession], extract_env: dict
) -> None:
    ctx = _ctx(kb_pg, extract_env)
    await run_extract(ctx)
    await run_align(ctx)
    async with kb_pg() as db:
        rows = (
            (await db.execute(select(KbFactORM).where(KbFactORM.document_id == extract_env["document_id"])))
            .scalars()
            .all()
        )
        facts = {f.subject: f for f in rows}
    feeder = facts["馈线F001"]
    assert feeder.aliases == [f"{PW}Feeder"]  # 命中 → aliases 补类 IRI
    assert feeder.subject_type == f"{PW}Feeder"  # subject_type 归一
    assert feeder.meta["align"] == {  # 一级决策记录（tier/status/reason 全量可追溯；template_ref 提示词治理）
        "class": f"{PW}Feeder",
        "rule": "contains",
        "tier": 1,
        "status": "aligned",
        "reason": None,
        "ref": "services/seeds/power_seed.ttl@v1",
        "template_ref": "kb_align@v1",
    }
    assert feeder.status == "candidate"
    order = facts["工单OO-123456"]  # 一级未命中；embedder 未装配跳二级；FakeModelPort 无 mappings → 三级降级待审
    assert order.aliases == [] and order.status == "candidate"
    assert order.meta["align"]["class"] is None
    assert order.meta["align"]["tier"] is None and order.meta["align"]["status"] == "needs_review"
    assert order.meta["align"]["reason"] == "LLM 判定不可用或输出不合法"
    # LLM 已给全量 IRI 的候选同样归一（subject_type 在种子类集内 → 不漂移）
    assert order.subject_type == f"{PW}OutageOrder"


async def test_validate_shacl_gate_rejects_violations_and_writes_gate_result(
    kb_pg: async_sessionmaker[AsyncSession], extract_env: dict
) -> None:
    ctx = _ctx(kb_pg, extract_env)
    await run_extract(ctx)
    await run_align(ctx)
    await run_validate(ctx)
    doc_filter = KbFactORM.document_id == extract_env["document_id"]
    async with kb_pg() as db:
        facts = {f.subject: f for f in (await db.execute(select(KbFactORM).where(doc_filter))).scalars().all()}
        tickets = {t.target_id: t for t in (await db.execute(select(ReviewTicketORM))).scalars().all()}
    assert all(facts[name].status == "candidate" for name in facts)  # 合规候选：留待人工终审
    assert all(facts[name].violations == [] for name in facts)
    assert all(tickets[facts[name].id].payload["gate_result"]["conforms"] is True for name in facts)
    assert all(tickets[facts[name].id].payload["gate_result"]["violation_count"] == 0 for name in facts)  # P2-2 自洽

    # 注入明确违规（R002 状态枚举）后重跑 validate（候选态才参与门禁）
    async with kb_pg() as db, db.begin():
        order = (await db.execute(select(KbFactORM).where(KbFactORM.subject == "工单OO-123456"))).scalar_one()
        meta = dict(order.meta or {})
        meta["properties"] = {**meta["properties"], "hasStatus": "flying"}
        order.meta = meta
    await run_validate(ctx)
    async with kb_pg() as db:
        order = (
            (
                await db.execute(
                    select(KbFactORM).where(
                        KbFactORM.document_id == extract_env["document_id"],
                        KbFactORM.subject == "工单OO-123456",
                    )
                )
            )
            .scalars()
            .one()
        )
        ticket = (
            (await db.execute(select(ReviewTicketORM).where(ReviewTicketORM.target_id == order.id))).scalars().one()
        )
    assert order.status == "rejected"  # kb_facts.status 枚举内取值；任何路径不写 authoritative
    assert order.violations and any("InConstraintComponent" in (v.get("constraint") or "") for v in order.violations)
    gate = ticket.payload["gate_result"]
    assert gate["conforms"] is False and gate["violation_count"] == 1  # R002 枚举违例恰一条（精确期望）
    assert gate["mark_count"] == 0  # 无剪枝留痕：mark_count 与 violation_count 分列自洽（K21 P2-2）
    assert gate["violation_count"] + gate["mark_count"] == len(order.violations)  # 计数拆分不漏不重
    assert gate["shapes"] == "services/seeds/power_seed.ttl@v1" and gate["checked_at"]
    assert ticket.status == "pending_review"  # 仍留人工终审队列


# ---------------------------------------------------------------- 纯函数：门禁子类闭包（回归 2026-09-27）


def test_门禁_子类实例命中父类形状_闭包并入不漏检(tmp_path):
    """回归：sh:targetClass 子类展开只看数据图内 subClassOf 公理——门禁必须把种子图作为
    tbox 闭包传入，否则子类候选（断路器）违反父类形状（设备编号必填）时静默放水
    （core/shacl.validate 文档同源；2026-09-27 实测复现）。"""
    from services.kb.business.kb_extraction import _CandidateRef, _gate_candidate

    seed_ttl = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix pw: <http://ontology-agent.local/o/t1/power#> .

pw:PowerDevice a owl:Class ; rdfs:label "电力设备"@zh .
pw:Breaker a owl:Class ; rdfs:subClassOf pw:PowerDevice ; rdfs:label "断路器"@zh .
pw:deviceCode a owl:DatatypeProperty ; rdfs:domain pw:PowerDevice ; rdfs:range xsd:string .
pw:PowerDeviceShape a sh:NodeShape ;
    sh:targetClass pw:PowerDevice ;
    sh:property [ sh:path pw:deviceCode ; sh:minCount 1 ; sh:name "设备编号必填"@zh ; ] .
"""
    seed_file = tmp_path / "mini_seed.ttl"
    seed_file.write_text(seed_ttl, encoding="utf-8")
    catalog = load_seed_catalog(seed_file)
    dev_iri = f"{PW}Breaker"

    missing = _CandidateRef(
        id=uuid.uuid4(),
        subject="断路器B12",
        predicate=None,
        object=None,
        subject_type=dev_iri,
        canonical_name=None,
        meta={},
    )
    assert not _gate_candidate(missing, catalog).conforms  # 闭包并入：子类实例命中父类必填

    ok = _CandidateRef(
        id=uuid.uuid4(),
        subject="断路器B12",
        predicate=None,
        object=None,
        subject_type=dev_iri,
        canonical_name=None,
        meta={"properties": {"deviceCode": "B12"}},
    )
    assert _gate_candidate(ok, catalog).conforms  # 合规候选照常放行


# ---------------------------------------------------------------- D1 证据逐字门禁 + D2 三级对齐


async def test_validate_marks_evidence_not_in_chunk_without_blocking_review(
    kb_pg: async_sessionmaker[AsyncSession], extract_env: dict
) -> None:
    """D1 证据逐字门禁（层轴验收 P1-3 等价）：引语未逐字命中 → evidence_not_in_chunk + rejected；
    无引语门禁空转不误判；违例不阻塞步、单据仍留人工终审队列。"""
    model = ScriptedModelPort(
        {
            "candidates": [
                {  # 引语杜撰：不在任何 chunk 内（Feeder 无 shape，SHACL 本身合规 → 隔离出规则侧违例）
                    "kind": "entity",
                    "name": "馈线F001",
                    "ontology_class": IDX_FEEDER,  # K24 序号口径：解析侧映射回 IRI 后走既有链
                    "confidence": 0.9,
                    "evidence": "这句引语纯属模型杜撰",
                },
                {  # 无引语（旧模板/确定性桩形态）：无可证伪 → 不判违例
                    "kind": "entity",
                    "name": "变压器T-09",
                    "ontology_class": IDX_TRANSFORMER,
                    "confidence": 0.8,
                },
            ]
        }
    )
    ctx = _ctx(kb_pg, extract_env, model=model)
    await run_extract(ctx)
    await run_validate(ctx)
    async with kb_pg() as db:
        facts = (
            (await db.execute(select(KbFactORM).where(KbFactORM.tenant_id == extract_env["tenant_id"]))).scalars().all()
        )
        tickets = {
            t.target_id: t
            for t in (
                await db.execute(select(ReviewTicketORM).where(ReviewTicketORM.tenant_id == extract_env["tenant_id"]))
            )
            .scalars()
            .all()
        }
    haunted = [f for f in facts if f.subject == "馈线F001"]
    clean = [f for f in facts if f.subject == "变压器T-09"]
    assert haunted and clean
    for fact in haunted:
        assert fact.evidence["quote"] == "这句引语纯属模型杜撰" and fact.evidence["span"] is None  # 保留不裁决
        assert [v["rule"] for v in fact.violations] == ["evidence_not_in_chunk"]
        assert fact.status == "rejected"  # 与 SHACL 违例同语义：标记供终审（不写 authoritative）
        gate = tickets[fact.id].payload["gate_result"]
        assert gate["conforms"] is False and gate["violations"][0]["rule"] == "evidence_not_in_chunk"
        assert tickets[fact.id].status == "pending_review"  # 不阻塞进审
    for fact in clean:
        assert fact.violations == [] and fact.status == "candidate"


async def test_align_tier2_embedding_cosine_aligns_pending_names(
    kb_pg: async_sessionmaker[AsyncSession], extract_env: dict
) -> None:
    """D2 二级：一级未命中 × 嵌入余弦 ≥ 阈值 → tier=2 对齐；model 未装配跳三级（降级不失败）。"""
    catalog = load_seed_catalog()
    texts = sorted({text for _, label, local in catalog.classes for text in (label, local)})
    dim = len(texts) + 1
    table = {text: [1.0 if i == j else 0.0 for j in range(dim)] for i, text in enumerate(texts)}
    table["工单OO-123456"] = list(table["停电工单"])  # 与「停电工单」标签同向：余弦=1.0 ≥ 0.92
    await run_extract(_ctx(kb_pg, extract_env))  # FakeModelPort：馈线F001 / 工单OO-123456 两候选
    await run_align(_ctx(kb_pg, extract_env, model=None, embedder=_ScriptedEmbedder(table)))
    async with kb_pg() as db:
        facts = {
            f.subject: f
            for f in (await db.execute(select(KbFactORM).where(KbFactORM.tenant_id == extract_env["tenant_id"])))
            .scalars()
            .all()
        }
    feeder, order = facts["馈线F001"], facts["工单OO-123456"]
    assert feeder.meta["align"]["tier"] == 1  # 对照：二级只接手一级未命中
    assert order.subject_type == f"{PW}OutageOrder" and order.aliases == [f"{PW}OutageOrder"]
    assert order.meta["align"]["tier"] == 2 and order.meta["align"]["rule"] == "embed"
    assert order.meta["align"]["status"] == "aligned" and order.meta["align"]["reason"] == "cosine=1.0000≥0.92"


async def test_align_tier3_llm_whitelist_boundary_rejection(
    kb_pg: async_sessionmaker[AsyncSession], extract_env: dict
) -> None:
    """D2 三级：LLM 判定过白名单校验（种子类名清单）才采纳；越界映射一律弃 → needs_review。"""
    model = ScriptedModelPort(
        {
            "candidates": [
                {  # 两者一级均未命中（无类标签/本地名包含关系）→ 落二/三级（embedder 未装配跳二级）
                    "kind": "entity",
                    "name": "配网环网柜",
                    "ontology_class": IDX_POWER_DEVICE,  # K24 序号口径：映射回 IRI 后走既有链
                    "confidence": 0.9,
                },
                {"kind": "entity", "name": "神秘设备", "ontology_class": IDX_POWER_DEVICE, "confidence": 0.8},
            ]
        },
        align={
            "mappings": [
                {"name": "配网环网柜", "target": "电力设备"},
                {"name": "神秘设备", "target": "BogusClass"},  # 越界 → 规则拒
            ]
        },
    )
    ctx = _ctx(kb_pg, extract_env, model=model)
    await run_extract(ctx)
    await run_align(ctx)
    async with kb_pg() as db:
        facts = {
            f.subject: f
            for f in (await db.execute(select(KbFactORM).where(KbFactORM.tenant_id == extract_env["tenant_id"])))
            .scalars()
            .all()
        }
    aligned, rejected = facts["配网环网柜"], facts["神秘设备"]
    assert aligned.subject_type == f"{PW}PowerDevice" and aligned.aliases == [f"{PW}PowerDevice"]
    assert aligned.meta["align"]["tier"] == 3 and aligned.meta["align"]["rule"] == "llm"
    assert aligned.meta["align"]["status"] == "aligned" and aligned.meta["align"]["reason"] == "LLM 判定过规则校验"
    assert rejected.subject_type == f"{PW}PowerDevice"  # 越界弃：subject_type 不被改写
    assert rejected.aliases == [] and rejected.status == "candidate"  # 保留待审（不失败）
    assert rejected.meta["align"]["tier"] is None and rejected.meta["align"]["status"] == "needs_review"
    assert "越界" in (rejected.meta["align"]["reason"] or "")


async def test_align_embed_unavailable_skips_tier2_degrades_not_fails(
    kb_pg: async_sessionmaker[AsyncSession], extract_env: dict
) -> None:
    """D2 降级契约：嵌入路不可用 → 二级整级跳过，步不失败；一级未命中保留待审（model 未装配跳三级）。"""
    await run_extract(_ctx(kb_pg, extract_env))
    await run_align(_ctx(kb_pg, extract_env, model=None, embedder=_FailingEmbedder()))
    async with kb_pg() as db:
        facts = {
            f.subject: f
            for f in (await db.execute(select(KbFactORM).where(KbFactORM.tenant_id == extract_env["tenant_id"])))
            .scalars()
            .all()
        }
    order = facts["工单OO-123456"]  # 一级未命中 + 二级不可用 + 三级未装配 → 保留待审
    assert order.status == "candidate" and order.aliases == []
    assert order.meta["align"]["status"] == "needs_review" and order.meta["align"]["tier"] is None
    assert order.meta["align"]["reason"] is None  # 各级均无着落（无异常上抛）
    assert facts["馈线F001"].meta["align"]["tier"] == 1  # 一级不受降级影响


# ---------------------------------------------------------------- extract 空候选显式信号（2026-10-07 静态清账批单元四）


def _detail_principal(env: dict) -> Principal:
    return Principal(
        {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(env["tenant_id"]),
            "roles": ["editor"],
            "scopes": ["kb:read"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


async def test_extract_空候选_全流程indexed与详情信号(
    kb_pg: async_sessionmaker[AsyncSession], extract_env: dict
) -> None:
    """空候选路径：零候选仍走完 align/validate/bm25 → indexed（空转既有契约不变）；文档 meta
    与详情 DTO extract_empty=true（信号可见可检索）。embedder 未配置走软降级（BM25-only 同
    degraded_doc 先例），不影响本信号断言。"""
    empty_model = ScriptedModelPort(extract={"candidates": []})  # 每 chunk 恒零候选
    report = await run_pipeline(
        kb_pg,
        tenant_id=extract_env["tenant_id"],
        document_id=extract_env["document_id"],
        embedder=None,
        model=empty_model,  # type: ignore[arg-type]
        review=extract_env["review"],
        steps=M2_FULL_STEPS,
        backoff=_instant_backoff,
    )
    assert report.document_status == "indexed"  # 空转既有契约：文档照常终态 indexed
    async with kb_pg() as db:
        detail = await get_document(extract_env["document_id"], _detail_principal(extract_env), db)
        assert detail.data.status == "indexed"
        assert detail.data.extract_empty is True  # 详情 DTO 透出零候选信号
        facts = (
            (await db.execute(select(KbFactORM).where(KbFactORM.tenant_id == extract_env["tenant_id"]))).scalars().all()
        )
    assert facts == []  # 零候选：kb_facts 无产物


async def test_extract_有候选_信号false(kb_pg: async_sessionmaker[AsyncSession], extract_env: dict) -> None:
    """有候选路径：FakeModelPort 正常产出 → extract_empty=false（不误报）。"""
    report = await run_pipeline(
        kb_pg,
        tenant_id=extract_env["tenant_id"],
        document_id=extract_env["document_id"],
        embedder=None,
        model=extract_env["model"],
        review=extract_env["review"],
        steps=M2_FULL_STEPS,
        backoff=_instant_backoff,
    )
    assert report.document_status == "indexed"
    async with kb_pg() as db:
        detail = await get_document(extract_env["document_id"], _detail_principal(extract_env), db)
        assert detail.data.extract_empty is False
        facts = (
            (await db.execute(select(KbFactORM).where(KbFactORM.tenant_id == extract_env["tenant_id"]))).scalars().all()
        )
    assert len(facts) >= 1  # 有候选


# ---------------------------------------------------------------- K21 事后剪枝（docs/Agent/13 §27）


def test_prune_extraction_out_of_taxonomy_marks_reason():
    """K21-a：schema 外自报类 → out_of_taxonomy 剪除+原因码；标签形可解析（match_seed_class
    同源判定）不误剪；残缺条目（缺 name）透传不归剪枝管。剪枝侧类目判定只认 exact/
    glossary_alias 两级（复核 P3-4：比 align 严——contains 字面擦边不再免剪，交终审）。"""
    catalog = load_seed_catalog()
    result = prune_extraction(
        [
            {"kind": "entity", "name": "馈线F001", "ontology_class": f"{PW}Feeder", "confidence": 0.9},
            {  # schema 外类（上游「宽松放行」教训的剪除面）：IRI 不可解析 → 剪除
                "kind": "entity",
                "name": "神秘设备",
                "ontology_class": "http://schema.example/Starship",
                "confidence": 0.8,
            },
            {"kind": "entity", "name": "城东站", "ontology_class": "变电站", "confidence": 0.7},  # 标签形 exact：在目
            {  # contains 级（「城东变电站」字面包含「变电站」）：align 会对齐，剪枝侧收严 → 剪除交终审
                "kind": "entity",
                "name": "城东变电站甲",
                "ontology_class": "城东变电站",
                "confidence": 0.6,
            },
            {"kind": "entity", "name": "抢修单WO-7", "ontology_class": "抢修单", "confidence": 0.6},  # 术语别名级：在目
            {"kind": "entity", "confidence": 0.5},  # 残缺（缺 name）：透传 kept
        ],
        catalog,
    )
    assert [c.get("name") for c in result.kept] == ["馈线F001", "城东站", "抢修单WO-7", None]
    assert [(c["name"], reason) for c, reason in result.pruned] == [
        ("神秘设备", PruningReason.OUT_OF_TAXONOMY),
        ("城东变电站甲", PruningReason.OUT_OF_TAXONOMY),
    ]
    assert result.pruning_stats == {"out_of_taxonomy": 2}


def test_prune_extraction_keeps_valid_products_intact():
    """K21-a：全合法产物（实体+关系双端点在目）零剪除原序全保留；纯 IRI 集合口径=仅精确
    成员判定（无 SeedCatalog 时标签形不再可解析，预期剪除）。"""
    valid = [
        {"kind": "entity", "name": "馈线F001", "ontology_class": f"{PW}Feeder", "confidence": 0.9},
        {
            "kind": "relation",
            "name": "馈线F001",
            "ontology_class": f"{PW}Feeder",
            "predicate": "suppliedBy",
            "object": "城东变电站",
            "object_class": f"{PW}Substation",
            "confidence": 0.8,
        },
    ]
    result = prune_extraction(valid, load_seed_catalog())
    assert result.kept == tuple(valid)  # 合法产物全保留（原序、原对象）
    assert result.pruned == () and result.pruning_stats == {}
    strict = prune_extraction(
        [{"kind": "entity", "name": "城东站", "ontology_class": "变电站", "confidence": 0.7}],
        {f"{PW}Feeder"},  # 纯 IRI 集合口径：仅精确成员判定
    )
    assert strict.kept == ()  # 唯一候选被剪：保留集为空（精确期望，不自证）
    assert [reason for _, reason in strict.pruned] == [PruningReason.OUT_OF_TAXONOMY]
    assert strict.pruning_stats == {"out_of_taxonomy": 1}
    with pytest.raises(ValueError, match="裸字符串"):  # 裸字符串显式拒收（复核 P3-5：不静默拆字符集）
        prune_extraction([{"kind": "entity", "name": "馈线F001", "ontology_class": f"{PW}Feeder"}], f"{PW}Feeder")


def test_prune_extraction_duplicate_and_relation_endpoint():
    """K21-a：同键重复（同批先到先得，跨条目原序）与关系端点类不合法两形态；判定顺序=
    内容失效优先于键重复（单条单原因码）。"""
    catalog = load_seed_catalog()
    result = prune_extraction(
        [
            {"kind": "entity", "name": "馈线F001", "ontology_class": f"{PW}Feeder", "confidence": 0.9},
            {"kind": "entity", "name": "馈线F001", "ontology_class": f"{PW}Feeder", "confidence": 0.9},  # 同键再现
            {
                "kind": "relation",
                "name": "馈线F001",
                "ontology_class": f"{PW}Feeder",
                "predicate": "suppliedBy",
                "object": "神秘节点",
                "object_class": "http://schema.example/Bogus",  # 端点类 schema 外 → 剪除
                "confidence": 0.8,
            },
        ],
        catalog,
    )
    assert [c["name"] for c in result.kept] == ["馈线F001"]
    assert [(c["name"], reason) for c, reason in result.pruned] == [
        ("馈线F001", PruningReason.DUPLICATE),
        ("馈线F001", PruningReason.INVALID_RELATION_ENDPOINT),
    ]
    assert result.pruning_stats == {"duplicate": 1, "invalid_relation_endpoint": 1}


async def test_extract_pruned_items_marked_not_discarded(
    kb_pg: async_sessionmaker[AsyncSession], extract_env: dict
) -> None:
    """K21-b：剪除项不丢弃不阻塞——照常落 kb_facts(candidate) 进审，violations 带原因码留痕
    （只标记不裁决）；合法产物零标记不受影响（主流程不变，一候选一单）。"""
    model = ScriptedModelPort(
        {
            "candidates": [
                # K24 序号口径：合法序号映射回 IRI（干净候选）；直出 IRI 文本=违反序号口径的
                # 幻觉回包，经哨兵化后照常落 K21 剪枝留痕（硬幻觉门禁回归面）
                {"kind": "entity", "name": "馈线F001", "ontology_class": IDX_FEEDER, "confidence": 0.9},
                {
                    "kind": "entity",
                    "name": "神秘设备",
                    "ontology_class": "http://schema.example/Starship",
                    "confidence": 0.8,
                },
            ]
        }
    )
    await run_extract(_ctx(kb_pg, extract_env, model=model))
    async with kb_pg() as db:
        rows = (
            (await db.execute(select(KbFactORM).where(KbFactORM.tenant_id == extract_env["tenant_id"]))).scalars().all()
        )
        tickets = (
            (await db.execute(select(ReviewTicketORM).where(ReviewTicketORM.tenant_id == extract_env["tenant_id"])))
            .scalars()
            .all()
        )
    pruned = [f for f in rows if f.subject == "神秘设备"]
    valid = [f for f in rows if f.subject == "馈线F001"]
    assert pruned and valid  # 剪除不丢弃：与合法产物同批落候选
    ticket_by_target = {t.target_id: t for t in tickets}
    for fact in pruned:
        assert fact.status == "candidate"  # 剪除≠裁决：候选非成品交终审
        assert [v["rule"] for v in fact.violations] == ["out_of_taxonomy"]
        assert "不在种子类目" in fact.violations[0]["detail"]
        envelope = ticket_by_target[fact.id].payload["payload"]
        assert [v["rule"] for v in envelope["fact"]["violations"]] == ["out_of_taxonomy"]  # 留痕随单透出（P2-3）
    for fact in valid:
        assert fact.violations == [] and fact.status == "candidate"  # 合法产物零标记
        assert ticket_by_target[fact.id].payload["payload"]["fact"]["violations"] == []
    assert len(tickets) == 4  # 2 候选 × 2 chunks（fact_key 含 chunk_id 跨 chunk 不判重）：一候选一 open 单


async def test_extract_pruning_stats_meta_and_gate_preserves_marks(
    kb_pg: async_sessionmaker[AsyncSession], extract_env: dict
) -> None:
    """K21-b：pruning_stats 随抽取完成落账（Document.meta，extract_empty 同落点，精确计数）；
    validate 门禁回写保留剪枝留痕且不改变门禁裁决（无门禁违例时仍 candidate），gate_result
    拆 violation_count/mark_count 自洽（P2-2）；带留痕幽灵候选不进 §8.1 冲突分诊池（P2-1：
    同文档同主谓权威行在手也不触发 T1 封口/T2 工单——权威面零污染）。"""
    model = ScriptedModelPort(
        {
            "candidates": [
                {
                    "kind": "entity",
                    "name": "神秘设备",
                    "ontology_class": "http://schema.example/Starship",
                    "confidence": 0.8,
                }
            ]
        }
    )
    ctx = _ctx(kb_pg, extract_env, model=model)
    await run_extract(ctx)
    async with kb_pg() as db:
        doc = await db.get(DocumentORM, extract_env["document_id"])
    assert doc.meta["pruning_stats"] == {"out_of_taxonomy": 2}  # 2 chunks × 1 候选（fact_key 含 chunk_id）
    # 权威事实对照（P2-1）：同文档同主谓权威行在手——若幽灵候选未被整池排除，分诊会命中
    # T1 same_document：权威行被封口（meta.conflict_triage.state=superseded）+ superseded_by 边
    async with kb_pg() as db, db.begin():
        db.add(
            KbFactORM(
                tenant_id=extract_env["tenant_id"],
                document_id=extract_env["document_id"],
                fact_type="entity",
                subject="神秘设备",
                canonical_name="神秘设备",
                confidence=1.0,
                status="authoritative",
            )
        )
    await run_validate(ctx)  # 门禁回写：留痕跨 validate 保留；幽灵候选不进分诊池
    async with kb_pg() as db:
        rows_after = (
            (
                await db.execute(
                    select(KbFactORM).where(
                        KbFactORM.tenant_id == extract_env["tenant_id"], KbFactORM.status == "candidate"
                    )
                )
            )
            .scalars()
            .all()
        )
        auth = (
            (
                await db.execute(
                    select(KbFactORM).where(
                        KbFactORM.tenant_id == extract_env["tenant_id"], KbFactORM.status == "authoritative"
                    )
                )
            )
            .scalars()
            .one()
        )
        tickets = (
            (await db.execute(select(ReviewTicketORM).where(ReviewTicketORM.tenant_id == extract_env["tenant_id"])))
            .scalars()
            .all()
        )
    assert len(rows_after) == 2  # 2 chunks × 1 幽灵候选（kb_fact_relations 表未在测试 PG 建：边计数
    # 不可查——排除性由下方 meta/工单断言覆盖：T1 封口权威行 meta、T4/T3 标注候选 meta、T2 建单）
    assert auth.meta.get("conflict_triage") is None  # 幽灵候选未进分诊池：权威行未被 T1 封口
    assert not any(t.target_type == "conflict" for t in tickets)  # 无 T2 冲突工单（幽灵不比对面）
    ticket_by_target = {t.target_id: t for t in tickets}
    for fact in rows_after:
        assert [v["rule"] for v in fact.violations] == ["out_of_taxonomy"]  # 留痕未被门禁回写抹除
        assert fact.status == "candidate"  # 剪枝留痕不计入裁决（主流程不变）
        assert "conflict_triage" not in (fact.meta or {})  # T4/T3 亦未标注（整池排除，非仅挡 T2）
        gate = ticket_by_target[fact.id].payload["gate_result"]
        assert gate["conforms"] is True and gate["violation_count"] == 0 and gate["mark_count"] == 1  # P2-2 计数自洽


# ---------------------------------------------------------------- K24 序号→IRI 映射门禁（docs/Agent/13 §30 E-3）


def test_v3_catalog_numbering_matches_declaration_order():
    """K24-c 序不变式：v3 编号目录的类行号 = SeedCatalog.classes 声明序（声明序=渲染序=映射序，
    真实种子全量核对——解析侧 idx→IRI 映射表与提示词目录两处 enumerate 必须一致）。"""
    catalog = load_seed_catalog()
    class_lines = [line for line in extract_v3.render_catalog(catalog).splitlines() if re.match(r"- \d+\. 类 ", line)]
    assert len(class_lines) == len(catalog.classes)  # 类行与声明一一对应（属性行不入此列）
    for idx, (_iri, label, local) in enumerate(catalog.classes, start=1):
        assert class_lines[idx - 1] == f"- {idx}. 类 {label}（{local}）"


async def test_extract_v3_index_hit_maps_back_to_iri_end_to_end(
    kb_pg: async_sessionmaker[AsyncSession], extract_env: dict
) -> None:
    """K24-c 命中路径（端到端）：模型回目录序号（实体类+关系双端点）→ 解析侧在剪枝/落库前映射回
    种子类 IRI 走既有链——subject_type/object_type=IRI、violations=[]、template_ref=kb_extract@v3、
    evidence 引语照常保留、零剪除 pruning_stats 显式落账。"""
    model = ScriptedModelPort(
        {
            "candidates": [
                {
                    "kind": "entity",
                    "name": "馈线F001",
                    "ontology_class": IDX_FEEDER,
                    "confidence": 0.9,
                    "evidence": "馈线F001",
                },
                {
                    "kind": "relation",
                    "name": "馈线F001",
                    "ontology_class": IDX_FEEDER,
                    "predicate": "suppliedBy",
                    "object": "城东变电站",
                    "object_class": IDX_SUBSTATION,
                    "confidence": 0.8,
                    "evidence": "馈线F001",
                },
            ]
        }
    )
    ctx = _ctx(kb_pg, extract_env, model=model)
    await run_extract(ctx)
    async with kb_pg() as db:
        facts = (
            (await db.execute(select(KbFactORM).where(KbFactORM.document_id == extract_env["document_id"])))
            .scalars()
            .all()
        )
        doc = await db.get(DocumentORM, extract_env["document_id"])
    entities = [f for f in facts if f.fact_type == "entity"]
    relations = [f for f in facts if f.fact_type == "relation"]
    assert entities and relations  # 2 候选 × 每 chunk 各落一条
    for fact in entities:
        assert fact.subject_type == f"{PW}Feeder"  # 序号已映射回 IRI（非序号原文落库）
        assert fact.violations == [] and fact.status == "candidate"
        assert fact.meta["template_ref"] == "kb_extract@v3"
        assert fact.evidence["quote"] == "馈线F001"  # evidence 链不受映射影响
    for fact in relations:
        assert fact.subject_type == f"{PW}Feeder" and fact.object_type == f"{PW}Substation"  # 双端点映射
        assert fact.violations == []  # 端点在目：不触发 invalid_relation_endpoint
    assert doc.meta["pruning_stats"] == {}  # 零剪除显式落账（K21 口径）


async def test_extract_v3_out_of_range_index_pruned_with_mark(
    kb_pg: async_sessionmaker[AsyncSession], extract_env: dict
) -> None:
    """K24-c 越界路径：序号 0/超目录长度 → 复用 K21 剪枝管线 OUT_OF_TAXONOMY 留痕（detail 注
    「序号越界」），剪除不丢弃照常落候选进审（不加新枚举不 hard-raise）；pruning_stats 精确落账。"""
    catalog = load_seed_catalog()
    model = ScriptedModelPort(
        {
            "candidates": [
                {
                    "kind": "entity",
                    "name": "越界设备甲",
                    "ontology_class": len(catalog.classes) + 1,
                    "confidence": 0.9,
                },
                {
                    "kind": "entity",
                    "name": "越界设备乙",
                    "ontology_class": 0,  # 目录序从 1 起，0 必越界（falsy 陷阱不漏剪：映射层先行哨兵化）
                    "confidence": 0.8,
                },
            ]
        }
    )
    ctx = _ctx(kb_pg, extract_env, model=model)
    await run_extract(ctx)  # 不 hard-raise：双写/进审主流程不变
    async with kb_pg() as db:
        facts = (
            (await db.execute(select(KbFactORM).where(KbFactORM.tenant_id == extract_env["tenant_id"]))).scalars().all()
        )
        tickets = (
            (await db.execute(select(ReviewTicketORM).where(ReviewTicketORM.tenant_id == extract_env["tenant_id"])))
            .scalars()
            .all()
        )
        doc = await db.get(DocumentORM, extract_env["document_id"])
    assert {f.subject for f in facts} == {"越界设备甲", "越界设备乙"}  # 剪除不丢弃：照常落候选
    assert len(facts) == 4 and len(tickets) == 4  # 2 候选 × 2 chunks：一候选一 open 单
    ticket_by_target = {t.target_id: t for t in tickets}
    for fact in facts:
        assert fact.status == "candidate"  # 剪除≠裁决：候选非成品交终审
        assert [v["rule"] for v in fact.violations] == ["out_of_taxonomy"]
        assert "序号越界" in fact.violations[0]["detail"]
        envelope = ticket_by_target[fact.id].payload["payload"]
        assert [v["rule"] for v in envelope["fact"]["violations"]] == ["out_of_taxonomy"]  # 留痕随单透出
    assert doc.meta["pruning_stats"] == {"out_of_taxonomy": 4}  # 2 候选 × 2 chunks 精确计数


async def test_extract_v3_non_integer_index_pruned_with_mark(
    kb_pg: async_sessionmaker[AsyncSession], extract_env: dict
) -> None:
    """K24-c 非整数容错：文本/浮点序号与模型直出 IRI 文本（违反序号口径的幻觉回包）→ 同落 K21
    OUT_OF_TAXONOMY 留痕（detail 注「非整数」），不 hard-raise；哨兵串保留原值可回溯。"""
    model = ScriptedModelPort(
        {
            "candidates": [
                {"kind": "entity", "name": "文本序号候选", "ontology_class": "Feeder", "confidence": 0.9},
                {"kind": "entity", "name": "浮点序号候选", "ontology_class": 2.5, "confidence": 0.8},
                {"kind": "entity", "name": "直出IRI候选", "ontology_class": f"{PW}Feeder", "confidence": 0.7},
            ]
        }
    )
    ctx = _ctx(kb_pg, extract_env, model=model)
    await run_extract(ctx)
    async with kb_pg() as db:
        facts = (
            (await db.execute(select(KbFactORM).where(KbFactORM.tenant_id == extract_env["tenant_id"]))).scalars().all()
        )
    assert {f.subject for f in facts} == {"文本序号候选", "浮点序号候选", "直出IRI候选"}
    assert len(facts) == 6  # 3 候选 × 2 chunks（跨 chunk 不判重）
    for fact in facts:
        assert [v["rule"] for v in fact.violations] == ["out_of_taxonomy"]
        assert "非整数" in fact.violations[0]["detail"]
        assert (fact.subject_type or "").startswith("非整数:")  # 哨兵串=失效形态:原值，终审可回溯


def test_整值浮点2_0容错落穿映射为IRI():
    """替代复核发现 1 回归钉：2.0（jsonschema 合法 integer）应映射回 IRI 而非被剪除。"""
    from services.kb.business.kb_extraction import _map_class_indices, load_seed_catalog

    catalog = load_seed_catalog()
    prod_idx = dict(enumerate(catalog.classes, start=1))  # 与生产 enumerate 同源
    target_iri = prod_idx[2][0] if isinstance(prod_idx[2], tuple) else prod_idx[2]
    index = {2: target_iri}
    out = _map_class_indices([{"ontology_class": 2.0}], index)
    assert out == [{"ontology_class": target_iri}]  # 容错落穿：2.0 → int(2) → IRI

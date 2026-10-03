# tests/kb/test_rule_extraction.py
"""规则候选抽取通道用例（波次④切片 v1）：时序流程型文本 → 规则草案（OB2 规则层）。

- 纯内存：确定性 LLM 桩（FakeModelPort 式，恒返回预置结构化输出，零网络）+ 真种子 catalog
  （services/seeds/power_seed.ttl 经 load_seed_catalog）+ rdflib/pySHACL 同步自检；
- 落库用例跑 aiosqlite（生产同一 ORM/会话路径；JSONB/UUID 经本文件 @compiles shim 降编译，
  同 tests/kb/test_connector.py 先例；aiosqlite 未装则仅落库用例 skip）；
- 断言目标：结构化抽取解析（模板 rule_id/系统自留字段剥离/confidence 收敛）、risk_flag 恒
  True（类型级 Literal + 数据库级 CHECK 双保险，宪法底线 3）、evidence 逐字校验（str.find
  命中/未命中）、target_class 类目校验（越类目抽取标记 + 自检打回 + 标签归一）、draft_shacl
  语法自检（合法 Turtle 过 / 非法 Turtle 打回 / 引擎不可执行打回）、落库往返与评审单双写幂等
  （candidate_type=rule_draft + risk_flag=true 随单透出）。

psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略（仓库同款）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUuid
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from services.iam.data.orm import Tenant as TenantORM
from services.kb.business.kb_extraction import load_seed_catalog
from services.kb.business.rule_extraction import (
    RULE_EXTRACT_TEMPLATE_REF,
    RuleCandidate,
    extract_rule_candidates,
    persist_rule_candidates,
    validate_draft,
)
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.rule_orm import KbRuleCandidate
from services.platform.db import registry as orm_registry  # noqa: F401  # 全模块 ORM 入 metadata（documents FK 解析）
from services.platform.ports.model_port import ModelUnavailableError
from services.review.business.candidates import ReviewTicketService
from services.review.data.orm import ReviewTicket as ReviewTicketORM

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    import aiosqlite  # noqa: F401

    _HAS_AIOSQLITE = True
except ImportError:  # 本地开发依赖缺失：仅落库用例跳过（CI requirements 未含 aiosqlite）
    _HAS_AIOSQLITE = False

sqlite_needed = pytest.mark.skipif(not _HAS_AIOSQLITE, reason="aiosqlite 未安装：规则候选落库用例")

# ── SQLite 方言 shim（仅测试进程：PG 专列类型建表降编译；同 test_connector.py 先例）──


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSONB"


@compiles(PgUuid, "sqlite")
def _sqlite_uuid(type_: Any, compiler: Any, **kw: Any) -> str:
    return "CHAR(32)"


PW = "http://ontology-agent.local/o/t1/power#"

CHUNK = (
    "## 检修规程（节选）\n"
    "工单OO-654321 由值班调度员创建后，方可派发现场抢修。\n"
    "同一台设备不得同时挂牌检修与带电作业。\n"
    "工单状态仅允许沿 created、dispatched、in_progress、resolved、closed 顺序流转。\n"
)
QUOTE_IN_CHUNK = "同一台设备不得同时挂牌检修与带电作业。"
QUOTE_FABRICATED = "设备必须先完成绝缘遮蔽方可接触导线（原文无此句）"

# 合法草案：R004 同式（pw:OutageOrder × pw:orderNo 格式约束）——pySHACL 空数据图可执行
DRAFT_VALID = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix pw: <http://ontology-agent.local/o/t1/power#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
[] a sh:NodeShape ;
    sh:targetClass pw:OutageOrder ;
    sh:property [
        sh:path pw:orderNo ;
        sh:datatype xsd:string ;
        sh:pattern "^OO-[0-9]{6}$" ;
        sh:minCount 1 ;
    ] .
"""
DRAFT_BAD_TTL = "这不是 Turtle {{{ 缺少前缀与三元组"
DRAFT_TARGET_OUT = (
    "@prefix sh: <http://www.w3.org/ns/shacl#> .\n"
    "@prefix pw: <http://ontology-agent.local/o/t1/power#> .\n"
    "[] a sh:NodeShape ; sh:targetClass pw:VoltageLevel ;\n"
    "    sh:property [ sh:path pw:orderNo ; sh:minCount 1 ] .\n"
)

# 确定性抽取桩预置输出（3 条：合法+逐字命中 / 越类目+幻觉引语 / 标签归一+confidence 越界收敛）
STUB_PAYLOAD: dict[str, Any] = {
    "rule_candidates": [
        {
            "kind": "state_transition",
            "trigger": "工单状态从 created 迁出",
            "consequence": "只允许沿 created→dispatched→in_progress→resolved→closed 顺序迁移",
            "target_class": f"{PW}OutageOrder",
            "evidence": "工单状态仅允许沿 created、dispatched、in_progress、resolved、closed 顺序流转。",
            "draft_shacl": DRAFT_VALID,
            "confidence": 0.92,
            # 系统自留字段注入攻击面：LLM 试图置 risk_flag=False / 自带 rule_id → 构造前强制剥离
            "risk_flag": False,
            "rule_id": "LLM-SELF-ASSIGNED",
        },
        {
            "kind": "exclusion",
            "trigger": "同一台设备同时挂牌检修与带电作业",
            "consequence": "两作业状态互斥，禁止并存",
            "target_class": "带电作业区",  # 不在种子类目（越类目：抽取标记 + 自检打回）
            "evidence": QUOTE_FABRICATED,  # 幻觉引语（str.find 未命中）
            "draft_shacl": DRAFT_TARGET_OUT,
            "confidence": 0.7,
        },
        {
            "kind": "invariant",
            "trigger": "馈线处于运行状态",
            "consequence": "馈线必须隶属于恰好一个变电站（不变式）",
            "target_class": "馈线",  # 中文标签 → 术语对齐一级归一到 {PW}Feeder
            "evidence": QUOTE_IN_CHUNK,
            "draft_shacl": DRAFT_VALID,
            "confidence": 1.7,  # 越界 → 代码侧收敛到 [0, 1]
        },
    ]
}


class RuleStubModelPort:
    """确定性规则抽取桩（FakeModelPort 式）：恒返回预置结构化输出，记录调用与 trace_id，零网络。"""

    def __init__(self, payload: dict[str, Any] | None = None, *, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error
        self.calls = 0
        self.last_trace_id: str | None = None

    async def complete_structured(
        self, *, system: str, user: str, json_schema: dict, trace_id: str | None = None, **_: object
    ) -> dict[str, Any]:
        self.calls += 1
        self.last_trace_id = trace_id
        if self._error is not None:
            raise self._error
        assert "本体引导清单" in user and "抽取文本" in user  # 本体引导=种子类目注入提示词（kb_extract 同式）
        return self._payload or {}


# ---------------------------------------------------------------- 结构化抽取解析


async def test_结构化抽取解析_模板id_系统自留字段剥离_风险恒真() -> None:
    candidates = await extract_rule_candidates(CHUNK, load_seed_catalog(), RuleStubModelPort(STUB_PAYLOAD),
                                               trace_id="trace-rule-1")
    assert [c.rule_id for c in candidates] == ["RD-001", "RD-002", "RD-003"]  # 模板 id（声明序）
    assert [c.kind for c in candidates] == ["state_transition", "exclusion", "invariant"]
    assert all(c.risk_flag is True for c in candidates)  # 底线 3：类型级恒真（注入 False 已剥离）
    assert candidates[0].target_class == f"{PW}OutageOrder"  # 种子类 IRI 直认
    assert candidates[2].target_class == f"{PW}Feeder"  # 中文标签「馈线」归一到类 IRI
    assert candidates[2].confidence == 1.0  # 越界 confidence 收敛到 [0, 1]
    assert candidates[0].evidence.startswith("工单状态仅允许沿")  # 逐字引语原样保留


def test_risk_flag_类型级False构造即拒() -> None:
    with pytest.raises(ValidationError):
        RuleCandidate(
            rule_id="RD-001",
            kind="invariant",
            trigger="t",
            consequence="c",
            target_class=f"{PW}Feeder",
            evidence="e",
            draft_shacl=DRAFT_VALID,
            risk_flag=False,  # type: ignore[arg-type]  # Literal[True]：构造即 ValidationError
        )


async def test_LLM不可达与输出结构不可用抛_ModelUnavailableError() -> None:
    with pytest.raises(ModelUnavailableError):
        await extract_rule_candidates(CHUNK, load_seed_catalog(), None)  # 端口未装配
    with pytest.raises(ModelUnavailableError):
        await extract_rule_candidates(
            CHUNK, load_seed_catalog(), RuleStubModelPort(error=ModelUnavailableError("模拟：LLM 不可达"))
        )
    with pytest.raises(ModelUnavailableError):  # 缺 rule_candidates 数组=输出结构不可用（5002 同口径）
        await extract_rule_candidates(CHUNK, load_seed_catalog(), RuleStubModelPort({"candidates": []}))


# ---------------------------------------------------------------- 门禁：evidence 逐字 + target_class 类目


async def test_evidence逐字校验_命中定位_未命中标记() -> None:
    candidates = await extract_rule_candidates(CHUNK, load_seed_catalog(), RuleStubModelPort(STUB_PAYLOAD))
    hit, miss = candidates[0], candidates[1]
    assert hit.evidence.startswith("工单状态仅允许沿")  # 候选引语=工单状态句
    idx = CHUNK.find(hit.evidence)
    assert idx >= 0 and hit.evidence_span == [idx, idx + len(hit.evidence)]  # str.find 逐字定位 [start, end)
    assert hit.violations == []
    assert miss.evidence_span is None  # 幻觉引语：不可回指
    assert {v["rule"] for v in miss.violations} >= {"evidence_not_in_chunk"}  # 只标记供终审，不裁决
    assert QUOTE_IN_CHUNK in CHUNK  # 夹具自检：互斥句原文在场（供其它用例引用）


async def test_target_class越类目_抽取标记_自检打回() -> None:
    catalog = load_seed_catalog()
    candidates = await extract_rule_candidates(CHUNK, catalog, RuleStubModelPort(STUB_PAYLOAD))
    stray = candidates[1]
    assert stray.target_class == "带电作业区"  # 无着落：原样保留交终审（不静默丢弃）
    assert {v["rule"] for v in stray.violations} >= {"target_class_out_of_catalog"}
    problems = validate_draft(stray, catalog)  # 自检独立复核：越类目打回
    assert any(p.startswith("target_class_out_of_catalog") for p in problems)


# ---------------------------------------------------------------- 草案 SHACL 语法自检


async def test_draft_shacl语法自检_合法过_非法Turtle打回_引擎失败打回() -> None:
    import services.kb.business.rule_extraction as rx

    catalog = load_seed_catalog()
    good = candidates_of_kind(kind="state_transition", target=f"{PW}OutageOrder", draft=DRAFT_VALID)
    assert validate_draft(good, catalog) == []  # 解析不抛错 + 空数据图可执行 + targetClass ∈ 种子类目

    bad_ttl = candidates_of_kind(kind="invariant", target=f"{PW}Feeder", draft=DRAFT_BAD_TTL)
    problems = validate_draft(bad_ttl, catalog)
    assert any(p.startswith("draft_shacl_unparseable") for p in problems)  # 语法不合法即打回

    out = candidates_of_kind(kind="invariant", target="pw:VoltageLevel", draft=DRAFT_TARGET_OUT)
    problems = validate_draft(out, catalog)
    assert sum(p.startswith("target_class_out_of_catalog") for p in problems) == 2  # 候选字段 + 草案 targetClass 双标记

    engine_down = candidates_of_kind(kind="invariant", target=f"{PW}Feeder", draft=DRAFT_VALID)
    original = rx.ontology_shacl.validate

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("模拟：pySHACL 引擎异常")

    rx.ontology_shacl.validate = _boom  # 引擎不可执行=草案不可被机器检验，必须打回
    try:
        problems = validate_draft(engine_down, catalog)
    finally:
        rx.ontology_shacl.validate = original
    assert any(p.startswith("draft_shacl_not_executable") for p in problems)


def candidates_of_kind(*, kind: str, target: str, draft: str) -> RuleCandidate:
    return RuleCandidate(
        rule_id="RD-001",
        kind=kind,  # type: ignore[arg-type]
        trigger="触发条件",
        consequence="约束后果",
        target_class=target,
        evidence=QUOTE_IN_CHUNK,
        draft_shacl=draft,
        confidence=0.8,
    )


# ---------------------------------------------------------------- 落库往返 + 评审单双写（aiosqlite）

TENANT = uuid.uuid4()


@pytest.fixture
async def kb_factory(tmp_path: Any) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from services.platform.db.base import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'kb-rule.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c,
                tables=[
                    TenantORM.__table__,
                    KbCollectionORM.__table__,
                    DocumentORM.__table__,
                    KbRuleCandidate.__table__,
                    ReviewTicketORM.__table__,
                ],
            )
        )
    # 注：document_chunks 表不建（其 FTS 表达式索引依赖 PG to_tsvector，SQLite 无此函数）；
    # kb_rule_candidates.chunk_id 的 FK 在 SQLite 不强制（pragma 默认关），None 路径即覆盖。
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        tenant = TenantORM(id=TENANT, name="kb-rule-租户", slug=f"kb-rule-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=TENANT, name="kb-rule-库", embedding_model="bge-m3")
        db.add(collection)
        await db.flush()
        doc = DocumentORM(
            tenant_id=TENANT,
            kb_collection_id=collection.id,
            title="检修规程",
            source_type="upload",
            size_bytes=len(CHUNK.encode()),
            minio_key=f"raw-docs/{TENANT}/{collection.id}/{uuid.uuid4()}/source.md",
            checksum_sha256="a" * 64,
            meta={},
            status="uploaded",
        )
        db.add(doc)
    factory.document_id = doc.id  # type: ignore[attr-defined]  # 夹具坐标透出（测试直取）
    factory.chunk_id = None  # type: ignore[attr-defined]  # 出处 chunk 悬置（SQLite FK 不强制）
    yield factory
    await engine.dispose()


@sqlite_needed
async def test_落库往返_评审单双写_重放幂等(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    candidates = await extract_rule_candidates(CHUNK, load_seed_catalog(), RuleStubModelPort(STUB_PAYLOAD))
    review = ReviewTicketService(kb_factory)
    ids = await persist_rule_candidates(
        kb_factory,
        candidates,
        kb_factory.document_id,  # type: ignore[attr-defined]
        kb_factory.chunk_id,  # type: ignore[attr-defined]
        "trace-rule-1",
        review=review,
    )
    assert len(ids) == 3 and len(set(ids)) == 3

    async with kb_factory() as db:
        rows = (await db.execute(select(KbRuleCandidate).order_by(KbRuleCandidate.rule_id))).scalars().all()
        ticket_count = (
            await db.execute(
                select(func.count()).select_from(ReviewTicketORM).where(ReviewTicketORM.tenant_id == TENANT)
            )
        ).scalar_one()
    assert [r.rule_id for r in rows] == ["RD-001", "RD-002", "RD-003"]
    head = rows[0]
    assert head.risk_flag is True  # 底线 3：行级恒真
    assert head.kind == "state_transition" and head.target_class == f"{PW}OutageOrder"
    assert head.trace_id == "trace-rule-1"
    assert head.meta["template_ref"] == RULE_EXTRACT_TEMPLATE_REF
    assert head.evidence["quote"].startswith("工单状态仅允许沿")  # 证据信封往返一致
    assert head.evidence["span"] == candidates[0].evidence_span
    assert head.draft_shacl == DRAFT_VALID
    assert float(head.confidence) == pytest.approx(0.92)
    # 评审单：candidate_type=rule_draft + risk_flag=true 随单透出（100% 人工终审）
    async with kb_factory() as db:
        ticket_rows = (
            (await db.execute(select(ReviewTicketORM).where(ReviewTicketORM.tenant_id == TENANT))).scalars().all()
        )
    assert ticket_count == 3
    assert {t.target_id for t in ticket_rows} == set(ids)
    for t in ticket_rows:
        assert t.target_type == "knowledge_instance"  # 约束枚举内唯一 kb 值（扩枚举随改表回填）
        assert t.status == "pending_review"
        assert t.payload["candidate_type"] == "rule_draft"
        assert t.payload["risk_flag"] is True
        assert t.payload["payload"]["rule"]["rule_id"] in {"RD-001", "RD-002", "RD-003"}
        assert t.payload["payload"]["quote"].startswith(("工单状态", "同一台设备", "设备必须先完成"))

    # 重放（断点续跑语义）：rule_key 幂等不重插 + uk_review_one_open 幂等不重开单
    replay_ids = await persist_rule_candidates(
        kb_factory,
        candidates,
        kb_factory.document_id,  # type: ignore[attr-defined]
        kb_factory.chunk_id,  # type: ignore[attr-defined]
        "trace-rule-2",
        review=review,
    )
    assert replay_ids == ids
    async with kb_factory() as db:
        assert (await db.execute(select(func.count()).select_from(KbRuleCandidate))).scalar_one() == 3
        assert (
            await db.execute(
                select(func.count()).select_from(ReviewTicketORM).where(ReviewTicketORM.tenant_id == TENANT)
            )
        ).scalar_one() == 3


@sqlite_needed
async def test_risk_flag_数据库级CHECK恒真(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    async with kb_factory() as db, db.begin():
        db.add(
            KbRuleCandidate(
                tenant_id=TENANT,
                document_id=kb_factory.document_id,  # type: ignore[attr-defined]
                chunk_id=None,
                rule_id="RD-XXX",
                rule_key="k" * 32,
                kind="invariant",
                trigger="t",
                consequence="c",
                target_class=f"{PW}Feeder",
                evidence={},
                draft_shacl=DRAFT_VALID,
                confidence=0.5,
                risk_flag=False,  # 应用层误写 → CHECK (risk_flag) 数据库级拒绝
                meta={},
            )
        )
        with pytest.raises(IntegrityError):
            await db.flush()


@sqlite_needed
async def test_文档不存在与评审端口未装配(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    from services.kb.business.pipeline_base import PipelineError

    candidates = await extract_rule_candidates(CHUNK, load_seed_catalog(), RuleStubModelPort(STUB_PAYLOAD))
    with pytest.raises(PipelineError, match="404"):  # 文档不存在
        await persist_rule_candidates(
            kb_factory, candidates, uuid.uuid4(), None, "trace-rule-1", review=ReviewTicketService(kb_factory)
        )
    with pytest.raises(PipelineError, match="409"):  # 审核端口未装配（候选非成品，无单不落库）
        await persist_rule_candidates(
            kb_factory,
            candidates,
            kb_factory.document_id,  # type: ignore[attr-defined]
            kb_factory.chunk_id,  # type: ignore[attr-defined]
            "trace-rule-1",
            review=None,
        )

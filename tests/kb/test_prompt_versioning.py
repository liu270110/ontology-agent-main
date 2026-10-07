"""提示词版本化治理用例（18 篇 §1 / standards/01 §5.1）：快照断言锁现网逐字节输出。

- 快照：模板正文/渲染输出对现网字面量逐字节锁定——重构或换版本时快照字面量不得随手改，
  断言失败即提示词行为漂移；变更模板必须发新版本（extract_v2→extract_v3）并更新快照走评审回归
  （18 篇 §1.3），禁止原地改写 active 版本；
- 注册表：PROMPTS 按 ref（"模板 id@version"）登记渲染函数，未知 ref 明确抛错（version pin 不回退）；
- 信封：kb_facts.meta.template_ref / 审核票据 payload.template_ref 与所用模板 ref 同源落库。
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from collections.abc import AsyncIterator

import pytest
from rdflib import Graph
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from services.iam.data.orm import Tenant as TenantORM
from services.kb.business.kb_extraction import (
    SeedCatalog,
    StepContext,
    _candidate_fact,
    _ChunkRef,
    _ticket_envelope,
    load_seed_catalog,
    run_extract,
)
from services.kb.business.kb_pipeline import run_pipeline
from services.kb.business.prompts import (
    PROMPTS,
    SYSTEM_PROMPTS,
    UnknownTemplateRefError,
    extract_v1,
    extract_v2,
    extract_v3,
    extract_v4,
    get_prompt,
    get_system_prompt,
)
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import DocumentChunk as DocumentChunkORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.orm import KbFact as KbFactORM
from services.kb.data.orm import KbPipelineStep as KbPipelineStepORM
from services.platform.config import Settings
from services.platform.db import registry as orm_registry  # noqa: F401  # 全模块 ORM 入 metadata（FK 解析）
from services.platform.llm.gateway import FakeModelPort
from services.review.business.candidates import ReviewTicketService
from services.review.data.orm import ReviewTicket as ReviewTicketORM

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ---------------------------------------------------------------- 现网输出快照（字面量，重构前后不得改动）

_PW = "http://ontology-agent.local/o/t1/power#"

_SNAPSHOT_CLASSES: tuple[tuple[str, str, str], ...] = (
    (f"{_PW}Feeder", "馈线", "Feeder"),
    (f"{_PW}OutageOrder", "抢修工单", "OutageOrder"),
)
_SNAPSHOT_PROPERTIES: tuple[tuple[str, str, str], ...] = ((f"{_PW}hasStatus", "工单状态", "hasStatus"),)
_SNAPSHOT_CHUNK = "馈线F001 由城东变电站供电。"

_EXPECTED_SYSTEM_PROMPT_V1 = """你是电力配电网领域的知识抽取引擎。从「抽取文本」中抽取实体/属性/关系/事件候选。
规则（违反即无效）：
1. 禁止凭空创造：只抽取文本明确提及的内容，每条候选必须能在原文中找到依据；
2. ontology_class 只能取自「本体引导清单」中的类 IRI；清单没有合适类时省略该字段；
3. predicate（如有）优先取清单中的属性本地名（如 hasStatus/orderNo）；
4. confidence ∈ [0,1]，反映该候选的确定性；
5. 证据（source_ref）由系统自动附加，禁止生成，候选之间不得互为证据；
6. 只输出 JSON 对象：{"candidates": [{"kind", "name", "ontology_class", "predicate",
   "object", "confidence", "detail", "properties"}]}，kind ∈ entity|relation|attribute|event，
   relation/attribute 必须附 predicate 与 object，properties 为「属性本地名 → 字符串值」；
7. 文本没有任何可抽取内容时返回 {"candidates": []}。"""

_EXPECTED_SYSTEM_PROMPT_V2 = """你是电力配电网领域的知识抽取引擎。从「抽取文本」中抽取实体/属性/关系/事件候选。
规则（违反即无效）：
1. 禁止凭空创造：只抽取文本明确提及的内容，每条候选必须能在原文中找到依据；
2. ontology_class 只能取自「本体引导清单」中的类 IRI；清单没有合适类时省略该字段；
3. predicate（如有）优先取清单中的属性本地名（如 hasStatus/orderNo）；
4. confidence ∈ [0,1]，反映该候选的确定性；
5. evidence 必须是「抽取文本」中的原文逐字片段（禁止改写、概括、拼接，每条候选附一条）；
   出处四元组（source_ref）由系统自动附加，禁止生成，候选之间不得互为证据；
6. 只输出 JSON 对象：{"candidates": [{"kind", "name", "ontology_class", "predicate",
   "object", "evidence", "confidence", "detail", "properties"}]}，kind ∈ entity|relation|attribute|event，
   relation/attribute 必须附 predicate 与 object，properties 为「属性本地名 → 字符串值」；
7. 文本没有任何可抽取内容时返回 {"candidates": []}。"""

_EXPECTED_CATALOG_TEXT = (
    f"- 类 {_PW}Feeder（标签：馈线）\n- 类 {_PW}OutageOrder（标签：抢修工单）\n- 属性 hasStatus（标签：工单状态）"
)

_EXPECTED_USER_PROMPT = (
    "## 本体引导清单\n"
    f"- 类 {_PW}Feeder（标签：馈线）\n"
    f"- 类 {_PW}OutageOrder（标签：抢修工单）\n"
    "- 属性 hasStatus（标签：工单状态）"
    f"\n\n## 抽取文本\n{_SNAPSHOT_CHUNK}"
)

# ---- v3 快照字面量（K24 批，docs/Agent/13 §30：编号目录制 + 序号口径；v2 段按现状保留）

_EXPECTED_SYSTEM_PROMPT_V3 = """你是电力配电网领域的知识抽取引擎。从「抽取文本」中抽取实体/属性/关系/事件候选。
规则（违反即无效）：
1. 禁止凭空创造：只抽取文本明确提及的内容，每条候选必须能在原文中找到依据；
2. ontology_class 与 object_class 只能填「本体引导清单」类条目的序号（整数，如 2）——清单
   未展示类 IRI，禁止自行构造；清单没有合适类时省略该字段；
3. predicate（如有）优先取清单中的属性本地名（如 hasStatus/orderNo）；
4. confidence ∈ [0,1]，反映该候选的确定性；
5. evidence 必须是「抽取文本」中的原文逐字片段（禁止改写、概括、拼接，每条候选附一条）；
   出处四元组（source_ref）由系统自动附加，禁止生成，候选之间不得互为证据；
6. 只输出 JSON 对象：{"candidates": [{"kind", "name", "ontology_class", "predicate",
   "object", "evidence", "confidence", "detail", "properties"}]}，kind ∈ entity|relation|attribute|event，
   relation/attribute 必须附 predicate 与 object，properties 为「属性本地名 → 字符串值」；
7. 文本没有任何可抽取内容时返回 {"candidates": []}。"""

_EXPECTED_CATALOG_TEXT_V3 = (
    "- 1. 类 馈线（Feeder）\n- 2. 类 抢修工单（OutageOrder）\n- 属性 hasStatus（标签：工单状态）"
)

_EXPECTED_USER_PROMPT_V3 = (
    "## 本体引导清单\n"
    "- 1. 类 馈线（Feeder）\n"
    "- 2. 类 抢修工单（OutageOrder）\n"
    "- 属性 hasStatus（标签：工单状态）"
    f"\n\n## 抽取文本\n{_SNAPSHOT_CHUNK}"
)

# v4 标题栏结构化线索区快照（extract_lab A2 实测正文，ce543e8 实验版 v3 重登记 v4；
# 示例字段值用 SAMPLE-* 占位，零真实图号）。目录前缀=编号制（v4 沿用 v3 目录口径）。
_EXPECTED_HINT_V4 = (
    "## 标题栏结构化线索（确定性投影产物，可能含错配——仅作位置线索，取值须以「抽取文本」原文为准）\n"
    "图号: SAMPLE-0001\n"
    "\n"
    "## 候选字段清单（标题栏字段；输出 attribute 候选时 predicate 取清单中的**字段中文名**（括号外部分），"
    '每个可配对字段一条：{"kind": "attribute", "name": "<字段值所属实体>", '
    '"predicate": "<字段名>", "object": "<字段值>"}）\n'
    "- 图号(图样代号/Drawing NO/DWG NO)\n"
    "- 名称(图名/Title)\n"
    "- 材料(Material)\n"
    "- 表面处理(Finish/Surface Treatment)\n"
    "- 热处理(Heat Treat)\n"
    "- 数量(Qty/Pcs)\n"
    "- 比例(Scale)\n"
    "- 重量(Weight)\n"
    "- 幅面(Size/Sheet Size)\n"
    "- 版本(Rev/Version)\n"
    "- 日期(Date)\n"
    "- 设计(Design/Drawn)\n"
    "- 审核(Check/Checked)\n"
    "- 批准(Approval/Approved)\n"
    "- 共几张(Sheet,如 1 OF 4 形态)\n"
    "- 项目名(Project Name)\n"
    "\n"
    "## 输出 JSON schema（强约束，违反即无效）\n" + extract_v4.OUTPUT_SCHEMA_JSON + "\n"
)
_SNAPSHOT_TITLEBLOCK_FIELDS = {"图号": "SAMPLE-0001"}
_EXPECTED_USER_PROMPT_V4_HINT = (
    "## 本体引导清单\n"
    "- 1. 类 馈线（Feeder）\n"
    "- 2. 类 抢修工单（OutageOrder）\n"
    "- 属性 hasStatus（标签：工单状态）"
    f"\n\n{_EXPECTED_HINT_V4}## 抽取文本\n{_SNAPSHOT_CHUNK}"
)


def test_snapshot_template_ref_版本钉死() -> None:
    """version pin（18 篇 §1.1）：v1/v2/v3 冻结为历史（v2/v3 留注册表可回退），active ref = kb_extract@v4。"""
    assert extract_v1.TEMPLATE_REF == "kb_extract@v1"  # 历史版本不可变
    assert extract_v2.TEMPLATE_REF == "kb_extract@v2"  # 历史（可回退）
    assert extract_v3.TEMPLATE_REF == "kb_extract@v3"  # 历史（K24 §30，可回退）
    assert extract_v4.TEMPLATE_REF == "kb_extract@v4"  # 现役（v3 编号制 + 标题栏线索区复合）


def test_snapshot_system_prompt_v1_历史冻结_byte_exact() -> None:
    """v1 历史正文不可变（治理约束：active 版本一经发布即冻结）。"""
    assert extract_v1.SYSTEM_PROMPT == _EXPECTED_SYSTEM_PROMPT_V1


def test_snapshot_system_prompt_v2_byte_exact() -> None:
    """v2 系统提示词正文与深化批次字面量逐字节一致（新增 evidence 逐字引语要求）。"""
    assert extract_v2.SYSTEM_PROMPT == _EXPECTED_SYSTEM_PROMPT_V2


def test_snapshot_system_prompt_v4_与v3一致_byte_exact() -> None:
    """v4 系统提示词与 v3 逐字节一致（v4 只动用户提示词组装面——标题栏结构化线索区；
    序号口径规则 2 随 v3 原样生效）。"""
    assert extract_v4.SYSTEM_PROMPT == _EXPECTED_SYSTEM_PROMPT_V3
    assert extract_v4.SYSTEM_PROMPT == extract_v3.SYSTEM_PROMPT


def test_snapshot_catalog_text_byte_exact() -> None:
    """本体引导清单渲染与现网拼接逐字节一致（声明序；属性取本地名）。"""
    catalog = SeedCatalog(
        _SNAPSHOT_CLASSES,
        frozenset(iri for iri, _, _ in _SNAPSHOT_CLASSES),
        _SNAPSHOT_PROPERTIES,
        Graph(),
    )
    assert extract_v1.render_catalog(catalog) == _EXPECTED_CATALOG_TEXT
    assert extract_v2.render_catalog(catalog) == _EXPECTED_CATALOG_TEXT


def test_snapshot_user_prompt_byte_exact() -> None:
    """用户提示词组装与现网 f-string 逐字节一致（清单区 + 空行 + 抽取文本区）。"""
    assert extract_v1.render(_EXPECTED_CATALOG_TEXT, _SNAPSHOT_CHUNK) == _EXPECTED_USER_PROMPT
    assert extract_v2.render(_EXPECTED_CATALOG_TEXT, _SNAPSHOT_CHUNK) == _EXPECTED_USER_PROMPT


def test_snapshot_v4_user_prompt_none形态与v3逐字节一致() -> None:
    """v4 双参形态（titleblock_fields=None，非图纸文档路径）= v3 输出逐字节一致（零行为变化）。"""
    assert extract_v4.render(_EXPECTED_CATALOG_TEXT_V3, _SNAPSHOT_CHUNK) == _EXPECTED_USER_PROMPT_V3


def test_snapshot_user_prompt_v4_hint_byte_exact() -> None:
    """v4 标题栏结构化线索区逐字节快照（extract_lab A2 实测正文 + v3 编号目录前缀；
    线索区恒在「抽取文本」标记前）。"""
    rendered = extract_v4.render(_EXPECTED_CATALOG_TEXT_V3, _SNAPSHOT_CHUNK, _SNAPSHOT_TITLEBLOCK_FIELDS)
    assert rendered == _EXPECTED_USER_PROMPT_V4_HINT
    # FakeModelPort 契约：正文段以「## 抽取文本」标记切分，标记必须仍是唯一末段入口
    assert rendered.count("## 抽取文本\n") == 1
    assert rendered.index("## 标题栏结构化线索") < rendered.index("## 抽取文本\n")


def test_snapshot_v4_schema_literal_与抽取schema一致() -> None:
    """v4 提示词内嵌 schema 字面量与 kb_extraction._EXTRACT_SCHEMA_V3 逐字节一致
    （跨模块一致性锁：v4 沿用 K24 integer 序号 schema 口径）。"""
    import json

    from services.kb.business.kb_extraction import _EXTRACT_SCHEMA_V3

    assert extract_v4.OUTPUT_SCHEMA_JSON == json.dumps(_EXTRACT_SCHEMA_V3, ensure_ascii=False)


def test_snapshot_system_prompt_v3_byte_exact() -> None:
    """v3 系统提示词正文与 K24 批字面量逐字节一致（规则 2 序号口径=编号制硬幻觉门禁提示词面）。"""
    assert extract_v3.SYSTEM_PROMPT == _EXPECTED_SYSTEM_PROMPT_V3


def test_snapshot_catalog_text_v3_byte_exact() -> None:
    """v3 编号目录渲染逐字节一致：类行=「序号. 标签（本地名）」不带 IRI（序号=声明序 1 起）、
    属性行同 v2 口径。"""
    catalog = SeedCatalog(
        _SNAPSHOT_CLASSES,
        frozenset(iri for iri, _, _ in _SNAPSHOT_CLASSES),
        _SNAPSHOT_PROPERTIES,
        Graph(),
    )
    assert extract_v3.render_catalog(catalog) == _EXPECTED_CATALOG_TEXT_V3


def test_snapshot_user_prompt_v3_byte_exact() -> None:
    """v3 用户提示词组装逐字节一致（两段式结构同 v2——FakeModelPort 依「## 抽取文本」切分兼容）。"""
    assert extract_v3.render(_EXPECTED_CATALOG_TEXT_V3, _SNAPSHOT_CHUNK) == _EXPECTED_USER_PROMPT_V3


# ---------------------------------------------------------------- 注册表治理（version pin）


def test_registry_binds_active_template() -> None:
    """注册表按 ref 登记渲染函数与系统提示词正文（两表同键集；取用函数同源）。"""
    assert PROMPTS[extract_v1.TEMPLATE_REF] is extract_v1.render
    assert PROMPTS[extract_v2.TEMPLATE_REF] is extract_v2.render
    assert PROMPTS[extract_v3.TEMPLATE_REF] is extract_v3.render
    assert PROMPTS[extract_v4.TEMPLATE_REF] is extract_v4.render
    assert SYSTEM_PROMPTS[extract_v2.TEMPLATE_REF] is extract_v2.SYSTEM_PROMPT
    assert SYSTEM_PROMPTS[extract_v3.TEMPLATE_REF] is extract_v3.SYSTEM_PROMPT
    assert SYSTEM_PROMPTS[extract_v4.TEMPLATE_REF] is extract_v4.SYSTEM_PROMPT
    assert get_prompt(extract_v3.TEMPLATE_REF) is extract_v3.render
    assert get_system_prompt(extract_v3.TEMPLATE_REF) is extract_v3.SYSTEM_PROMPT
    assert get_prompt(extract_v4.TEMPLATE_REF) is extract_v4.render
    assert get_system_prompt(extract_v4.TEMPLATE_REF) is extract_v4.SYSTEM_PROMPT
    assert set(PROMPTS) == set(SYSTEM_PROMPTS)


def test_unknown_template_ref_raises_explicitly() -> None:
    """未知 ref 明确抛 UnknownTemplateRefError（不静默回退其他版本，18 篇 §1.1）。"""
    with pytest.raises(UnknownTemplateRefError, match="extract@v999"):
        get_prompt("extract@v999")
    with pytest.raises(UnknownTemplateRefError, match="extract@v999"):
        get_system_prompt("extract@v999")


# ---------------------------------------------------------------- 信封 template_ref 同源（纯函数级）


def test_candidate_envelopes_carry_active_template_ref() -> None:
    """kb_facts.meta 与审核票据信封的 template_ref 与本次所用模板 ref 同源（注册表在册版本）。"""
    ctx = StepContext(
        session_factory=None,
        tenant_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        embedder=None,
        model=None,
        review=None,
    )
    chunk = _ChunkRef(id=uuid.uuid4(), seq=0, content=_SNAPSHOT_CHUNK, meta={"span": [0, 12]})
    cand = {"kind": "entity", "name": "馈线F001", "ontology_class": f"{_PW}Feeder", "confidence": 0.9}
    fact = _candidate_fact(ctx, chunk, cand, "kb-extract:trace", extract_v3.TEMPLATE_REF)
    assert fact["meta"]["template_ref"] == "kb_extract@v3"
    assert fact["meta"]["template_ref"] in PROMPTS  # 落库 ref 必须是注册表在册版本
    ticket = _ticket_envelope(fact, "kb-extract:trace", extract_v2.TEMPLATE_REF)
    assert ticket["template_ref"] == "kb_extract@v2"
    fact_v3 = _candidate_fact(ctx, chunk, cand, "kb-extract:trace", extract_v3.TEMPLATE_REF)  # 历史版同源
    assert fact_v3["meta"]["template_ref"] == "kb_extract@v3"
    ticket_v3 = _ticket_envelope(fact_v3, "kb-extract:trace", extract_v3.TEMPLATE_REF)
    assert ticket_v3["template_ref"] == "kb_extract@v3"
    fact_v4 = _candidate_fact(ctx, chunk, cand, "kb-extract:trace", extract_v4.TEMPLATE_REF)  # 现役版同源
    assert fact_v4["meta"]["template_ref"] == "kb_extract@v4"
    ticket_v4 = _ticket_envelope(fact_v4, "kb-extract:trace", extract_v4.TEMPLATE_REF)
    assert ticket_v4["template_ref"] == "kb_extract@v4"


# ---------------------------------------------------------------- 集成：template_ref 落库（PG 夹具风格同存量用例）

CONTENT = "馈线F001 由城东变电站供电。\n"


def _seed_class_index(iri: str) -> int:
    """K24 序号口径：种子类在声明序（=v3 目录渲染序=映射序）中的 1 起序号（FakeModelPort 序号桩用）。"""
    return next(i for i, (c_iri, _, _) in enumerate(load_seed_catalog().classes, start=1) if c_iri == iri)


async def _instant_backoff(attempt: int) -> None:
    return None  # 测试不等待真实退避（30s 起）


@pytest.fixture
async def kb_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect():
            pass
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达，跳过提示词版本化集成用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def extract_env(kb_pg: async_sessionmaker[AsyncSession]) -> AsyncIterator[dict]:
    """独立租户/集合/文档 + 确定性模型与审核服务；结束按 FK 逆序清理。"""
    async with kb_pg() as db, db.begin():
        tenant = TenantORM(name="kb-pv-租户", slug=f"kb-pv-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        collection = KbCollectionORM(tenant_id=tenant.id, name="kb-pv-库", embedding_model="bge-m3")
        db.add(collection)
        await db.flush()
        doc = DocumentORM(
            tenant_id=tenant.id,
            kb_collection_id=collection.id,
            title="提示词版本化联调",
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
        "model": FakeModelPort(keyword_classes={"馈线F001": _seed_class_index(f"{_PW}Feeder")}),
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


async def test_extract_persists_template_ref_into_envelopes(
    kb_pg: async_sessionmaker[AsyncSession], extract_env: dict
) -> None:
    """落库断言：kb_facts.meta.template_ref 与 review_tickets.payload.template_ref = active ref。"""
    ctx = StepContext(
        session_factory=kb_pg,
        tenant_id=extract_env["tenant_id"],
        document_id=extract_env["document_id"],
        embedder=None,
        model=extract_env["model"],
        review=extract_env["review"],
    )
    await run_extract(ctx)
    async with kb_pg() as db:
        facts = (
            (await db.execute(select(KbFactORM).where(KbFactORM.document_id == extract_env["document_id"])))
            .scalars()
            .all()
        )
        tickets = (
            (await db.execute(select(ReviewTicketORM).where(ReviewTicketORM.tenant_id == extract_env["tenant_id"])))
            .scalars()
            .all()
        )
    assert facts and tickets  # FakeModelPort 确定性产出 ≥1 候选
    assert all(fact.meta["template_ref"] == extract_v4.TEMPLATE_REF for fact in facts)
    assert all(ticket.payload["template_ref"] == extract_v4.TEMPLATE_REF for ticket in tickets)

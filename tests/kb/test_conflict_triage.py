# tests/kb/test_conflict_triage.py
"""KB-G1a 冲突分诊四型用例（OntRAG §8.1 落库版；纯 aiosqlite 内存库，零 PG 真连）。

背景：共享 PG 池被并行会话耗尽——本文件全部 aiosqlite + @compiles shim（tests/kb/
test_review_queue.py / test_conflict_governance.py 同款先例：JSONB/UUID 降编译到 SQLite
DDL），事务内 create_all 只建本切片三表（kb_facts + kb_fact_relations + documents）。

覆盖：分诊固定顺序 T1→T4→T3→T2——T1 文档链/结构化源行事件链（自动承接封口建边，无工单）、
T4 主谓宾全同合并佐证、T3 span 硬门禁正反 + source_system 豁免通道、T2 建冲突工单
（target_type=conflict，payload 并排两事实+出处+四项评分参考）、分诊顺序（T1 命中不进 T4）、
对象属性多值合法非矛盾、无端口 T2 响亮失败；承接判定 a（封口+split 组级边）/b（词法命中
加急工单与未命中转 needs_review）/c（明确否定走分诊→同链 T1 承接）；裁决执行 winner 封口
败者连 outranked_by 边、t3_coexist 人工填 scope（含封闭枚举校验）、pending 不动事实。

psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略（仓库同款）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from services.iam.data.orm import Tenant, User  # noqa: F401  FK 目标表入 metadata（sqlite 只解析不建目标表）
from services.kb.business.conflict_triage import (
    CARRYOVER_BRANCH_A,
    CARRYOVER_BRANCH_B_HIT,
    CARRYOVER_BRANCH_B_MISS,
    CARRYOVER_BRANCH_C,
    DECISION_OPTIONS,
    RELATION_OUTRANKED_BY,
    RELATION_SUPERSEDED_BY,
    STATE_COEXIST,
    STATE_MERGED_INTO,
    STATE_NEEDS_REVIEW,
    STATE_OUTRANKED,
    STATE_SUPERSEDED,
    TARGET_TYPE_CONFLICT,
    TRIAGE_NONE,
    TRIAGE_T1,
    TRIAGE_T2,
    TRIAGE_T3,
    TRIAGE_T4,
    ConflictTriageError,
    apply_decision,
    carryover_facts,
    triage_conflicts,
)
from services.kb.data.governance_orm import KbFactRelation
from services.kb.data.orm import (
    Document,
    KbCollection,  # noqa: F401  documents FK 目标表入 metadata（sqlite 只解析不建）
    KbFact,
)
from services.platform.db.base import Base

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    import aiosqlite  # noqa: F401

    _HAS_AIOSQLITE = True
except ImportError:  # 本地依赖缺失：仅落库用例跳过（纯参数校验用例不受影响）
    _HAS_AIOSQLITE = False

sqlite_needed = pytest.mark.skipif(not _HAS_AIOSQLITE, reason="aiosqlite 未安装：冲突分诊落库用例")

# ── SQLite 方言 shim（test_connector.py 同款：PG 专列类型建表降编译；幂等重注册无害）──


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSONB"


@compiles(PgUuid, "sqlite")
def _sqlite_uuid(type_: Any, compiler: Any, **kw: Any) -> str:
    return "CHAR(32)"


TENANT = uuid.uuid4()
REVIEWER = uuid.uuid4()
_T9 = lambda h, m: datetime(2026, 9, 1, h, m)  # noqa: E731 — 种子时间戳（created_at 定序用）


class _FakeTickets:
    """CandidateReviewPort.submit_candidate 鸭子类型桩（记录建单调用，返回确定性 id）。"""

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    async def submit_candidate(
        self,
        *,
        tenant_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
        payload: dict[str, Any],
        status: str = "pending_review",
        submitter_id: uuid.UUID | None = None,
        sla_deadline: datetime | None = None,
    ) -> uuid.UUID:
        ticket_id = uuid.uuid4()
        self.submitted.append(
            {"ticket_id": ticket_id, "target_type": target_type, "target_id": target_id, "payload": payload}
        )
        return ticket_id


def _doc(doc_id: uuid.UUID, *, title: str, meta: dict[str, Any] | None = None) -> Document:
    """documents 行值（版本链经 meta.supersedes_id——database/01 独立列回填后切列）。"""
    return Document(
        id=doc_id,
        tenant_id=TENANT,
        kb_collection_id=uuid.uuid4(),
        title=title,
        minio_key=f"raw-docs/{doc_id}",
        checksum_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        meta=meta or {},
    )


def _fact(
    *,
    document_id: uuid.UUID,
    subject: str,
    predicate: str | None = "hasLimit",
    obj: str | None = None,  # noqa: A002  与 kb_facts 列名同形
    status: str = "authoritative",
    confidence: float = 0.8,
    fact_type: str = "attribute",
    evidence: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    aliases: list[str] | None = None,
    created_at: datetime | None = None,
) -> KbFact:
    """kb_facts 行值（缺省 evidence 只带 source_ref 骨架；scope 走 meta.scope）。"""
    row = KbFact(
        id=uuid.uuid4(),
        tenant_id=TENANT,
        document_id=document_id,
        fact_type=fact_type,
        subject=subject,
        predicate=predicate,
        object=obj,
        confidence=confidence,
        status=status,
        evidence=(
            evidence if evidence is not None else {"source_ref": {"document_id": str(document_id), "doc_version": 1}}
        ),
        violations=[],
        meta=meta or {},
        aliases=aliases or [],
    )
    if created_at is not None:
        row.created_at = created_at
    return row


async def _seed(factory: async_sessionmaker[AsyncSession], docs: list[Document], facts: list[KbFact]) -> None:
    """事务内批量播种（文档先行——版本链上溯与业务生效时间读取依赖）。"""
    async with factory() as db, db.begin():
        for doc in docs:
            db.add(doc)
        for fact in facts:
            db.add(fact)


async def _run(factory: async_sessionmaker[AsyncSession], fn: Callable[[AsyncSession], Any]) -> Any:
    """调用方持有事务口径执行被测函数（session.begin() 出口提交；服务只 flush 不 commit）。"""
    async with factory() as db, db.begin():
        return await fn(db)


async def _fact_meta(factory: async_sessionmaker[AsyncSession], fact_id: uuid.UUID) -> dict[str, Any]:
    async with factory() as db:
        row = await db.get(KbFact, fact_id)
        assert row is not None
        return dict(row.meta or {})


async def _edges(factory: async_sessionmaker[AsyncSession]) -> list[KbFactRelation]:
    async with factory() as db:
        return list((await db.execute(select(KbFactRelation))).scalars().all())


def _ct(meta: dict[str, Any]) -> dict[str, Any]:
    """meta.conflict_triage（无标注返回空 dict）。"""
    ct = meta.get("conflict_triage")
    return dict(ct) if isinstance(ct, dict) else {}


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """aiosqlite 内存库（StaticPool 单连接共享）+ 事务内建表（本切片三表）。"""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c, tables=[KbFact.__table__, KbFactRelation.__table__, Document.__table__]
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


# ---------------------------------------------------------------- 分诊四型（§8.1 固定顺序）


@sqlite_needed
async def test_T1_版本演进_文档链自动承接无工单(factory: async_sessionmaker[AsyncSession]) -> None:
    """同文档版本链同主谓异值 → T1：旧封口（valid_to=继任业务生效时间）+ superseded_by 边，无工单。"""
    # Arrange：旧版权威事实 100Ah；新版候选 120Ah（meta.valid_from=继任业务生效时间，§8.2 封口规则）
    old_doc, new_doc = uuid.uuid4(), uuid.uuid4()
    old_fact = _fact(document_id=old_doc, subject="馈线F001", obj="100Ah", created_at=_T9(8, 0))
    cand = _fact(
        document_id=new_doc,
        subject="馈线F001",
        obj="120Ah",
        status="candidate",
        created_at=_T9(9, 0),
        meta={"valid_from": "2024-03-01T00:00:00"},
    )
    await _seed(
        factory,
        [
            _doc(old_doc, title="GB/T 31486-2015"),
            _doc(new_doc, title="GB/T 31486-2024", meta={"supersedes_id": str(old_doc)}),
        ],
        [old_fact, cand],
    )
    tickets = _FakeTickets()
    # Act
    report = await _run(factory, lambda db: triage_conflicts(db, TENANT, fact_pairs=[{"id": cand.id}], tickets=tickets))
    # Assert：T1 自动承接，零工单（不是冲突，§8.1）
    (outcome,) = report.outcomes
    assert outcome.triage == TRIAGE_T1 and outcome.matched_fact_id == old_fact.id and outcome.ticket_id is None
    assert tickets.submitted == [] and report.tickets_opened == 0
    meta = await _fact_meta(factory, old_fact.id)
    assert meta["conflict_triage"]["state"] == STATE_SUPERSEDED
    assert meta["conflict_triage"]["valid_to"] == "2024-03-01T00:00:00"
    assert meta["conflict_triage"]["effective_date_known"] is True
    assert meta["conflict_triage"]["successor_fact_id"] == str(cand.id)
    edges = await _edges(factory)
    assert [(e.from_fact_id, e.to_fact_id, e.relation) for e in edges] == [
        (old_fact.id, cand.id, RELATION_SUPERSEDED_BY)
    ]
    assert edges[0].evidence["chain"] == "document_chain" and edges[0].evidence["valid_to"] == "2024-03-01T00:00:00"
    async with factory() as db:  # 封口=遮蔽不删除：status 不翻（硬门禁，机器不替人终审）
        rows = {r.id: r for r in (await db.execute(select(KbFact))).scalars()}
        assert rows[old_fact.id].status == "authoritative" and rows[cand.id].status == "candidate"


@sqlite_needed
async def test_T1_结构化源行事件链与单调性(factory: async_sessionmaker[AsyncSession]) -> None:
    """同 (source_system, external_id) 且 occurred_at 单调递增 → T1；递增不成立落 T2 工单。"""
    # Arrange：ERP 行事件链两行（occurred_at 递增）；另备一组递增不成立的同链对（旧行更新）
    doc_a, doc_b, doc_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    def _line(doc_id: uuid.UUID, obj: str, occ: str, *, status: str = "authoritative", hh: int, mm: int) -> KbFact:
        return _fact(
            document_id=doc_id,
            subject="设备DEV-001",
            predicate="localStatus",
            obj=obj,
            status=status,
            created_at=_T9(hh, mm),
            evidence={"source_ref": {"source_system": "erp", "external_id": "DEV-001", "occurred_at": occ}},
        )

    old_line = _line(doc_a, "running", "2026-01-01T00:00:00", hh=8, mm=0)
    newer = _line(doc_b, "maintenance", "2026-02-01T00:00:00", status="candidate", hh=9, mm=0)
    older = _line(doc_c, "idle", "2025-12-01T00:00:00", status="candidate", hh=9, mm=30)
    await _seed(
        factory,
        [_doc(doc_a, title="ERP-01"), _doc(doc_b, title="ERP-02"), _doc(doc_c, title="ERP-03")],
        [old_line, newer, older],
    )
    tickets = _FakeTickets()
    # Act（先裁逆行候选再裁递增候选：T2 建单不封口，后者仍可命中 T1）
    report = await _run(
        factory,
        lambda db: triage_conflicts(db, TENANT, fact_pairs=[{"id": older.id}, {"id": newer.id}], tickets=tickets),
    )
    # Assert：递增行命中 T1（结构化源=版本链同视，封口=新行 occurred_at）；逆行不判 T1 → T2 工单
    by_cand = {o.candidate_fact_id: o for o in report.outcomes}
    assert by_cand[newer.id].triage == TRIAGE_T1
    assert by_cand[older.id].triage == TRIAGE_T2 and by_cand[older.id].ticket_id is not None
    edges = await _edges(factory)
    assert [(e.from_fact_id, e.relation) for e in edges] == [(old_line.id, RELATION_SUPERSEDED_BY)]
    assert edges[0].evidence["chain"] == "structured_source" and edges[0].evidence["valid_to"] == "2026-02-01T00:00:00"
    # 旧行被 T1 封口（T2 建单不动事实）；older 候选侧无标注
    assert _ct(await _fact_meta(factory, old_line.id))["state"] == STATE_SUPERSEDED
    assert _ct(await _fact_meta(factory, older.id)) == {}
    assert [t["target_type"] for t in tickets.submitted] == [TARGET_TYPE_CONFLICT]


@sqlite_needed
async def test_T4_多源重复合并佐证(factory: async_sessionmaker[AsyncSession]) -> None:
    """主谓宾全同不同源 → T4：既有事实挂多 source_ref、置信度上调；候选标 merged_into 不翻状态；无工单。"""
    # Arrange：文档 A/B 无版本关系，双方确认同一参数
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    existing = _fact(
        document_id=doc_a,
        subject="馈线F001",
        obj="50万",
        confidence=0.6,
        created_at=_T9(8, 0),
        evidence={
            "source_ref": {"document_id": str(doc_a), "doc_version": 1, "span": [10, 14]},
            "quote": "审批上限50万",
            "span": [2, 8],
        },
    )
    cand = _fact(
        document_id=doc_b,
        subject="馈线F001",
        obj="50万",
        status="candidate",
        confidence=0.9,
        created_at=_T9(9, 0),
        evidence={"source_ref": {"document_id": str(doc_b), "doc_version": 1, "span": [30, 34]}},
    )
    await _seed(factory, [_doc(doc_a, title="制度A"), _doc(doc_b, title="制度B")], [existing, cand])
    tickets = _FakeTickets()
    # Act
    report = await _run(factory, lambda db: triage_conflicts(db, TENANT, fact_pairs=[{"id": cand.id}], tickets=tickets))
    # Assert：佐证合并（来源列表保留 + 置信度上调 min(1, max(0.6,0.9)+0.05)=0.95）
    (outcome,) = report.outcomes
    assert outcome.triage == TRIAGE_T4 and outcome.ticket_id is None
    assert tickets.submitted == [] and (await _edges(factory)) == []
    async with factory() as db:
        rows = {r.id: r for r in (await db.execute(select(KbFact))).scalars()}
        merged = rows[existing.id].evidence["merged_source_refs"]
        assert merged == [{"document_id": str(doc_b), "doc_version": 1, "span": [30, 34]}]
        assert float(rows[existing.id].confidence) == pytest.approx(0.95)
        assert rows[existing.id].status == "authoritative"
        assert rows[cand.id].status == "candidate"  # 硬门禁：机器不替人终审
    assert _ct(await _fact_meta(factory, existing.id))["state"] == "corroborated"
    assert _ct(await _fact_meta(factory, cand.id))["state"] == STATE_MERGED_INTO
    assert _ct(await _fact_meta(factory, cand.id))["target_fact_id"] == str(existing.id)


@sqlite_needed
async def test_T3_限定差异_span落地双保留(factory: async_sessionmaker[AsyncSession]) -> None:
    """scope 键不相交且值有源文 span 落地 → T3：双保留各标 scope，无工单。"""
    # Arrange：京沪地标（region）vs 国标（regulatory_domain），限定语均在原文 span 出现
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    existing = _fact(
        document_id=doc_a,
        subject="台区T09",
        obj="120kVA",
        created_at=_T9(8, 0),
        meta={"scope": {"region": {"value": "110000", "span": [12, 18]}}},
    )
    cand = _fact(
        document_id=doc_b,
        subject="台区T09",
        obj="100kVA",
        status="candidate",
        created_at=_T9(9, 0),
        meta={"scope": {"regulatory_domain": {"value": "http://oa.local/gb", "span": [4, 9]}}},
    )
    await _seed(factory, [_doc(doc_a, title="北京地标"), _doc(doc_b, title="国标")], [existing, cand])
    tickets = _FakeTickets()
    # Act
    report = await _run(factory, lambda db: triage_conflicts(db, TENANT, fact_pairs=[{"id": cand.id}], tickets=tickets))
    # Assert
    (outcome,) = report.outcomes
    assert outcome.triage == TRIAGE_T3 and outcome.ticket_id is None
    assert tickets.submitted == [] and (await _edges(factory)) == []
    assert _ct(await _fact_meta(factory, existing.id)) == {"state": STATE_COEXIST, "with_fact_id": str(cand.id)}
    assert _ct(await _fact_meta(factory, cand.id)) == {"state": STATE_COEXIST, "with_fact_id": str(existing.id)}


@sqlite_needed
async def test_T3_span未落地_硬门禁落T2工单(factory: async_sessionmaker[AsyncSession]) -> None:
    """scope 平值无 span 落地（LLM 推断嫌疑）→ 不得自动 T3，落 T2 冲突工单（§8.1 硬门禁）。"""
    # Arrange：键不相交但值无源文回指
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    existing = _fact(
        document_id=doc_a,
        subject="台区T09",
        obj="120kVA",
        created_at=_T9(8, 0),
        meta={"scope": {"region": "110000"}},
    )
    cand = _fact(
        document_id=doc_b,
        subject="台区T09",
        obj="100kVA",
        status="candidate",
        created_at=_T9(9, 0),
        meta={"scope": {"regulatory_domain": "http://oa.local/gb"}},
    )
    await _seed(factory, [_doc(doc_a, title="北京地标"), _doc(doc_b, title="国标")], [existing, cand])
    tickets = _FakeTickets()
    # Act
    report = await _run(factory, lambda db: triage_conflicts(db, TENANT, fact_pairs=[{"id": cand.id}], tickets=tickets))
    # Assert：不自动共存（防 T2 被误判 T3 长期无人知晓）
    (outcome,) = report.outcomes
    assert outcome.triage == TRIAGE_T2 and outcome.ticket_id == tickets.submitted[0]["ticket_id"]
    assert _ct(await _fact_meta(factory, existing.id)) == {} and _ct(await _fact_meta(factory, cand.id)) == {}
    assert (await _edges(factory)) == []


@sqlite_needed
async def test_T3_source_system豁免通道(factory: async_sessionmaker[AsyncSession]) -> None:
    """白名单谓词跨系统同主谓异值 → T3 自动共存；非白名单谓词同景落 T2（谓词级声明制）。"""
    # Arrange：localStatus（白名单）两系统异值；hasLimit（未声明 systemRelative）同景
    doc_a, doc_b, doc_c, doc_d = (uuid.uuid4() for _ in range(4))
    facts = [
        _fact(
            document_id=doc_a,
            subject="设备D1",
            predicate="localStatus",
            obj="running",
            created_at=_T9(8, 0),
            meta={"scope": {"source_system": "http://sys/erp"}},
        ),
        _fact(
            document_id=doc_b,
            subject="设备D1",
            predicate="localStatus",
            obj="idle",
            status="candidate",
            created_at=_T9(9, 0),
            meta={"scope": {"source_system": "http://sys/crm"}},
        ),
        _fact(
            document_id=doc_c,
            subject="设备D2",
            obj="100Ah",
            created_at=_T9(8, 0),
            meta={"scope": {"source_system": "http://sys/erp"}},
        ),
        _fact(
            document_id=doc_d,
            subject="设备D2",
            obj="120Ah",
            status="candidate",
            created_at=_T9(9, 0),
            meta={"scope": {"source_system": "http://sys/crm"}},
        ),
    ]
    await _seed(factory, [_doc(d, title=f"源{i}") for i, d in enumerate([doc_a, doc_b, doc_c, doc_d])], facts)
    tickets = _FakeTickets()
    # Act
    report = await _run(
        factory,
        lambda db: triage_conflicts(db, TENANT, fact_pairs=[{"id": facts[1].id}, {"id": facts[3].id}], tickets=tickets),
    )
    # Assert
    by_cand = {o.candidate_fact_id: o for o in report.outcomes}
    assert by_cand[facts[1].id].triage == TRIAGE_T3  # 跨系统 localStatus：豁免 span、自动共存
    assert by_cand[facts[3].id].triage == TRIAGE_T2  # hasLimit 未声明 systemRelative：仍走 T2 工单
    assert [t["target_type"] for t in tickets.submitted] == [TARGET_TYPE_CONFLICT]


@sqlite_needed
async def test_T2_真矛盾_建冲突工单与评分参考(factory: async_sessionmaker[AsyncSession]) -> None:
    """无版本关系语义互斥 → T2：target_type=conflict 工单并排两事实+出处+四项评分（仅参考），不动事实。"""
    # Arrange：A 说 50 万、B 说 100 万，无链无 scope
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    existing = _fact(
        document_id=doc_a,
        subject="馈线F001",
        obj="50万",
        created_at=_T9(8, 0),
        evidence={"source_ref": {"document_id": str(doc_a), "doc_version": 1}, "quote": "审批上限50万", "span": [2, 8]},
    )
    cand = _fact(
        document_id=doc_b,
        subject="馈线F001",
        obj="100万",
        status="candidate",
        confidence=0.9,
        created_at=_T9(9, 0),
        evidence={
            "source_ref": {"document_id": str(doc_b), "doc_version": 1},
            "quote": "上限调整为100万",
            "span": [3, 12],
        },
    )
    await _seed(factory, [_doc(doc_a, title="制度A"), _doc(doc_b, title="制度B")], [existing, cand])
    tickets = _FakeTickets()
    # Act
    report = await _run(factory, lambda db: triage_conflicts(db, TENANT, fact_pairs=[{"id": cand.id}], tickets=tickets))
    # Assert：工单信封（standards/01 §5.3 统一信封 + decision_options 四选项）
    (outcome,) = report.outcomes
    assert outcome.triage == TRIAGE_T2 and outcome.matched_fact_id == existing.id
    (submit,) = tickets.submitted
    assert submit["target_type"] == TARGET_TYPE_CONFLICT and submit["target_id"] == cand.id
    assert outcome.ticket_id == submit["ticket_id"] and report.tickets_opened == 1
    inner = submit["payload"]["payload"]
    assert inner["conflict_type"] == TRIAGE_T2
    assert inner["fact_a_id"] == str(cand.id) and inner["fact_b_id"] == str(existing.id)
    assert inner["fact_a"]["subject"] == "馈线F001" and inner["fact_b"]["object"] == "50万"
    assert inner["fact_a"]["quote"] == "上限调整为100万" and inner["fact_a"]["source_ref"]["document_id"] == str(doc_b)
    assert submit["payload"]["decision_options"] == list(DECISION_OPTIONS)
    detail = inner["score_detail"]
    assert detail["fact_a"]["weights"] == {"authority": 0.35, "recency": 0.25, "corroboration": 0.20, "citation": 0.20}
    assert 0.0 <= detail["fact_a"]["total"] <= 1.0 and 0.0 <= detail["fact_b"]["total"] <= 1.0
    assert detail["fact_a"]["citation"] == 1.0 and detail["fact_b"]["recency"] == 0.0  # 新近者得分/双方 span 均落地
    assert _ct(await _fact_meta(factory, existing.id)) == {} and _ct(await _fact_meta(factory, cand.id)) == {}
    assert (await _edges(factory)) == []  # 评分不触发动作（v1 全人工，§8.1 v0.2）


@sqlite_needed
async def test_分诊顺序_T1命中不进T4(factory: async_sessionmaker[AsyncSession]) -> None:
    """同文档重抽主谓宾全同 → 固定顺序 T1 先于 T4：判版本演进自动承接，而非合并佐证。"""
    # Arrange：同文档旧权威 + 新候选，主谓宾全同（同时满足 T1 与 T4 特征）
    doc = uuid.uuid4()
    old_fact = _fact(document_id=doc, subject="馈线F001", obj="100Ah", created_at=_T9(8, 0))
    cand = _fact(document_id=doc, subject="馈线F001", obj="100Ah", status="candidate", created_at=_T9(9, 0))
    await _seed(factory, [_doc(doc, title="同文档重抽")], [old_fact, cand])
    tickets = _FakeTickets()
    # Act
    report = await _run(factory, lambda db: triage_conflicts(db, TENANT, fact_pairs=[{"id": cand.id}], tickets=tickets))
    # Assert：T1 命中（same_document）→ 封口建边；未走 T4（无 merged_source_refs）
    (outcome,) = report.outcomes
    assert outcome.triage == TRIAGE_T1 and "same_document" in outcome.reason
    async with factory() as db:
        row = await db.get(KbFact, old_fact.id)
        assert "merged_source_refs" not in row.evidence
    assert _ct(await _fact_meta(factory, old_fact.id))["state"] == STATE_SUPERSEDED
    assert [(e.from_fact_id, e.to_fact_id) for e in await _edges(factory)] == [(old_fact.id, cand.id)]


@sqlite_needed
async def test_对象属性多值合法与无端口响亮失败(factory: async_sessionmaker[AsyncSession]) -> None:
    """对象属性类（relation）主谓同宾语异=多值合法非矛盾（none）；T2 而端口未装配 → 409 响亮失败不静默。"""
    # Arrange：relation 型同主谓异宾语（连线多目标）；attribute 型真矛盾对（用于端口缺失断言）
    doc_a, doc_b, doc_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    old_rel = _fact(
        document_id=doc_a,
        subject="馈线F001",
        predicate="connectedTo",
        obj="台区T09",
        fact_type="relation",
        created_at=_T9(8, 0),
    )
    rel_cand = _fact(
        document_id=doc_b,
        subject="馈线F001",
        predicate="connectedTo",
        obj="台区T10",
        fact_type="relation",
        status="candidate",
        created_at=_T9(9, 0),
    )
    old_attr = _fact(document_id=doc_a, subject="台区T09", obj="100kVA", created_at=_T9(8, 0))
    attr_cand = _fact(document_id=doc_c, subject="台区T09", obj="120kVA", status="candidate", created_at=_T9(9, 30))
    await _seed(
        factory,
        [_doc(doc_a, title="A"), _doc(doc_b, title="B"), _doc(doc_c, title="C")],
        [old_rel, rel_cand, old_attr, attr_cand],
    )
    # Act / Assert：relation 多值合法 → none
    report = await _run(
        factory, lambda db: triage_conflicts(db, TENANT, fact_pairs=[{"id": rel_cand.id}], tickets=_FakeTickets())
    )
    (outcome,) = report.outcomes
    assert outcome.triage == TRIAGE_NONE and "多值合法" in outcome.reason
    # Act / Assert：attribute T2 无端口 → ConflictTriageError（409，不静默丢工单）
    with pytest.raises(ConflictTriageError, match="409"):
        await _run(factory, lambda db: triage_conflicts(db, TENANT, fact_pairs=[{"id": attr_cand.id}]))


async def test_入口参数二选一校验() -> None:
    """fact_pairs 与 doc_id 必须二选一（同传/同缺 → 3001；不触库恒跑）。"""
    with pytest.raises(ValueError, match="3001"):
        await triage_conflicts(None, TENANT)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="3001"):
        await triage_conflicts(  # type: ignore[arg-type]
            None, TENANT, fact_pairs=[{"id": uuid.uuid4()}], doc_id=uuid.uuid4()
        )


# ---------------------------------------------------------------- 事实级承接判定（§8.1 a/b/c）


def _chain_docs() -> tuple[uuid.UUID, uuid.UUID, Document, Document]:
    """旧版→新版版本链文档对（新 supersedes 旧；新版业务生效时间 valid_from=2024-03-01）。"""
    old_doc_id, new_doc_id = uuid.uuid4(), uuid.uuid4()
    old_doc = _doc(old_doc_id, title="旧版")
    new_doc = _doc(new_doc_id, title="新版", meta={"supersedes_id": str(old_doc_id)})
    new_doc.valid_from = datetime(2024, 3, 1)
    return old_doc_id, new_doc_id, old_doc, new_doc


@sqlite_needed
async def test_承接判定_a_同主谓重现封口建边(factory: async_sessionmaker[AsyncSession]) -> None:
    """a 分支：新版同主谓重现（一条拆两条）→ 旧封口（valid_to=新版业务生效时间）+ split 组级边。"""
    # Arrange
    old_doc_id, new_doc_id, old_doc, new_doc = _chain_docs()
    old_fact = _fact(document_id=old_doc_id, subject="保护R1", obj="100Ah", created_at=_T9(8, 0))
    succ_1 = _fact(document_id=new_doc_id, subject="保护R1", obj="120Ah", created_at=_T9(9, 0))
    succ_2 = _fact(document_id=new_doc_id, subject="保护R1", obj="备用120Ah", created_at=_T9(9, 1))
    await _seed(factory, [old_doc, new_doc], [old_fact, succ_1, succ_2])
    # Act
    report = await _run(factory, lambda db: carryover_facts(db, TENANT, new_doc_id, tickets=_FakeTickets()))
    # Assert
    (outcome,) = report.outcomes
    assert outcome.branch == CARRYOVER_BRANCH_A and outcome.successor_fact_id == succ_1.id
    ct = _ct(await _fact_meta(factory, old_fact.id))
    assert ct["state"] == STATE_SUPERSEDED and ct["valid_to"] == "2024-03-01T00:00:00"
    (edge,) = await _edges(factory)
    assert edge.evidence["cardinality"] == "split"
    assert set(edge.evidence["successor_group"]) == {str(succ_1.id), str(succ_2.id)}
    assert (edge.from_fact_id, edge.to_fact_id, edge.relation) == (old_fact.id, succ_1.id, RELATION_SUPERSEDED_BY)


@sqlite_needed
async def test_承接判定_b_词法命中疑似漏抽加急工单(factory: async_sessionmaker[AsyncSession]) -> None:
    """b 分支命中：旧宾语在新版 chunks 词法出现 → 疑似漏抽加急工单（knowledge_instance 复用），不动事实。"""
    # Arrange：旧事实宾语 100Ah 在新版原文出现但抽取未重现
    old_doc_id, new_doc_id, old_doc, new_doc = _chain_docs()
    old_fact = _fact(document_id=old_doc_id, subject="保护R1", obj="100Ah", created_at=_T9(8, 0))
    new_fact = _fact(document_id=new_doc_id, subject="保护R2", obj="200Ah", created_at=_T9(9, 0))
    await _seed(factory, [old_doc, new_doc], [old_fact, new_fact])
    tickets = _FakeTickets()
    chunks = ["新版第3条：保护R1 额定容量 100Ah 暂按旧版执行。"]
    # Act
    report = await _run(factory, lambda db: carryover_facts(db, TENANT, new_doc_id, new_chunks=chunks, tickets=tickets))
    # Assert
    (outcome,) = report.outcomes
    assert outcome.branch == CARRYOVER_BRANCH_B_HIT and outcome.ticket_id is not None
    (submit,) = tickets.submitted
    assert submit["target_type"] == "knowledge_instance" and submit["target_id"] == old_fact.id
    payload = submit["payload"]["payload"]
    assert payload["suspected_miss"] is True and payload["urgent"] is True and payload["fact"]["object"] == "100Ah"
    assert _ct(await _fact_meta(factory, old_fact.id)) == {} and (await _edges(factory)) == []


@sqlite_needed
async def test_承接判定_b_未命中转needs_review(factory: async_sessionmaker[AsyncSession]) -> None:
    """b 分支未命中：确认删除 → 转 needs_review（meta 承载默认可见待复核，不物理删除）。"""
    # Arrange
    old_doc_id, new_doc_id, old_doc, new_doc = _chain_docs()
    old_fact = _fact(document_id=old_doc_id, subject="保护R1", obj="100Ah", created_at=_T9(8, 0))
    new_fact = _fact(document_id=new_doc_id, subject="保护R2", obj="200Ah", created_at=_T9(9, 0))
    await _seed(factory, [old_doc, new_doc], [old_fact, new_fact])
    tickets = _FakeTickets()
    # Act
    report = await _run(
        factory,
        lambda db: carryover_facts(db, TENANT, new_doc_id, new_chunks=["新版全部参数已重写。"], tickets=tickets),
    )
    # Assert
    (outcome,) = report.outcomes
    assert outcome.branch == CARRYOVER_BRANCH_B_MISS and outcome.ticket_id is None
    ct = _ct(await _fact_meta(factory, old_fact.id))
    assert ct["state"] == STATE_NEEDS_REVIEW and ct["rule"] == "carryover_b_miss"
    assert tickets.submitted == [] and (await _edges(factory)) == []


@sqlite_needed
async def test_承接判定_c_明确否定走分诊(factory: async_sessionmaker[AsyncSession]) -> None:
    """c 分支：新版谓词/宾语含否定标记且宾语相异 → 走冲突分诊（同链对定 T1 承接，否定证据入边）。"""
    # Arrange：旧 100Ah 上限；新版同主谓明言「不再执行100Ah限值」
    old_doc_id, new_doc_id, old_doc, new_doc = _chain_docs()
    old_fact = _fact(document_id=old_doc_id, subject="保护R1", obj="100Ah", created_at=_T9(8, 0))
    new_fact = _fact(document_id=new_doc_id, subject="保护R1", obj="不再执行100Ah限值", created_at=_T9(9, 0))
    await _seed(factory, [old_doc, new_doc], [old_fact, new_fact])
    tickets = _FakeTickets()
    # Act
    report = await _run(factory, lambda db: carryover_facts(db, TENANT, new_doc_id, tickets=tickets))
    # Assert：分诊落 T1（文档链）自动承接 + 否定证据可追溯
    (outcome,) = report.outcomes
    assert outcome.branch == CARRYOVER_BRANCH_C and outcome.successor_fact_id == new_fact.id
    assert TRIAGE_T1 in outcome.reason and tickets.submitted == []
    ct = _ct(await _fact_meta(factory, old_fact.id))
    assert ct["state"] == STATE_SUPERSEDED and ct["note"] == "carryover_c_explicit_negation"
    (edge,) = await _edges(factory)
    assert edge.evidence["note"] == "carryover_c_explicit_negation" and edge.relation == RELATION_SUPERSEDED_BY
    assert _ct(await _fact_meta(factory, new_fact.id)) == {}  # 继任方不动


@sqlite_needed
async def test_承接判定_无版本链返回空(factory: async_sessionmaker[AsyncSession]) -> None:
    """新版无 supersedes_id → 空 outcomes 合法态（幂等，不误伤）。"""
    # Arrange
    new_doc_id = uuid.uuid4()
    await _seed(factory, [_doc(new_doc_id, title="独立文档")], [])
    # Act
    report = await _run(factory, lambda db: carryover_facts(db, TENANT, new_doc_id, tickets=_FakeTickets()))
    # Assert
    assert report.outcomes == ()


# ---------------------------------------------------------------- 冲突工单裁决执行（apply_decision）


def _ticket(fact_a: uuid.UUID, fact_b: uuid.UUID) -> dict[str, Any]:
    """冲突工单 get_ticket 形（T2 建单信封的裁决面最小消费形）。"""
    return {
        "id": uuid.uuid4(),
        "status": "pending_review",
        "target_type": TARGET_TYPE_CONFLICT,
        "target_id": fact_a,
        "payload": {"payload": {"fact_a_id": str(fact_a), "fact_b_id": str(fact_b)}},
    }


@sqlite_needed
async def test_裁决_点选胜者封口败者连边(factory: async_sessionmaker[AsyncSession]) -> None:
    """winner_a：败者（b）封口 + outranked_by 边 + needs_review 标注；胜者不动（败者不物理删除）。"""
    # Arrange：裁决前的 T2 对（双事实仍有效）
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    fact_a = _fact(document_id=doc_a, subject="馈线F001", obj="100万", created_at=_T9(9, 0))
    fact_b = _fact(document_id=doc_b, subject="馈线F001", obj="50万", created_at=_T9(8, 0))
    await _seed(factory, [_doc(doc_a, title="A"), _doc(doc_b, title="B")], [fact_a, fact_b])
    ticket = _ticket(fact_a.id, fact_b.id)
    # Act
    report = await _run(
        factory,
        lambda db: apply_decision(
            db,
            TENANT,
            ticket=ticket,
            decision={"resolution": "winner_a", "resolved_by": REVIEWER, "comment": "新制度为准"},
        ),
    )
    # Assert
    assert (report.resolution, report.winner_fact_id, report.loser_fact_id) == ("winner_a", fact_a.id, fact_b.id)
    assert report.facts_annotated == 1 and report.edges_created == 1
    ct = _ct(await _fact_meta(factory, fact_b.id))
    assert ct["state"] == STATE_OUTRANKED and ct["needs_review"] is True and ct["winner_fact_id"] == str(fact_a.id)
    assert ct["valid_to"] and ct["decided_by"] == str(REVIEWER)
    (edge,) = await _edges(factory)
    assert (edge.from_fact_id, edge.to_fact_id, edge.relation) == (fact_b.id, fact_a.id, RELATION_OUTRANKED_BY)
    assert edge.evidence["rule"] == "t2_manual_decision" and edge.evidence["ticket_id"] == str(ticket["id"])
    assert _ct(await _fact_meta(factory, fact_a.id)) == {}  # 胜者不动
    assert report.payload_patch["resolution"] == "winner_a" and report.payload_patch["comment"] == "新制度为准"


@sqlite_needed
async def test_裁决_T3共存人工填scope(factory: async_sessionmaker[AsyncSession]) -> None:
    """t3_coexist：人工填 scope 写入双方（键 ⊆ 封闭枚举），双保留共存。"""
    # Arrange
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    fact_a = _fact(document_id=doc_a, subject="台区T09", obj="120kVA", created_at=_T9(8, 0))
    fact_b = _fact(document_id=doc_b, subject="台区T09", obj="100kVA", created_at=_T9(9, 0))
    await _seed(factory, [_doc(doc_a, title="北京"), _doc(doc_b, title="上海")], [fact_a, fact_b])
    ticket = _ticket(fact_a.id, fact_b.id)
    decision = {
        "resolution": "t3_coexist",
        "scope_a": {"region": "110000"},
        "scope_b": {"region": "310000"},
        "resolved_by": REVIEWER,
    }
    # Act
    report = await _run(factory, lambda db: apply_decision(db, TENANT, ticket=ticket, decision=decision))
    # Assert
    assert report.resolution == "t3_coexist" and report.facts_annotated == 2 and (await _edges(factory)) == []
    for fid, region in ((fact_a.id, "110000"), (fact_b.id, "310000")):
        meta = await _fact_meta(factory, fid)
        assert meta["scope"] == {"region": region} and _ct(meta)["state"] == STATE_COEXIST


@sqlite_needed
async def test_裁决_待定不动事实与非法入参(factory: async_sessionmaker[AsyncSession]) -> None:
    """pending：不动事实仅回 payload_patch；非法 resolution / scope 越封闭枚举 → 3001。"""
    # Arrange
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    fact_a = _fact(document_id=doc_a, subject="馈线F001", obj="100万", created_at=_T9(9, 0))
    fact_b = _fact(document_id=doc_b, subject="馈线F001", obj="50万", created_at=_T9(8, 0))
    await _seed(factory, [_doc(doc_a, title="A"), _doc(doc_b, title="B")], [fact_a, fact_b])
    ticket = _ticket(fact_a.id, fact_b.id)
    # Act：pending
    report = await _run(
        factory, lambda db: apply_decision(db, TENANT, ticket=ticket, decision={"resolution": "pending"})
    )
    # Assert：零事实副作用，留痕补丁可回填工单
    assert report.facts_annotated == 0 and report.payload_patch["resolution"] == "pending"
    assert _ct(await _fact_meta(factory, fact_a.id)) == {} and (await _edges(factory)) == []
    # Assert：非法 resolution / 越枚举 scope / 缺 scope → 3001
    with pytest.raises(ValueError, match="3001"):
        await _run(factory, lambda db: apply_decision(db, TENANT, ticket=ticket, decision={"resolution": "auto"}))
    with pytest.raises(ValueError, match="3001"):
        await _run(
            factory,
            lambda db: apply_decision(
                db,
                TENANT,
                ticket=ticket,
                decision={"resolution": "t3_coexist", "scope_a": {"foo": 1}, "scope_b": {"region": "110000"}},
            ),
        )
    with pytest.raises(ValueError, match="3001"):
        await _run(factory, lambda db: apply_decision(db, TENANT, ticket=ticket, decision={"resolution": "t3_coexist"}))
    # Assert：非冲突单拒收
    with pytest.raises(ValueError, match="3001"):
        await _run(
            factory,
            lambda db: apply_decision(
                db, TENANT, ticket={**ticket, "target_type": "knowledge_instance"}, decision={"resolution": "pending"}
            ),
        )

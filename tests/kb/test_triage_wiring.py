# tests/kb/test_triage_wiring.py
"""冲突分诊接线闭环用例（知识库问题清单 A1，波次⑩；纯 aiosqlite 内存库，零 PG 真连）。

背景：共享 PG 池被并行会话耗尽——本文件全部 aiosqlite + @compiles shim（tests/kb/
test_conflict_triage.py / test_conflict_governance.py 同款先例：JSONB/UUID 降编译到 SQLite
DDL），事务内 create_all 建本切片四表（kb_facts + kb_fact_relations + documents +
review_tickets）。

覆盖（A1 接线四验收面）：
① 流水线接线：run_validate 尾调 triage_conflicts——同主谓矛盾候选（过 SHACL 门禁）+ 既有
   权威事实 → T2 冲突工单生成（target_type=conflict，复用 CandidateReviewPort 通道）；
② 终审分流：候选牵涉 conflict open 单时 accept/reject 联动 apply_decision——工单先推进
   approved/rejected，败者封口（meta conflict_triage.state=outranked + needs_review）+
   outranked_by 边落库 + conflict_decision 回填信封留痕；
③ 双实现归一注记存在性（governance.py 模块头收编声明，防回退）；
④ 冲突工单两端点直调（ReviewReadDep 鉴权模式照抄 test_review_api）：列表 resolution 过滤
   与分页、详情并排双方事实与出处 + 实时行态 + 裁决留痕、404 与 scope 门禁。

已登记 A1 遗留（接线时发现，非本批授权域）：conflict_triage._fact_digest 携带 created_at
（datetime）——默认 json serializer 不可序列化，生产 PG 引擎（platform/db/uow.py 无自定义
json_serializer）T2 建单会在 flush 时 TypeError；本文件以测试引擎 json_serializer=default=str
验证接线逻辑，生产收口（引擎 serializer 或 digest 改 ISO 串）随 A1 遗留另切片。

psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略（仓库同款）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from starlette.requests import Request as StarletteRequest

from services.gateway.app import create_app
from services.iam.data.orm import Tenant, User  # noqa: F401  FK 目标表入 metadata（sqlite 只解析不建目标表）
from services.kb.api.kb import decide_candidate, get_conflict, list_conflicts
from services.kb.api.schemas.kb import CandidateDecisionIn
from services.kb.business.conflict_triage import (
    DECISION_OPTIONS,
    RELATION_OUTRANKED_BY,
    STATE_OUTRANKED,
    TARGET_TYPE_CONFLICT,
)
from services.kb.business.kb_extraction import run_validate
from services.kb.business.pipeline_base import StepContext
from services.kb.data.governance_orm import KbFactRelation
from services.kb.data.orm import (  # noqa: F401  KbCollection：documents FK 目标入 metadata
    Document,
    KbCollection,
    KbFact,
)
from services.platform.config import Settings
from services.platform.db.base import Base
from services.platform.deps import Principal, require_scope
from services.platform.errors import GatewayError
from services.review.business.candidates import ReviewTicketService
from services.review.data.orm import ReviewTicket

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    import aiosqlite  # noqa: F401

    _HAS_AIOSQLITE = True
except ImportError:  # 本地依赖缺失：全部落库/接线用例跳过（归一注记用例不触库恒跑）
    _HAS_AIOSQLITE = False

sqlite_needed = pytest.mark.skipif(not _HAS_AIOSQLITE, reason="aiosqlite 未安装：冲突分诊接线用例")

# ── SQLite 方言 shim（test_conflict_triage.py 同款：PG 专列类型建表降编译；幂等重注册无害）──


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSONB"


@compiles(PgUuid, "sqlite")
def _sqlite_uuid(type_: Any, compiler: Any, **kw: Any) -> str:
    return "CHAR(32)"


TENANT = uuid.uuid4()
REVIEWER = uuid.uuid4()

_GOVERNANCE_PATH = Path(__file__).resolve().parents[2] / "services" / "kb" / "business" / "governance.py"


def _fact(
    *,
    document_id: uuid.UUID,
    subject: str,
    predicate: str | None = "repairCrewAvailable",
    obj: str | None = None,  # noqa: A002  与 kb_facts 列名同形
    status: str = "authoritative",
    confidence: float = 0.8,
    fact_type: str = "attribute",
    created_at: datetime | None = None,
) -> KbFact:
    """kb_facts 行值（谓词取种子白名单 repairCrewAvailable：候选过 SHACL 门禁保持 candidate）。"""
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
        evidence={"source_ref": {"document_id": str(document_id), "doc_version": 1}},
        violations=[],
        meta={},
        aliases=[],
    )
    if created_at is not None:
        row.created_at = created_at
    return row


def _conflict_envelope(
    fact_a_id: uuid.UUID,
    fact_b_id: uuid.UUID,
    *,
    fact_a_obj: str = "true",
    fact_b_obj: str = "false",
    predicate: str = "repairCrewAvailable",
) -> dict[str, Any]:
    """T2 建单信封（conflict_triage._triage_one 产形的最小等价——裁决面消费契约）。"""
    return {
        "envelope_version": "v1",
        "candidate_type": TARGET_TYPE_CONFLICT,
        "template_ref": "conflict_triage@v1",
        "trace_id": "kb-triage-wiring-it",
        "payload": {
            "conflict_type": "T2",
            "fact_a_id": str(fact_a_id),
            "fact_b_id": str(fact_b_id),
            "fact_a": {"id": str(fact_a_id), "subject": "馈线F001", "predicate": predicate,
                       "object": fact_a_obj, "scope": {}, "source_ref": {}, "confidence": 0.9},
            "fact_b": {"id": str(fact_b_id), "subject": "馈线F001", "predicate": predicate,
                       "object": fact_b_obj, "scope": {}, "source_ref": {}, "confidence": 0.8},
            "score_detail": {},
        },
        "decision_options": list(DECISION_OPTIONS),
        "review": {"state": "pending_review"},
    }


async def _seed(factory: async_sessionmaker[AsyncSession], *rows: Any) -> None:
    """事务内批量播种（documents/kb_facts/review_tickets 行值混排）。"""
    async with factory() as db, db.begin():
        for row in rows:
            db.add(row)


def _doc(doc_id: uuid.UUID, *, title: str) -> Document:
    return Document(
        id=doc_id,
        tenant_id=TENANT,
        kb_collection_id=uuid.uuid4(),  # kb_collections 不建表：documents FK 目标缺席（sqlite 只解析）
        title=title,
        minio_key=f"raw-docs/{doc_id}",
        checksum_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        meta={},
    )


def _principal(*, tenant_id: uuid.UUID = TENANT, scopes: list[str] | None = None) -> Principal:
    return Principal(
        {
            "sub": str(REVIEWER),
            "tenant_id": str(tenant_id),
            "roles": ["reviewer"],
            "scopes": scopes or ["review:read", "review:approve"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


def _request() -> StarletteRequest:
    """携带 app 与候选审核单服务装配的最小 Request（不跑 lifespan，tests/kb/test_review_api 同款）。"""
    app = create_app(Settings())
    app.state.candidate_review = ReviewTicketService(_FACTORY)  # type: ignore[arg-type]
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/kb",
        "raw_path": b"/api/v1/kb",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
    }
    request = StarletteRequest(scope)
    request.state.trace_id = "kb-triage-wiring-it"
    return request


_FACTORY: async_sessionmaker[AsyncSession] | None = None  # 模块级装配缝（_request 闭包引用，fixture 内赋值）


async def _fact_meta(factory: async_sessionmaker[AsyncSession], fact_id: uuid.UUID) -> dict[str, Any]:
    async with factory() as db:
        row = await db.get(KbFact, fact_id)
        assert row is not None
        return dict(row.meta or {})


async def _edges(factory: async_sessionmaker[AsyncSession]) -> list[KbFactRelation]:
    async with factory() as db:
        return list((await db.execute(select(KbFactRelation))).scalars().all())


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """aiosqlite 内存库（StaticPool 单连接共享）+ 事务内建表（本切片四表）。

    json_serializer=default=str：T2 建单信封 _fact_digest 携带 created_at（datetime）——
    sqlite 默认 json.dumps 不可序列化（生产 PG 引擎同口、收口登记 A1 遗留，见模块 docstring）。
    """
    global _FACTORY
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        json_serializer=lambda value: json.dumps(value, default=str, ensure_ascii=False),
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c, tables=[KbFact.__table__, KbFactRelation.__table__, Document.__table__, ReviewTicket.__table__]
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    _FACTORY = session_factory  # _request 装配缝（端点直调的 app.state.candidate_review）
    yield session_factory
    _FACTORY = None
    await engine.dispose()


# ---------------------------------------------------------------- ① 流水线接线：run_validate 尾调分诊


@sqlite_needed
async def test_run_validate_尾调分诊_T2矛盾候选建冲突工单(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A1 接线点①：validate 门禁回写后对合规候选跑 triage_conflicts——同主谓矛盾 → conflict 工单。"""
    old_doc_id, new_doc_id = uuid.uuid4(), uuid.uuid4()
    old_fact = _fact(document_id=old_doc_id, subject="馈线F001", obj="false", created_at=datetime(2026, 9, 1, 8, 0))
    cand = _fact(
        document_id=new_doc_id, subject="馈线F001", obj="true", status="candidate",
        confidence=0.9, created_at=datetime(2026, 9, 1, 9, 0),
    )
    await _seed(factory, _doc(old_doc_id, title="制度A"), _doc(new_doc_id, title="制度B"), old_fact, cand)
    tickets = ReviewTicketService(factory)
    await tickets.submit_candidate(  # 候选 open 单（validate 门禁 gate_result 回写前置，extract 双写同款）
        tenant_id=TENANT,
        target_type="knowledge_instance",
        target_id=cand.id,
        payload={"envelope_version": "v1", "candidate_type": "knowledge_instance", "payload": {}, "review": {}},
    )
    ctx = StepContext(
        session_factory=factory,
        tenant_id=TENANT,
        document_id=new_doc_id,
        embedder=None,
        model=None,
        review=tickets,
    )
    # Act：SHACL 门禁（候选合规保持 candidate）→ 尾调分诊 → T2 建单
    await run_validate(ctx)

    async with factory() as db:
        rows = (await db.execute(select(ReviewTicket).where(ReviewTicket.tenant_id == TENANT))).scalars().all()
        cand_row = await db.get(KbFact, cand.id)
        old_row = await db.get(KbFact, old_fact.id)
    by_type = {(t.target_type, t.target_id): t for t in rows}
    conflict = by_type[(TARGET_TYPE_CONFLICT, cand.id)]
    assert conflict.status == "pending_review"
    inner = conflict.payload["payload"]
    assert inner["conflict_type"] == "T2"
    assert inner["fact_a_id"] == str(cand.id) and inner["fact_b_id"] == str(old_fact.id)
    assert inner["fact_a"]["object"] == "true" and inner["fact_b"]["object"] == "false"
    assert conflict.payload["decision_options"] == list(DECISION_OPTIONS)
    assert conflict.payload["trace_id"] == f"kb-validate:{new_doc_id}"
    # T2 不动事实（v1 全人工裁决）；门禁面照旧：合规候选保持 candidate 且无违例
    assert cand_row is not None and cand_row.status == "candidate" and cand_row.violations == []
    assert old_row is not None and old_row.status == "authoritative"
    assert "conflict_triage" not in (old_row.meta or {}) and "conflict_triage" not in (cand_row.meta or {})
    assert (await _edges(factory)) == []


# ---------------------------------------------------------------- ② 终审分流：decision → apply_decision


async def _seed_conflict_pair(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[ReviewTicketService, uuid.UUID, uuid.UUID, uuid.UUID]:
    """终审分流环境：fact_a(candidate)+fact_b(authoritative)+双单（knowledge_instance + conflict）。"""
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    fact_a = _fact(
        document_id=doc_a, subject="馈线F001", obj="100万", status="candidate", confidence=0.9,
        predicate="hasLimit", created_at=datetime(2026, 9, 1, 9, 0),
    )
    fact_b = _fact(
        document_id=doc_b, subject="馈线F001", obj="50万", status="authoritative", confidence=0.8,
        predicate="hasLimit", created_at=datetime(2026, 9, 1, 8, 0),
    )
    await _seed(factory, _doc(doc_a, title="制度A"), _doc(doc_b, title="制度B"), fact_a, fact_b)
    tickets = ReviewTicketService(factory)
    await tickets.submit_candidate(
        tenant_id=TENANT,
        target_type="knowledge_instance",
        target_id=fact_a.id,
        payload={"envelope_version": "v1", "candidate_type": "knowledge_instance", "payload": {}, "review": {}},
    )
    await tickets.submit_candidate(
        tenant_id=TENANT,
        target_type=TARGET_TYPE_CONFLICT,
        target_id=fact_a.id,
        payload=_conflict_envelope(fact_a.id, fact_b.id),
    )
    return tickets, fact_a.id, fact_b.id, fact_a.document_id


@sqlite_needed
async def test_终审_accept_分流冲突工单_败者封口连边(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A1 接线点②：accept → 工单 approved + apply_decision(winner_a)——fact_b 封口 + outranked_by 边。"""
    tickets, fact_a_id, fact_b_id, _ = await _seed_conflict_pair(factory)
    async with factory() as db:
        out = await decide_candidate(fact_a_id, CandidateDecisionIn(action="accept"), _principal(), _request(), db)
        await db.commit()
    assert out.status == "authoritative" and out.trail_recorded is True
    # 败者（既有权威方）封口：meta 遮蔽标注，不物理删除、status 不翻（硬门禁 DDL 未回填）
    meta = await _fact_meta(factory, fact_b_id)
    ct = meta.get("conflict_triage") or {}
    assert ct.get("state") == STATE_OUTRANKED and ct.get("needs_review") is True
    assert ct.get("winner_fact_id") == str(fact_a_id) and ct.get("valid_to")
    (edge,) = await _edges(factory)
    assert (edge.from_fact_id, edge.to_fact_id, edge.relation) == (fact_b_id, fact_a_id, RELATION_OUTRANKED_BY)
    assert edge.evidence["rule"] == "t2_manual_decision"
    # 工单先推进 approved（08 §4 终态语义），裁决留痕回填信封
    ticket = await tickets.get_latest_ticket(tenant_id=TENANT, target_type=TARGET_TYPE_CONFLICT, target_id=fact_a_id)
    assert ticket is not None and ticket["status"] == "approved"
    assert ticket["payload"]["conflict_decision"]["resolution"] == "winner_a"
    assert ticket["payload"]["conflict_decision"]["resolved_by"] == str(REVIEWER)


@sqlite_needed
async def test_终审_reject_分流冲突工单_既有方胜出(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """reject → 工单 rejected + apply_decision(winner_b)——候选（fact_a）自身标 outranked 封口。"""
    tickets, fact_a_id, fact_b_id, _ = await _seed_conflict_pair(factory)
    async with factory() as db:
        out = await decide_candidate(fact_a_id, CandidateDecisionIn(action="reject"), _principal(), _request(), db)
        await db.commit()
    assert out.status == "rejected"
    ct = (await _fact_meta(factory, fact_a_id)).get("conflict_triage") or {}
    assert ct.get("state") == STATE_OUTRANKED and ct.get("winner_fact_id") == str(fact_b_id)
    (edge,) = await _edges(factory)
    assert (edge.from_fact_id, edge.to_fact_id, edge.relation) == (fact_a_id, fact_b_id, RELATION_OUTRANKED_BY)
    ticket = await tickets.get_latest_ticket(tenant_id=TENANT, target_type=TARGET_TYPE_CONFLICT, target_id=fact_a_id)
    assert ticket is not None and ticket["status"] == "rejected"
    assert ticket["payload"]["conflict_decision"]["resolution"] == "winner_b"


@sqlite_needed
async def test_终审_无冲突单_零分流_单已终态_不重复分流(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """无 conflict 单 → 终审照旧（零分流）；conflict 单已终态 → 不重复分流（迟保护）。"""
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    fact_a = _fact(document_id=doc_a, subject="馈线F001", obj="120Ah", status="candidate", predicate="hasLimit")
    fact_b = _fact(document_id=doc_b, subject="馈线F001", obj="100Ah", status="authoritative", predicate="hasLimit")
    await _seed(factory, _doc(doc_a, title="A"), _doc(doc_b, title="B"), fact_a, fact_b)
    tickets = ReviewTicketService(factory)  # 只挂 knowledge_instance 单：无 conflict 单
    await tickets.submit_candidate(
        tenant_id=TENANT,
        target_type="knowledge_instance",
        target_id=fact_a.id,
        payload={"envelope_version": "v1", "candidate_type": "knowledge_instance", "payload": {}, "review": {}},
    )
    async with factory() as db:
        out = await decide_candidate(fact_a.id, CandidateDecisionIn(action="accept"), _principal(), _request(), db)
        await db.commit()
    assert out.status == "authoritative" and (await _edges(factory)) == []
    assert (await _fact_meta(factory, fact_b.id)).get("conflict_triage") is None


# ---------------------------------------------------------------- ③ 双实现归一注记（防回退）


def test_governance_头部收编注记存在() -> None:
    """A1 裁决防回退面：governance.py 模块头必须声明「已收编：生产口径以 conflict_triage.py 为准」。"""
    head = _GOVERNANCE_PATH.read_text(encoding="utf-8")[:1200]
    assert "已收编" in head and "conflict_triage.py" in head
    assert "A1 接线裁决 2026-10-04" in head


# ---------------------------------------------------------------- ④ 冲突工单两端点直调


async def _seed_one_conflict(
    factory: async_sessionmaker[AsyncSession], *, settled: bool = False, obj: str = "50万"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """单张 conflict 工单（settled=True 时预回填 conflict_decision=winner_a 模拟已裁决）。"""
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    fact_a = _fact(document_id=doc_a, subject="馈线F001", obj="100万", status="candidate", predicate="hasLimit")
    fact_b = _fact(document_id=doc_b, subject="馈线F001", obj=obj, status="authoritative", predicate="hasLimit")
    await _seed(factory, _doc(doc_a, title="A"), _doc(doc_b, title="B"), fact_a, fact_b)
    payload = _conflict_envelope(
        fact_a.id, fact_b.id, fact_a_obj="100万", fact_b_obj=obj, predicate="hasLimit"
    )
    if settled:
        payload["conflict_decision"] = {
            "resolution": "winner_a",
            "resolved_by": str(REVIEWER),
            "comment": "新制度为准",
            "decided_at": "2026-10-04T00:00:00+00:00",
        }
    async with factory() as db, db.begin():
        ticket = ReviewTicket(
            tenant_id=TENANT,
            target_type=TARGET_TYPE_CONFLICT,
            target_id=fact_a.id,
            payload=payload,
            status="approved" if settled else "pending_review",
        )
        db.add(ticket)
        await db.flush()
        return ticket.id, fact_a.id, fact_b.id


@sqlite_needed
async def test_GET_conflicts_列表_resolution过滤_分页(factory: async_sessionmaker[AsyncSession]) -> None:
    """列表：缺省=全部；pending=工单 open 态；winner_a=信封已回填裁决；分页 total 恒定。"""
    open_id, _, _ = await _seed_one_conflict(factory)
    settled_id, _, _ = await _seed_one_conflict(factory, settled=True, obj="80万")
    async with factory() as db:
        page = await list_conflicts(_principal(), db)
        assert page.meta.total == 2 and {t.id for t in page.data} == {open_id, settled_id}
        pend = await list_conflicts(_principal(), db, resolution="pending")
        assert pend.meta.total == 1 and pend.data[0].id == open_id and pend.data[0].resolution == "pending"
        assert pend.data[0].status == "pending_review" and pend.data[0].conflict_type == "T2"
        won = await list_conflicts(_principal(), db, resolution="winner_a")
        assert won.meta.total == 1 and won.data[0].id == settled_id and won.data[0].resolution == "winner_a"
        none_won = await list_conflicts(_principal(), db, resolution="winner_b")
        assert none_won.meta.total == 0 and none_won.data == []
        p1 = await list_conflicts(_principal(), db, page=1, page_size=1)
        p2 = await list_conflicts(_principal(), db, page=2, page_size=1)
        assert p1.meta.total == 2 and p2.meta.total == 2 and len(p1.data) == 1 and len(p2.data) == 1
        assert p1.data[0].id != p2.data[0].id  # 新单优先排序下不重不漏


@sqlite_needed
async def test_GET_conflicts_id_详情_双方事实与出处_裁决留痕(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """详情：并排双方事实（digest 快照 + kb_facts 实时行态）+ 出处 + decision_options + 留痕。"""
    ticket_id, fact_a_id, fact_b_id = await _seed_one_conflict(factory, settled=True, obj="80万")
    async with factory() as db:
        detail = await get_conflict(ticket_id, _principal(), db)
    assert detail.id == ticket_id and detail.status == "approved" and detail.resolution == "winner_a"
    assert detail.conflict_type == "T2" and detail.decision_options == list(DECISION_OPTIONS)
    assert detail.fact_a.fact_id == fact_a_id and detail.fact_a.subject == "馈线F001"
    assert detail.fact_a.object == "100万" and detail.fact_a.status == "candidate"  # 实时行态
    assert detail.fact_b.fact_id == fact_b_id and detail.fact_b.object == "80万"
    assert detail.fact_b.status == "authoritative" and detail.fact_b.source_ref == {}
    assert detail.decision is not None and detail.decision["resolution"] == "winner_a"


@sqlite_needed
async def test_GET_conflicts_不存在与跨租户_404(factory: async_sessionmaker[AsyncSession]) -> None:
    """不存在 → 404；他租户 principal → 404（deny-by-default，工单与事实均不透出）。"""
    ticket_id, _, _ = await _seed_one_conflict(factory)
    async with factory() as db:
        with pytest.raises(GatewayError) as exc:
            await get_conflict(uuid.uuid4(), _principal(), db)
        assert exc.value.status_code == 404
        with pytest.raises(GatewayError) as exc:
            await get_conflict(ticket_id, _principal(tenant_id=uuid.uuid4()), db)
        assert exc.value.status_code == 404
        other = await list_conflicts(_principal(tenant_id=uuid.uuid4()), db)
        assert other.meta.total == 0


async def test_review_scope门禁_2001拒绝() -> None:
    """端点直调绕过 Depends 解析，直测依赖工厂（tests/kb/test_review_api 同款）：deny-by-default。"""
    principal = _principal(scopes=["kb:read"])  # kb scope 不等于 review scope
    with pytest.raises(GatewayError) as exc:
        require_scope("review:read")(principal)
    assert exc.value.code == 2001 and exc.value.status_code == 403

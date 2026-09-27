# tests/kb/test_review_queue.py
"""needs_review 聚合复核用例（OntRAG §7.1 批次纪律/§8.1 b 类承接判定；纯 aiosqlite 内存库，零 PG 真连）。

背景：共享 PG 连接池当前被并行会话耗尽——本文件全部用 aiosqlite 内存库（StaticPool 单连接 +
事务内建表，test_connector.py @compiles shim 先例），生产同一 ORM/查询构造路径（窗口函数/聚合
均为方言中立 Core SQL；PG 真连用例不写，规避池耗尽挂起套件）。

状态词汇纪律：kb_facts 现行 CheckConstraint 仅 candidate|rejected|authoritative——落库用例以
candidate 队列经 statuses=("candidate",) 跑通聚合/裁决全路径；needs_review（§8.1 b 类目标态）
走「缺省词汇 → v1 数据零命中 → decided=0」断言（同时覆盖空队列返回空）。

覆盖：分组计数与最旧排序、样本 ≤3 且取最旧、batch_decide 全组裁决（跨文档聚拢/组级原子/
行内审计留痕/open 单 payload 留痕复用 fake 单据服务）、非法 decision（DTO 422 面 + 服务 3001
防御）、空队列返回空、租户隔离、路由直调信封（端点直调=tests/kb/test_review_api 同款）。

psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略（仓库同款）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from starlette.requests import Request as StarletteRequest

from services.iam.data.orm import (
    Tenant as TenantORM,  # noqa: F401 — kb_facts.tenant_id FK 解析需 tenants 表对象入共享 metadata
)
from services.kb.api.kb import review_queue_batch_decide, review_queue_summary
from services.kb.api.schemas.kb import ReviewQueueBatchDecideIn, ReviewQueueBatchDecideOut, ReviewQueueSummaryOut
from services.kb.business.review_queue import ReviewQueueService
from services.kb.data.orm import KbFact as KbFactORM
from services.platform.db.base import Base
from services.platform.deps import Principal

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    import aiosqlite  # noqa: F401

    _HAS_AIOSQLITE = True
except ImportError:  # 本地依赖缺失：仅落库用例跳过（DTO 契约用例不受影响）
    _HAS_AIOSQLITE = False

sqlite_needed = pytest.mark.skipif(not _HAS_AIOSQLITE, reason="aiosqlite 未安装：聚合复核落库用例")

# ── SQLite 方言 shim（test_connector.py 同款：PG 专列类型建表降编译；幂等重注册无害）──


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSONB"


@compiles(PgUuid, "sqlite")
def _sqlite_uuid(type_: Any, compiler: Any, **kw: Any) -> str:
    return "CHAR(32)"


TENANT = uuid.uuid4()
TENANT_OTHER = uuid.uuid4()
REVIEWER = uuid.uuid4()
DOC_A = uuid.uuid4()
DOC_B = uuid.uuid4()

_T = lambda h, m: datetime(2026, 9, 1, h, m)  # noqa: E731 — 种子时间戳（tz-aware 入库，SQLite 往返为 naive 同值）


def _seed_row(
    tenant_id: uuid.UUID,
    *,
    subject: str,
    status: str = "candidate",
    subject_type: str | None = "http://oa.local/power#Feeder",
    document_id: uuid.UUID = DOC_A,
    chunk_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """kb_facts 行值（聚合/裁决断言所需最小列集；CHECK 约束词汇内合法）。"""
    row: dict[str, Any] = {
        "tenant_id": tenant_id,
        "document_id": document_id,
        "chunk_id": chunk_id,
        "fact_type": "entity",
        "subject": subject,
        "subject_type": subject_type,
        "confidence": 0.8,
        "status": status,
        "evidence": {},
        "violations": [],
        "meta": {},
    }
    if created_at is not None:
        row["created_at"] = created_at
    return row


async def _add_rows(factory: async_sessionmaker[AsyncSession], rows: list[dict[str, Any]]) -> dict[str, uuid.UUID]:
    """事务内批量播种，返回 {subject: [fact_id 按插入序]} 供样本断言回查。"""
    ids: dict[str, list[uuid.UUID]] = {}
    async with factory() as db, db.begin():
        for row in rows:
            instance = KbFactORM(**row)
            db.add(instance)
            await db.flush()
            ids.setdefault(row["subject"], []).append(instance.id)
    return {subject: list(seq) for subject, seq in ids.items()}


@pytest.fixture
async def kb_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """aiosqlite 内存库（StaticPool 单连接共享）+ 事务内建表（仅 kb_facts 一表）。"""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[KbFactORM.__table__]))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def svc(kb_factory: async_sessionmaker[AsyncSession]) -> ReviewQueueService:
    return ReviewQueueService(kb_factory)


class _CandidateQueue(ReviewQueueService):
    """v1 candidate 队列桥接桩（路由缺省 needs_review 词汇不变；生产经 statuses 参数显式传入）。"""

    async def queue_summary(self, tenant_id: uuid.UUID, **kwargs: Any) -> list:
        kwargs["statuses"] = ("candidate",)
        return await super().queue_summary(tenant_id, **kwargs)

    async def batch_decide(self, tenant_id: uuid.UUID, **kwargs: Any) -> Any:
        kwargs["statuses"] = ("candidate",)
        return await super().batch_decide(tenant_id, **kwargs)


class _StubApp:
    """最小 app 桩：路由仅触达 app.state（装配缝注入服务；settings 永不触网）。"""

    def __init__(self, service: ReviewQueueService) -> None:
        self.state = SimpleNamespace(kb_review_queue=service, candidate_review=None, settings=None)


def _request(app: _StubApp) -> StarletteRequest:
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/kb/review-queue",
        "raw_path": b"/api/v1/kb/review-queue",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
    }
    request = StarletteRequest(scope)
    request.state.trace_id = "kb-review-queue-test"
    return request


def _principal(tenant_id: uuid.UUID = TENANT) -> Principal:
    return Principal(
        {
            "sub": str(REVIEWER),
            "tenant_id": str(tenant_id),
            "roles": ["reviewer"],
            "scopes": ["review:read", "review:approve"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


class _FakeTickets:
    """ReviewTicketService 鸭子类型桩（get_latest_ticket/merge_payload 面，留痕复用断言用）。"""

    def __init__(self) -> None:
        self._tickets: dict[uuid.UUID, dict[str, Any]] = {}
        self.merged_payloads: list[dict[str, Any]] = []

    def add(self, target_id: uuid.UUID, status: str) -> dict[str, Any]:
        ticket = {
            "id": uuid.uuid4(),
            "status": status,
            "target_type": "knowledge_instance",
            "target_id": target_id,
            "payload": {"decisions": []},
        }
        self._tickets[target_id] = ticket
        return ticket

    async def get_latest_ticket(self, *, tenant_id: uuid.UUID, target_type: str, target_id: uuid.UUID) -> Any:
        ticket = self._tickets.get(target_id)
        return None if ticket is None else dict(ticket)

    async def merge_payload(self, *, tenant_id: uuid.UUID, ticket_id: uuid.UUID, payload: dict[str, Any]) -> None:
        self.merged_payloads.append(payload)
        for ticket in self._tickets.values():
            if ticket["id"] == ticket_id:
                ticket["payload"] = payload


# ---------------------------------------------------------------- 聚合查询（queue_summary）


@sqlite_needed
async def test_分组计数与最旧排序(svc: ReviewQueueService, kb_factory: async_sessionmaker[AsyncSession]) -> None:
    """最旧组优先；每组 count/oldest_created_at 正确（同 subject 异 subject_type 按组属性拆分）。"""
    await _add_rows(
        kb_factory,
        [
            _seed_row(TENANT, subject="馈线F001", created_at=_T(9, 0), document_id=DOC_A),
            _seed_row(TENANT, subject="馈线F001", created_at=_T(9, 5), chunk_id=uuid.uuid4()),
            _seed_row(TENANT, subject="馈线F001", created_at=_T(9, 10), document_id=DOC_B),
            _seed_row(TENANT, subject="台区T09", created_at=_T(8, 0), subject_type="http://oa.local/power#Transformer"),
        ],
    )
    groups = await svc.queue_summary(TENANT, statuses=("candidate",))
    assert [(g.subject, g.count) for g in groups] == [("台区T09", 1), ("馈线F001", 3)]  # 最旧组优先
    assert groups[0].oldest_created_at == _T(8, 0)
    assert groups[1].oldest_created_at == _T(9, 0)
    assert groups[0].subject_type == "http://oa.local/power#Transformer"
    assert groups[1].subject_type == "http://oa.local/power#Feeder"


@sqlite_needed
async def test_样本上限3条且取最旧(svc: ReviewQueueService, kb_factory: async_sessionmaker[AsyncSession]) -> None:
    """5 行组只出 3 条样本，且恰为最旧 3 条（chunk/document 引用随行）。"""
    chunks = [uuid.uuid4() for _ in range(5)]
    fact_ids = await _add_rows(
        kb_factory,
        [
            _seed_row(TENANT, subject="线路L5", created_at=_T(10, i), chunk_id=chunks[i])
            for i in range(5)
        ],
    )
    groups = await svc.queue_summary(TENANT, statuses=("candidate",))
    assert len(groups) == 1
    group = groups[0]
    assert group.count == 5
    assert len(group.samples) == 3  # 上限 3 条
    assert [s.fact_id for s in group.samples] == fact_ids["线路L5"][:3]  # 最旧优先
    assert [s.chunk_id for s in group.samples] == chunks[:3]
    assert group.samples[0].document_id == DOC_A
    assert group.oldest_created_at == _T(10, 0)  # 组最旧时间戳 = 样本首行（rn=1）


@sqlite_needed
async def test_空队列返回空与租户隔离(svc: ReviewQueueService, kb_factory: async_sessionmaker[AsyncSession]) -> None:
    """空库 → []；他人租户候选不可见（deny-by-default）。"""
    assert await svc.queue_summary(TENANT) == []  # 空库 + 缺省 needs_review 词汇 → 空
    await _add_rows(kb_factory, [_seed_row(TENANT_OTHER, subject="他租户馈线", created_at=_T(8, 0))])
    assert await svc.queue_summary(TENANT, statuses=("candidate",)) == []  # 租户隔离
    assert (await svc.queue_summary(TENANT_OTHER, statuses=("candidate",)))[0].subject == "他租户馈线"


async def test_group_by不支持值_服务拒绝() -> None:
    with pytest.raises(ValueError, match="3001"):
        await ReviewQueueService(None).queue_summary(TENANT, group_by="predicate")  # type: ignore[arg-type]


# ---------------------------------------------------------------- 全组裁决（batch_decide）


@sqlite_needed
async def test_全组裁决_跨文档聚拢_行内审计_邻组零副作用(
    svc: ReviewQueueService, kb_factory: async_sessionmaker[AsyncSession]
) -> None:
    """同 subject 全组单事务裁决（跨文档）；行内 meta 审计恒写；邻组/他租户零副作用。"""
    await _add_rows(
        kb_factory,
        [
            _seed_row(TENANT, subject="馈线F001", created_at=_T(9, 0), document_id=DOC_A, chunk_id=uuid.uuid4()),
            _seed_row(TENANT, subject="馈线F001", created_at=_T(9, 5), document_id=DOC_B, chunk_id=uuid.uuid4()),
            _seed_row(TENANT, subject="台区T09", created_at=_T(8, 0)),
            _seed_row(TENANT_OTHER, subject="馈线F001", created_at=_T(8, 0)),
        ],
    )
    outcome = await svc.batch_decide(
        TENANT,
        subject="馈线F001",
        decision="rejected",
        reviewer_id=REVIEWER,
        comment="换版确认删除",
        statuses=("candidate",),
    )
    assert outcome.decided == 2 and outcome.trail_recorded == 0  # 全组裁决；无单据服务 → 单据留痕 0
    assert (outcome.subject, outcome.decision) == ("馈线F001", "rejected")
    async with kb_factory() as db:
        rows = (await db.execute(KbFactORM.__table__.select())).mappings().all()
    by_subject: dict[tuple[uuid.UUID, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        by_subject.setdefault((row["tenant_id"], row["subject"], row["status"]), []).append(dict(row))
    rejected = by_subject[(TENANT, "馈线F001", "rejected")]
    assert len(rejected) == 2 and {r["document_id"] for r in rejected} == {DOC_A, DOC_B}  # 跨文档聚拢
    for row in rejected:  # 行内审计恒写（无单也有痕）
        audit = row["meta"]["review_queue"]
        assert audit["decision"] == "rejected" and audit["reviewer_id"] == str(REVIEWER)
        assert audit["comment"] == "换版确认删除" and audit["batch"] is True
        assert datetime.fromisoformat(audit["decided_at"])
    assert by_subject[(TENANT, "台区T09", "candidate")][0]["meta"].get("review_queue") is None  # 邻组零副作用
    assert len(by_subject[(TENANT_OTHER, "馈线F001", "candidate")]) == 1  # 他租户零副作用

    authoritative = await svc.batch_decide(
        TENANT, subject="台区T09", decision="authoritative", reviewer_id=REVIEWER, statuses=("candidate",)
    )
    assert authoritative.decided == 1 and authoritative.decision == "authoritative"


@sqlite_needed
async def test_全组裁决_审核单留痕复用(svc: ReviewQueueService, kb_factory: async_sessionmaker[AsyncSession]) -> None:
    """open 单逐行留痕复用单据服务（payload["decisions"]）；终态单/无单行跳过（行内审计兜底）。"""
    tickets = _FakeTickets()
    svc_with_tickets = ReviewQueueService(kb_factory, tickets=tickets)
    fact_ids = await _add_rows(
        kb_factory,
        [
            _seed_row(TENANT, subject="保护R1", created_at=_T(9, 0)),
            _seed_row(TENANT, subject="保护R1", created_at=_T(9, 5)),
            _seed_row(TENANT, subject="保护R1", created_at=_T(9, 10)),
        ],
    )
    rows = fact_ids["保护R1"]
    tickets.add(rows[0], "pending_review")  # open 单 → 留痕
    tickets.add(rows[1], "approved")  # 终态单 → 跳过（不产生幽灵痕）
    # rows[2] 无单 → 跳过（行内审计兜底）

    outcome = await svc_with_tickets.batch_decide(
        TENANT,
        subject="保护R1",
        decision="authoritative",
        reviewer_id=REVIEWER,
        comment="整组通过",
        statuses=("candidate",),
    )
    assert outcome.decided == 3 and outcome.trail_recorded == 1  # 仅 open 单行落单据留痕
    assert len(tickets.merged_payloads) == 1
    trail = tickets.merged_payloads[0]["decisions"][0]
    assert trail["action"] == "accept" and trail["batch"] is True
    assert trail["candidate_id"] == str(rows[0]) and trail["decided_by"] == str(REVIEWER)
    assert trail["comment"] == "整组通过" and datetime.fromisoformat(trail["decided_at"])
    assert tickets._tickets[rows[0]]["payload"]["decisions"][0]["action"] == "accept"  # 桩内持久化

    async with kb_factory() as db:  # 三行全部翻转 + 行内审计恒写（含无单/终态单行）
        stored = (await db.execute(KbFactORM.__table__.select())).mappings().all()
    assert all(row["status"] == "authoritative" and row["meta"]["review_queue"]["batch"] for row in stored)


@sqlite_needed
async def test_缺省needs_review词汇_v1队列零命中(svc, kb_factory) -> None:
    """缺省 statuses=("needs_review",)（§8.1 b 类目标态）对现行 candidate 队列零命中 → decided=0 零副作用。"""
    await _add_rows(kb_factory, [_seed_row(TENANT, subject="馈线F001", created_at=_T(9, 0))])
    outcome = await svc.batch_decide(TENANT, subject="馈线F001", decision="rejected", reviewer_id=REVIEWER)
    assert outcome.decided == 0
    async with kb_factory() as db:
        assert (await db.execute(KbFactORM.__table__.select())).mappings().all()[0]["status"] == "candidate"


@sqlite_needed
async def test_空组_幂等零裁决(svc: ReviewQueueService, kb_factory: async_sessionmaker[AsyncSession]) -> None:
    """空组（subject 不在队列）→ decided=0 幂等返回（行数即真值，不 404）。"""
    await _add_rows(kb_factory, [_seed_row(TENANT, subject="馈线F001", created_at=_T(9, 0))])
    outcome = await svc.batch_decide(
        TENANT, subject="不存在的subject", decision="rejected", reviewer_id=REVIEWER, statuses=("candidate",)
    )
    assert (outcome.decided, outcome.trail_recorded) == (0, 0)


async def test_非法decision_服务3001防御() -> None:
    with pytest.raises(ValueError, match="3001"):
        await ReviewQueueService(None).batch_decide(  # type: ignore[arg-type]
            TENANT, subject="x", decision="accept", reviewer_id=REVIEWER
        )


# ---------------------------------------------------------------- DTO 契约（不触库，恒跑）


def test_ReviewQueueBatchDecideIn_DTO契约() -> None:
    """decision 越枚举 → ValidationError（FastAPI 面即 422）；extra=forbid；comment 可选。"""
    with pytest.raises(ValidationError):
        ReviewQueueBatchDecideIn.model_validate({"subject": "馈线F001", "decision": "accept"})
    with pytest.raises(ValidationError):
        ReviewQueueBatchDecideIn.model_validate({"subject": "馈线F001", "decision": "rejected", "unexpected": 1})
    with pytest.raises(ValidationError):
        ReviewQueueBatchDecideIn.model_validate({"decision": "rejected"})  # 缺 subject
    ok = ReviewQueueBatchDecideIn(subject="馈线F001", decision="authoritative")
    assert ok.comment is None and ok.decision == "authoritative"
    ok2 = ReviewQueueBatchDecideIn.model_validate({"subject": "馈线F001", "decision": "rejected", "comment": "作废"})
    assert ok2.comment == "作废"


# ---------------------------------------------------------------- 路由直调（端点直调=tests/kb/test_review_api 同款）


@sqlite_needed
async def test_路由GET_聚合信封_字段投影与空队列(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    """GET /kb/review-queue：服务组 → DTO 投影（含样本引用）；空队列 groups=[] total=0。"""
    bridged = _CandidateQueue(kb_factory)
    await _add_rows(
        kb_factory,
        [
            _seed_row(TENANT, subject="馈线F001", created_at=_T(9, 0), chunk_id=uuid.uuid4()),
            _seed_row(TENANT, subject="馈线F001", created_at=_T(9, 5), chunk_id=uuid.uuid4()),
            _seed_row(TENANT, subject="台区T09", created_at=_T(8, 0)),
        ],
    )
    out = await review_queue_summary(_principal(), _request(_StubApp(bridged)))
    assert isinstance(out, ReviewQueueSummaryOut)
    assert [(g.subject, g.count) for g in out.groups] == [("台区T09", 1), ("馈线F001", 2)]
    assert out.total == 3  # 各组 count 之和
    feeder = next(g for g in out.groups if g.subject == "馈线F001")
    assert feeder.oldest_created_at == _T(9, 0) and len(feeder.samples) == 2
    assert feeder.samples[0].chunk_id is not None and feeder.samples[0].document_id == DOC_A

    empty = await review_queue_summary(_principal(), _request(_StubApp(ReviewQueueService(kb_factory))))
    # 缺省词汇（needs_review）+ v1 candidate 数据 → 空队列合法态
    assert empty.groups == [] and empty.total == 0


@sqlite_needed
async def test_路由POST_全组裁决_返回裁决行数(kb_factory: async_sessionmaker[AsyncSession]) -> None:
    """POST /kb/review-queue/batch-decide：body 三字段 + 返回裁决行数（信封出参契约）。"""
    bridged = _CandidateQueue(kb_factory)
    await _add_rows(
        kb_factory,
        [
            _seed_row(TENANT, subject="馈线F001", created_at=_T(9, 0), document_id=DOC_A),
            _seed_row(TENANT, subject="馈线F001", created_at=_T(9, 5), document_id=DOC_B),
        ],
    )
    body = ReviewQueueBatchDecideIn(subject="馈线F001", decision="rejected", comment="换版整组作废")
    out = await review_queue_batch_decide(body, _principal(), _request(_StubApp(bridged)))
    assert isinstance(out, ReviewQueueBatchDecideOut)
    assert (out.subject, out.decision, out.decided) == ("馈线F001", "rejected", 2)
    async with kb_factory() as db:
        rows = (await db.execute(KbFactORM.__table__.select())).mappings().all()
    assert all(row["status"] == "rejected" for row in rows)  # 全组落库翻转

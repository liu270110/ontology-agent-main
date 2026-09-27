"""kb 终审工作台三端点集成测试（api/01 §5.4 ★；端点直调=tests/review/test_admin_api 同款）。

覆盖：
- GET  /kb/documents/{id}/review/candidates：过滤（fact_type/status/confidence 下界）+ 分页
  （offset/limit，meta.total）+ quote/violations/align 全量透出（B2 深化产物到达前端的正式通道）；
- POST /kb/review/candidates/{cid}/decision：accept→authoritative（全仓唯一 authoritative 写路径，
  OntRAG §2.7 宪法第 3 条关口）、reject→rejected、edit_accept 修订后入审（rejected 复活）、
  非 candidate 态 409、终态单迟到决策 4701、决策留痕（payload["decisions"]）、无单容忍；
- POST /kb/documents/{id}/review/batch-decision：逐条独立执行 + meta 汇总 + 越限 3001；
- DTO 契约（extra=forbid / edit_accept 必附编辑载荷 / accept 拒带载荷）与 review scope 门禁。

环境纪律：直连本地 PG（不可达即跳过，同 tests/kb 夹具纪律）；审核单走真实 ReviewTicketService。
psycopg 异步要求 Selector 事件循环（Windows 默认 Proactor 不可用）——导入期固定策略。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request as StarletteRequest

from services.gateway.app import create_app
from services.iam.data.orm import Tenant as TenantORM
from services.iam.data.orm import User as UserORM
from services.kb.api.kb import batch_decide_candidates, decide_candidate, list_review_candidates
from services.kb.api.schemas.kb import (
    BatchCandidateDecisionIn,
    BatchDecisionIn,
    CandidateDecisionIn,
    CandidateEditIn,
)
from services.kb.data.orm import Document as DocumentORM
from services.kb.data.orm import KbCollection as KbCollectionORM
from services.kb.data.orm import KbFact as KbFactORM
from services.platform.config import Settings
from services.platform.deps import Principal, require_scope
from services.platform.errors import GatewayError
from services.review.business.candidates import ReviewTicketService
from services.review.data.orm import ReviewTicket as ReviewTicketORM

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------- 夹具（租户/文档 + 六条候选事实 + 审核单）


def _fact_row(
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    fact_type: str,
    subject: str,
    status: str,
    confidence: float,
    quote: str | None = None,
    violations: list[dict[str, Any]] | None = None,
    align: dict[str, Any] | None = None,
    predicate: str | None = None,
    obj: str | None = None,
) -> dict[str, Any]:
    """kb_facts 行值（evidence 信封/violations/meta["align"] 按 B2 写入口径构造）。"""
    return {
        "tenant_id": tenant_id,
        "document_id": document_id,
        "fact_type": fact_type,
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "subject_type": "http://ontology-agent.local/o/t1/power#Feeder" if fact_type == "entity" else None,
        "object_type": None,
        "canonical_name": subject,
        "aliases": ["http://ontology-agent.local/o/t1/power#Feeder"] if align else [],
        "confidence": confidence,
        "status": status,
        "evidence": {
            "source_ref": {"document_id": str(document_id), "doc_version": 1, "chunk_id": None, "span": [0, 16]},
            "quote": quote,
            "span": [0, len(quote)] if quote else None,
        },
        "violations": violations or [],
        "meta": {"align": align} if align else {},
    }


@pytest.fixture
async def kb_pg() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect() as conn:
            await conn.execute(text("SELECT 1 FROM kb_facts LIMIT 1"))
            await conn.execute(text("SELECT 1 FROM review_tickets LIMIT 1"))
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达或 kb_facts/review_tickets 未迁移，跳过终审工作台集成用例")
    await probe.dispose()
    engine = create_async_engine(settings.pg_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def wb_env(kb_pg: async_sessionmaker[AsyncSession]) -> AsyncIterator[dict]:
    """终审工作台装配：独立租户/集合/文档 + 六条候选事实（覆盖三态）+ 部分 open 审核单。"""
    tickets = ReviewTicketService(kb_pg)
    approver_id = uuid.uuid4()  # 终审人（users 真实行：review_tickets.reviewer_id FK 约束）
    async with kb_pg() as db, db.begin():
        tenant = TenantORM(name="wb-it-租户", slug=f"wb-it-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        db.add(
            UserORM(
                id=approver_id,
                tenant_id=tenant.id,
                email=f"{uuid.uuid4().hex[:10]}@wb-it.local",
                password_hash="it",
            )
        )
        collection = KbCollectionORM(tenant_id=tenant.id, name="wb-it-库", embedding_model="bge-m3")
        db.add(collection)
        await db.flush()
        doc = DocumentORM(
            tenant_id=tenant.id,
            kb_collection_id=collection.id,
            title="终审工作台联调",
            source_type="upload",
            size_bytes=128,
            minio_key=f"raw-docs/{tenant.id}/{collection.id}/{uuid.uuid4()}/source.md",
            checksum_sha256="0" * 64,
            meta={},
            status="pending_review",
        )
        db.add(doc)
    seed_rows = {
        # candidate 带 quote + align（工作台裁决依据全量透出的断言锚点）
        "feed": _fact_row(
            tenant.id,
            doc.id,
            fact_type="entity",
            subject="馈线F001",
            status="candidate",
            confidence=0.9,
            quote="馈线 F001 由 城东变电站 供电",
            align={"tier": 1, "status": "aligned", "rule": "exact"},
        ),
        # relation 候选（fact_type 过滤断言锚点），无审核单（无单容忍路径）
        "rel": _fact_row(
            tenant.id,
            doc.id,
            fact_type="relation",
            subject="馈线F001",
            status="candidate",
            confidence=0.5,
            predicate="suppliedBy",
            obj="城东变电站",
        ),
        # 低置信候选（confidence 下界过滤锚点）
        "low": _fact_row(tenant.id, doc.id, fact_type="entity", subject="台区T09", status="candidate", confidence=0.2),
        # rejected 带违例（violations 透出断言锚点 + edit_accept 复活锚点）
        "rej": _fact_row(
            tenant.id,
            doc.id,
            fact_type="entity",
            subject="凭空馈线",
            status="rejected",
            confidence=0.8,
            quote="原文中不存在的引语",
            violations=[{"rule": "evidence_not_in_chunk", "detail": "幻觉证据嫌疑"}],
        ),
        # authoritative（非 candidate 态 409 防呆锚点）
        "auth": _fact_row(
            tenant.id, doc.id, fact_type="entity", subject="变电站S1", status="authoritative", confidence=0.95
        ),
        # candidate（迟到决策 4701 锚点：单 approved 而事实未翻转）
        "late": _fact_row(
            tenant.id, doc.id, fact_type="entity", subject="保护动作R1", status="candidate", confidence=0.7
        ),
    }
    facts: dict[str, uuid.UUID] = {}
    async with kb_pg() as db, db.begin():
        for key, row in seed_rows.items():
            instance = KbFactORM(**row)
            db.add(instance)
            await db.flush()
            facts[key] = instance.id
    # 审核单：feed/low/auth/late 各一张 pending_review（rel/rej 无单——容忍票据缺失路径）
    for key in ("feed", "low", "auth", "late"):
        await tickets.submit_candidate(
            tenant_id=tenant.id,
            target_type="knowledge_instance",
            target_id=facts[key],
            payload={"envelope_version": "v1", "candidate_type": "knowledge_instance", "payload": {}, "review": {}},
            status="pending_review",
        )
    env = {
        "settings": Settings(),
        "factory": kb_pg,
        "tickets": tickets,
        "approver_id": approver_id,
        "tenant_id": tenant.id,
        "collection_id": collection.id,
        "document_id": doc.id,
        "facts": facts,
    }
    yield env
    async with kb_pg() as db, db.begin():  # FK 逆序清理
        for stmt in (
            delete(ReviewTicketORM).where(ReviewTicketORM.tenant_id == env["tenant_id"]),
            delete(KbFactORM).where(KbFactORM.tenant_id == env["tenant_id"]),
            delete(DocumentORM).where(DocumentORM.tenant_id == env["tenant_id"]),  # 按租户清（用例可能另建文档）
            delete(KbCollectionORM).where(KbCollectionORM.id == env["collection_id"]),
            delete(UserORM).where(UserORM.tenant_id == env["tenant_id"]),
            delete(TenantORM).where(TenantORM.id == env["tenant_id"]),
        ):
            await db.execute(stmt)


# ---------------------------------------------------------------- 调用辅助（端点直调模式）


def _principal(env: dict, *, user_id: uuid.UUID | None = None, scopes: list[str] | None = None) -> Principal:
    return Principal(
        {
            "sub": str(user_id or uuid.uuid4()),
            "tenant_id": str(env["tenant_id"]),
            "roles": ["reviewer"],
            "scopes": scopes or ["review:read", "review:approve"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


def _request(env: dict) -> StarletteRequest:
    """携带 app 与候选审核单服务装配的最小 Request（不跑 lifespan，tests/review 同款）。"""
    app = create_app(env["settings"])
    app.state.candidate_review = env["tickets"]
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
    request.state.trace_id = "kb-review-it-trace"
    return request


async def _fact_status(env: dict, fact_id: uuid.UUID) -> str:
    async with env["factory"]() as db:
        return (await db.get(KbFactORM, fact_id)).status


# ---------------------------------------------------------------- 列表端点


async def test_GET_candidates_过滤_分页_quote_violations_透出(wb_env, kb_pg):
    env = wb_env
    principal = _principal(env)
    async with kb_pg() as db:
        page = await list_review_candidates(env["document_id"], principal, db)
        # 缺省=全部三态（4 candidate + 1 rejected + 1 authoritative）
        assert page.meta.total == 6 and len(page.items) == 6
        assert page.meta.offset == 0 and page.meta.limit == 50

        # quote/violations/align 全量透出（B2 深化产物 = 工作台裁决依据）
        by_key = {(i.fact_type, i.subject): i for i in page.items}
        feed = by_key[("entity", "馈线F001")]
        assert feed.evidence.quote == "馈线 F001 由 城东变电站 供电"
        assert feed.evidence.span == [0, len("馈线 F001 由 城东变电站 供电")]
        assert feed.evidence.source_ref["document_id"] == str(env["document_id"])
        assert feed.align is not None and feed.align["tier"] == 1 and feed.align["status"] == "aligned"
        assert feed.aliases == ["http://ontology-agent.local/o/t1/power#Feeder"]
        assert feed.confidence == pytest.approx(0.9)
        assert isinstance(feed.created_at, datetime)
        rel = by_key[("relation", "馈线F001")]
        assert rel.predicate == "suppliedBy" and rel.object == "城东变电站"
        assert rel.evidence.quote is None  # 无引语候选照常透出（evidence 信封不缺位）
        rej = by_key[("entity", "凭空馈线")]
        assert rej.violations and rej.violations[0]["rule"] == "evidence_not_in_chunk"
        assert rej.status == "rejected"

        # fact_type / status / confidence 下界过滤（meta.total 同步）
        rel_only = await list_review_candidates(env["document_id"], principal, db, fact_type="relation")
        assert rel_only.meta.total == 1 and rel_only.items[0].predicate == "suppliedBy"
        cand = await list_review_candidates(env["document_id"], principal, db, status_filter="candidate")
        assert cand.meta.total == 4 and all(i.status == "candidate" for i in cand.items)
        high = await list_review_candidates(env["document_id"], principal, db, min_confidence=0.85)
        assert {i.subject for i in high.items} == {"馈线F001", "变电站S1"}

        # 分页：offset 推进不重不漏，total 恒定
        p1 = await list_review_candidates(env["document_id"], principal, db, offset=0, limit=2)
        p2 = await list_review_candidates(env["document_id"], principal, db, offset=2, limit=2)
        assert p1.meta.total == 6 and p2.meta.total == 6
        assert len(p1.items) == 2 and len(p2.items) == 2
        assert not {i.id for i in p1.items} & {i.id for i in p2.items}


async def test_GET_candidates_文档不存在_404(wb_env, kb_pg):
    env = wb_env
    async with kb_pg() as db:
        with pytest.raises(GatewayError) as exc:
            await list_review_candidates(uuid.uuid4(), _principal(env), db)
    assert exc.value.status_code == 404


async def test_review_scope门禁_2001拒绝(wb_env):
    # 端点直调绕过 Depends 解析，直测依赖工厂（tests/review 同款）：deny-by-default
    principal = _principal(wb_env, scopes=["kb:read"])  # kb scope 不等于 review scope
    with pytest.raises(GatewayError) as exc:
        require_scope("review:read")(principal)
    assert exc.value.code == 2001 and exc.value.status_code == 403
    with pytest.raises(GatewayError):
        require_scope("review:approve")(principal)


# ---------------------------------------------------------------- 单条决策端点


async def test_decision_accept_authoritative_留痕(wb_env, kb_pg):
    """accept → authoritative（全仓唯一 authoritative 写路径；宪法第 3 条人工终审关口）。"""
    env = wb_env
    principal = _principal(env)
    async with kb_pg() as db:  # 直调模式：测试持有会话生命周期，显式提交
        out = await decide_candidate(
            env["facts"]["feed"], CandidateDecisionIn(action="accept"), principal, _request(env), db
        )
        await db.commit()
    assert out.status == "authoritative" and out.trail_recorded is True
    assert await _fact_status(env, env["facts"]["feed"]) == "authoritative"
    # 决策留痕可溯（宪法 5）：决策人/时间/action/candidate_id 随 open 单落 payload
    ticket = await env["tickets"].get_open_ticket(
        tenant_id=env["tenant_id"], target_type="knowledge_instance", target_id=env["facts"]["feed"]
    )
    assert ticket is not None
    trail = ticket["payload"]["decisions"]
    assert trail[0]["action"] == "accept"
    assert trail[0]["candidate_id"] == str(env["facts"]["feed"])
    assert trail[0]["decided_by"] == principal.user_id.__str__()
    assert datetime.fromisoformat(trail[0]["decided_at"]) is not None


async def test_decision_reject_rejected(wb_env, kb_pg):
    env = wb_env
    principal = _principal(env)
    async with kb_pg() as db:
        out = await decide_candidate(
            env["facts"]["low"], CandidateDecisionIn(action="reject"), principal, _request(env), db
        )
        await db.commit()
    assert out.status == "rejected"
    assert await _fact_status(env, env["facts"]["low"]) == "rejected"


async def test_decision_edit_accept_修订后入审_无单容忍(wb_env, kb_pg):
    """edit_accept：按编辑载荷修订（未提供字段不覆盖）后保持 candidate；无审核单 → 留痕容忍缺失。"""
    env = wb_env
    principal = _principal(env)
    edit = CandidateEditIn(predicate="suppliedBy2", object="城西变电站")  # subject/canonical_name 不覆盖
    async with kb_pg() as db:
        out = await decide_candidate(
            env["facts"]["rel"], CandidateDecisionIn(action="edit_accept", edit=edit), principal, _request(env), db
        )
        await db.commit()
    assert out.status == "candidate" and out.trail_recorded is False  # rel 无单（容忍票据缺失）
    async with kb_pg() as db:
        fact = await db.get(KbFactORM, env["facts"]["rel"])
        assert fact.predicate == "suppliedBy2" and fact.object == "城西变电站"
        assert fact.subject == "馈线F001" and fact.canonical_name == "馈线F001"  # 未提供字段不覆盖


async def test_decision_edit_accept_rejected复活(wb_env, kb_pg):
    """rejected 候选经修订复位 candidate 重新进审（契约原文「修订后入审」）。"""
    env = wb_env
    principal = _principal(env)
    async with kb_pg() as db:
        out = await decide_candidate(
            env["facts"]["rej"],
            CandidateDecisionIn(action="edit_accept", edit=CandidateEditIn(subject="馈线F002")),
            principal,
            _request(env),
            db,
        )
        await db.commit()
    assert out.status == "candidate"
    async with kb_pg() as db:
        fact = await db.get(KbFactORM, env["facts"]["rej"])
        assert fact.subject == "馈线F002" and fact.status == "candidate"


async def test_decision_非candidate态_409(wb_env, kb_pg):
    """accept/reject 对 rejected/authoritative 重复决策防呆（且不产生留痕副作用）。"""
    env = wb_env
    principal = _principal(env)
    async with kb_pg() as db:
        with pytest.raises(GatewayError) as exc:
            await decide_candidate(
                env["facts"]["auth"], CandidateDecisionIn(action="accept"), principal, _request(env), db
            )
        assert exc.value.status_code == 409
        with pytest.raises(GatewayError) as exc:
            await decide_candidate(
                env["facts"]["rej"], CandidateDecisionIn(action="reject"), principal, _request(env), db
            )
        assert exc.value.status_code == 409
        # authoritative 亦不接受修订（终审生效态不被工作台静默降级）
        with pytest.raises(GatewayError) as exc:
            await decide_candidate(
                env["facts"]["auth"],
                CandidateDecisionIn(action="edit_accept", edit=CandidateEditIn(subject="X")),
                principal,
                _request(env),
                db,
            )
        assert exc.value.status_code == 409
    assert await _fact_status(env, env["facts"]["auth"]) == "authoritative"


async def test_decision_迟到决策_4701(wb_env, kb_pg):
    """单已 approved/published 后的迟到决策 → 4701（review 段，HTTP 409）；事实状态不被翻转。"""
    env = wb_env
    principal = _principal(env, user_id=env["approver_id"])  # 真实终审人（reviewer_id FK）
    ticket = await env["tickets"].get_open_ticket(
        tenant_id=env["tenant_id"], target_type="knowledge_instance", target_id=env["facts"]["late"]
    )
    await env["tickets"].approve(
        tenant_id=env["tenant_id"], ticket_id=ticket["id"], reviewer_id=principal.user_id, decision_note="队列侧已批"
    )
    async with kb_pg() as db:
        with pytest.raises(GatewayError) as exc:
            await decide_candidate(
                env["facts"]["late"], CandidateDecisionIn(action="accept"), principal, _request(env), db
            )
    assert exc.value.code == 4701 and exc.value.status_code == 409
    assert await _fact_status(env, env["facts"]["late"]) == "candidate"  # 迟到决策零副作用


async def test_decision_事实不存在_404(wb_env, kb_pg):
    env = wb_env
    async with kb_pg() as db:
        with pytest.raises(GatewayError) as exc:
            await decide_candidate(
                uuid.uuid4(), CandidateDecisionIn(action="accept"), _principal(env), _request(env), db
            )
    assert exc.value.status_code == 404


async def test_decision_跨租户候选_404(wb_env, kb_pg):
    """租户过滤：他人租户的候选按不存在处理（deny-by-default）。"""
    env = wb_env
    other_tenant = dict(env)
    other_tenant["tenant_id"] = uuid.uuid4()
    async with kb_pg() as db:
        with pytest.raises(GatewayError) as exc:
            await decide_candidate(
                env["facts"]["feed"], CandidateDecisionIn(action="accept"), _principal(other_tenant), _request(env), db
            )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------- 批量决策端点


async def test_batch_逐条独立执行_meta汇总(wb_env, kb_pg):
    env = wb_env
    principal = _principal(env)
    unknown = uuid.uuid4()
    body = BatchDecisionIn(
        decisions=[
            BatchCandidateDecisionIn(candidate_id=env["facts"]["feed"], action="accept"),
            BatchCandidateDecisionIn(candidate_id=env["facts"]["low"], action="reject"),
            BatchCandidateDecisionIn(
                candidate_id=env["facts"]["rej"],
                action="edit_accept",
                edit=CandidateEditIn(subject="馈线F003"),
            ),
            BatchCandidateDecisionIn(candidate_id=env["facts"]["auth"], action="accept"),  # 409 防呆
            BatchCandidateDecisionIn(candidate_id=unknown, action="accept"),  # 404
        ]
    )
    async with kb_pg() as db:
        out = await batch_decide_candidates(env["document_id"], body, principal, _request(env), db)
        await db.commit()
    assert [r.ok for r in out.results] == [True, True, True, False, False]
    assert out.results[0].status == "authoritative"
    assert out.results[2].status == "candidate"
    assert "409" in out.results[3].error and "404" in out.results[4].error
    # meta 汇总（部分成功语义；edited=edit_accept 成功数）
    assert out.meta.accepted == 1 and out.meta.rejected == 1 and out.meta.edited == 1 and out.meta.failed == 2
    # 落库状态与逐条结果一致
    assert await _fact_status(env, env["facts"]["feed"]) == "authoritative"
    assert await _fact_status(env, env["facts"]["low"]) == "rejected"
    assert await _fact_status(env, env["facts"]["rej"]) == "candidate"
    assert await _fact_status(env, env["facts"]["auth"]) == "authoritative"  # 失败项零副作用
    # 批量留痕随成功项落单（feed/low 各一张 open 单）
    for fact_id, action in ((env["facts"]["feed"], "accept"), (env["facts"]["low"], "reject")):
        ticket = await env["tickets"].get_open_ticket(
            tenant_id=env["tenant_id"], target_type="knowledge_instance", target_id=fact_id
        )
        assert ticket["payload"]["decisions"][0]["action"] == action


async def test_batch_跨文档候选_路径绑定拒绝(wb_env, kb_pg):
    """路径文档绑定（ocr 评审 medium）：他文档同租户候选经本文档批量端点决策 → 逐条失败不越权。"""
    env = wb_env
    principal = _principal(env)
    async with kb_pg() as db, db.begin():  # 另建一文档 + 其候选（同租户同集合）
        alt_doc = DocumentORM(
            tenant_id=env["tenant_id"],
            kb_collection_id=env["collection_id"],
            title="他文档",
            source_type="upload",
            size_bytes=64,
            minio_key=f"raw-docs/{env['tenant_id']}/{env['collection_id']}/{uuid.uuid4()}/source.md",
            checksum_sha256=uuid.uuid4().hex,
            meta={},
            status="pending_review",
        )
        db.add(alt_doc)
        await db.flush()
        other_fact = KbFactORM(
            tenant_id=env["tenant_id"],
            document_id=alt_doc.id,
            fact_type="entity",
            subject="越权候选",
            subject_type="http://ontology-agent.local/o/t1/power#Feeder",
            confidence=0.9,
            evidence={},
            meta={},
        )
        db.add(other_fact)
        await db.flush()
        other_id = other_fact.id
    body = BatchDecisionIn(decisions=[BatchCandidateDecisionIn(candidate_id=other_id, action="accept")])
    async with kb_pg() as db:
        out = await batch_decide_candidates(env["document_id"], body, principal, _request(env), db)
        await db.commit()
    assert out.results[0].ok is False and "404" in (out.results[0].error or "")
    assert out.meta.failed == 1
    assert await _fact_status(env, other_id) == "candidate"  # 零副作用


async def test_batch_越限_3001(wb_env, kb_pg):
    env = wb_env
    body = BatchDecisionIn(
        decisions=[
            BatchCandidateDecisionIn(candidate_id=uuid.uuid4(), action="accept")
            for _ in range(201)  # >200
        ]
    )
    async with kb_pg() as db:
        with pytest.raises(GatewayError) as exc:
            await batch_decide_candidates(env["document_id"], body, _principal(env), _request(env), db)
    assert exc.value.code == 3001 and exc.value.status_code == 422


async def test_batch_文档不存在_404(wb_env, kb_pg):
    env = wb_env
    body = BatchDecisionIn(decisions=[BatchCandidateDecisionIn(candidate_id=uuid.uuid4(), action="accept")])
    async with kb_pg() as db:
        with pytest.raises(GatewayError) as exc:
            await batch_decide_candidates(uuid.uuid4(), body, _principal(env), _request(env), db)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------- DTO 契约（不触 PG，恒跑）


def test_CandidateDecisionIn_DTO契约():
    """extra=forbid；edit_accept 必附编辑载荷；accept/reject 拒带载荷；编辑载荷至少一个字段。"""
    with pytest.raises(ValidationError):
        CandidateDecisionIn.model_validate({"action": "accept", "unexpected": 1})
    with pytest.raises(ValidationError):  # edit_accept 缺编辑载荷（契约：修订后入审）
        CandidateDecisionIn(action="edit_accept")
    with pytest.raises(ValidationError):  # accept 拒带编辑载荷（编辑语义只属 edit_accept）
        CandidateDecisionIn(action="accept", edit=CandidateEditIn(subject="X"))
    with pytest.raises(ValidationError):  # 空编辑载荷（无可修订字段）
        CandidateDecisionIn(action="edit_accept", edit=CandidateEditIn())
    with pytest.raises(ValidationError):  # action 越枚举
        CandidateDecisionIn.model_validate({"action": "publish"})
    # 合法面放行
    ok = CandidateDecisionIn(action="edit_accept", edit=CandidateEditIn(subject="馈线F9", object=None))
    assert ok.edit.provided() == {"subject": "馈线F9"}  # 显式 None 视为不覆盖


def test_BatchDecisionIn_DTO契约():
    with pytest.raises(ValidationError):  # 空批次
        BatchDecisionIn(decisions=[])
    with pytest.raises(ValidationError):  # 条目缺 candidate_id
        BatchDecisionIn.model_validate({"decisions": [{"action": "accept"}]})
    body = BatchDecisionIn.model_validate({"decisions": [{"candidate_id": str(uuid.uuid4()), "action": "reject"}]})
    assert len(body.decisions) == 1

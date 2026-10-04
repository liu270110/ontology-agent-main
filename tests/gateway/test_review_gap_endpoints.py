"""W1 缺口端点补齐批：审批详情 GET /admin/reviews/{id} + 批量审批 POST /admin/reviews/batch。

契约源=frontend/src/features/approvals/api.ts（getReview→normalizeReview / batchReviews
{ids,action,note}）+ ApprovalDetailModal 富形状；IX-APR-01/02 预登记追认（api/01 §5.8）。
装配样板=tests/review/test_admin_api（端点直调 Request 脚手架 + app.state.review_approvals
手工装配，不跑 lifespan；PG/review_tickets 不可达自动 skip）。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import dataclass
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
from services.platform.config import Settings
from services.platform.deps import Principal
from services.platform.errors import GatewayError
from services.review.api.admin import batch_review, get_review_detail
from services.review.api.schemas.admin import BatchDecisionIn
from services.review.business.candidates import ReviewApprovalService, ReviewTicketService
from services.review.data.governance import PgGovernanceTierReader

if sys.platform == "win32":
    # psycopg async 仅支持 selector 事件循环（Windows 默认 Proactor 不兼容）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pytestmark = pytest.mark.integration


@dataclass
class _GapSeed:
    """一次用例的最小装配（字段名对齐 tests/review ReviewSeed，脚手架直接复用口径）。"""

    settings: Settings
    factory: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    submitter_id: uuid.UUID
    approver_a: uuid.UUID
    tickets: ReviewTicketService
    approvals: ReviewApprovalService


def _principal(seed: _GapSeed) -> Principal:
    """审批人 A 主体（sub=approver_a，team 档非自批；review 双 scope）。"""
    return Principal(
        {
            "sub": str(seed.approver_a),
            "tenant_id": str(seed.tenant_id),
            "roles": ["admin"],
            "scopes": ["review:read", "review:approve"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


def _request(seed: _GapSeed) -> StarletteRequest:
    """携带 app 与审批装配单例的最小 Request（端点直调模式，不跑 lifespan）。"""
    app = create_app(seed.settings)
    app.state.review_approvals = seed.approvals
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/admin/reviews",
        "raw_path": b"/api/v1/admin/reviews",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
    }
    request = StarletteRequest(scope)
    request.state.trace_id = "review-gap-it-trace"
    return request


async def _submit(seed: _GapSeed, *, target_type: str = "plugin_listing") -> uuid.UUID:
    return await seed.tickets.submit_candidate(
        tenant_id=seed.tenant_id,
        target_type=target_type,
        target_id=uuid.uuid4(),
        payload={"envelope_version": 1, "candidate_type": target_type},
        submitter_id=seed.submitter_id,
    )


@pytest.fixture
async def review_gap() -> Any:
    """PG 全装配（team 档=禁自批一签终审）；不可达或 review_tickets 未迁移即跳过。"""
    settings = Settings()
    probe = create_async_engine(settings.pg_dsn, pool_pre_ping=True)
    try:
        async with probe.connect() as conn:
            await conn.execute(text("SELECT 1 FROM review_tickets LIMIT 1"))
    except (OSError, SQLAlchemyError):
        await probe.dispose()
        pytest.skip("本地 PG 不可达或 review_tickets 未迁移，跳过 review 缺口用例")
    await probe.dispose()

    engine = create_async_engine(settings.pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, submitter, approver = (uuid.uuid4() for _ in range(3))
    async with factory() as db, db.begin():
        db.add(
            TenantORM(
                id=tenant_id,
                name="review-gap-租户",
                slug=f"review-gap-{uuid.uuid4().hex[:12]}",
                settings={"governance_tier": "team"},
            )
        )
        for uid in (submitter, approver):
            db.add(
                UserORM(
                    id=uid, tenant_id=tenant_id, email=f"{uuid.uuid4().hex[:10]}@review-gap.local", password_hash="it"
                )
            )
    tickets = ReviewTicketService(factory)
    approvals = ReviewApprovalService(tickets, PgGovernanceTierReader(factory))
    seed = _GapSeed(
        settings=settings,
        factory=factory,
        tenant_id=tenant_id,
        submitter_id=submitter,
        approver_a=approver,
        tickets=tickets,
        approvals=approvals,
    )
    yield seed
    async with factory() as db, db.begin():
        from services.review.data.orm import ReviewTicket as ReviewTicketORM

        await db.execute(delete(ReviewTicketORM).where(ReviewTicketORM.tenant_id == tenant_id))
        await db.execute(delete(UserORM).where(UserORM.tenant_id == tenant_id))
        await db.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": str(tenant_id)})
    await engine.dispose()


async def test_审批详情_200_payload与chain派生_404(review_gap):  # noqa: ANN001
    # Arrange：plugin_listing 待审单（payload 含业务信封）；详情端点读全量富字段
    seed = review_gap
    ticket_id = await _submit(seed)
    principal = _principal(seed)
    async with seed.factory() as db:
        # Act / Assert：200 基础字段 + payload 原样透传 + 待审单 chain 末步 current
        detail = await get_review_detail(ticket_id, principal, db)
        assert detail.id == ticket_id
        assert (detail.target_type, detail.status) == ("plugin_listing", "pending_review")
        assert detail.payload["candidate_type"] == "plugin_listing"
        assert detail.chain and detail.chain[-1]["state"] == "current"
        # Assert：审批后 chain 出现 done 步、payload.approvals 留痕随 payload 全量透出
        from services.review.api.admin import decide_review
        from services.review.api.schemas.admin import DecisionIn

        await decide_review(ticket_id, DecisionIn(action="approve", note="合规通过"), principal, _request(seed))
        approved = await get_review_detail(ticket_id, principal, db)
        assert approved.status == "approved"
        assert approved.chain[0]["state"] == "done" and approved.chain[0]["note"] == "合规通过"
        assert approved.chain[-1]["actor"] == str(seed.approver_a)  # 终审步已被决策步取代（无 current）
        assert approved.payload["approvals"][0]["approver_id"] == str(seed.approver_a)
    # Assert：404（他租户/不存在单同口径）
    async with seed.factory() as db:
        with pytest.raises(GatewayError) as ei:
            await get_review_detail(uuid.uuid4(), principal, db)
        assert ei.value.status_code == 404


async def test_批量审批_一成一败_逐单独立_高危拒批_空ids_422(review_gap):  # noqa: ANN001
    # Arrange：两单 plugin_listing（一批）+ 一单 ontology_candidate（高危面）+ 一条不存在 id
    seed = review_gap
    t_ok = await _submit(seed)
    t_reject = await _submit(seed)
    t_high = await _submit(seed, target_type="ontology_candidate")
    ghost = uuid.uuid4()
    request = _request(seed)
    principal = _principal(seed)
    async with seed.factory() as db:
        # Act ①：批量 approve（一成一败：t_ok 成、ghost 败）
        out = await batch_review(
            BatchDecisionIn(ids=[t_ok, ghost], action="approve", note="批量通过"), principal, request, db
        )
        # Assert ①：succeeded/failed 冻结口径 + updated/ids 兼容镜像（前端 batchReviews 类型面）
        assert out.succeeded == [t_ok]
        assert [(f.id, "审核单不存在" in f.reason) for f in out.failed] == [(ghost, True)]
        assert (out.updated, out.ids) == (1, [t_ok])
    async with seed.factory() as db:
        # Act ②：批量 reject 附理由（t_reject 成）+ 高危单同批（t_high 拒批不进决策）
        out2 = await batch_review(
            BatchDecisionIn(ids=[t_reject, t_high], action="reject", note="证据不足"), principal, request, db
        )
        # Assert ②：高危单逐单落 failed（IX-APR-02 服务端同拒），不阻断 t_reject
        assert out2.succeeded == [t_reject]
        assert out2.failed[0].id == t_high and "高危" in out2.failed[0].reason
    async with seed.factory() as db:
        from sqlalchemy import select

        from services.review.data.orm import ReviewTicket as ReviewTicketORM

        rows = (
            (await db.execute(select(ReviewTicketORM).where(ReviewTicketORM.tenant_id == seed.tenant_id)))
            .scalars()
            .all()
        )
        by_id = {r.id: r for r in rows}
        # Assert ③：落库终态对账——approve/reject 各自生效、高危单保持待审未动
        assert by_id[t_ok].status == "approved" and by_id[t_reject].status == "rejected"
        assert by_id[t_reject].decision_note == "证据不足" and by_id[t_reject].reviewer_id == seed.approver_a
        assert by_id[t_high].status == "pending_review"

    # Assert ④：空 ids 422（DTO min_length=1，FastAPI 校验层同源）
    with pytest.raises(ValidationError):
        BatchDecisionIn(ids=[], action="approve")
    # Assert ⑤：批量驳回空理由 422（与单条 decision「驳回必附理由」同规，08 §4 REJ 回边）
    with pytest.raises(ValidationError):
        BatchDecisionIn(ids=[t_high], action="reject", note="")

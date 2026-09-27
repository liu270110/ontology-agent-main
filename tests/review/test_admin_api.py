"""admin 审核工单 REST 面集成测试（api/01 §5.8 ★ 两端点直调=tests/plugin 同款；PG 不可达自动跳过）。

覆盖：决策全链（team 档一签 approved 落库）、禁自批 4702（HTTP 403）、未授权 2001、
单不存在 404、列表缺省待审队列 + target_type/status 过滤 + 分页、DTO extra=forbid 与驳回必附理由。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError
from starlette.requests import Request as StarletteRequest

from services.gateway.app import create_app
from services.platform.deps import Principal, require_scope
from services.platform.errors import GatewayError
from services.review.api.admin import decide_review, list_reviews
from services.review.api.schemas.admin import DecisionIn

if TYPE_CHECKING:
    from tests.review.conftest import ReviewSeed

pytestmark = pytest.mark.integration


def _principal(seed: ReviewSeed, *, user_id: uuid.UUID | None = None, scopes: list[str] | None = None) -> Principal:
    return Principal(
        {
            "sub": str(user_id or seed.approver_a),
            "tenant_id": str(seed.tenant_id),
            "roles": ["admin"],
            "scopes": scopes or ["review:read", "review:approve"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


def _request(seed: ReviewSeed) -> StarletteRequest:
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
    request.state.trace_id = "review-admin-it-trace"
    return request


async def _submit(seed: ReviewSeed, *, target_type: str = "plugin_listing") -> uuid.UUID:
    return await seed.tickets.submit_candidate(
        tenant_id=seed.tenant_id,
        target_type=target_type,
        target_id=uuid.uuid4(),
        payload={"envelope_version": 1, "candidate_type": target_type},
        submitter_id=seed.submitter_id,
    )


async def test_POST_admin_reviews_decision_team档_审批全链_一签approved(review_seed):
    # Arrange：team 档（禁自批）；提交人提单，审批人 A 走 REST 面裁决
    seed = review_seed
    await seed.set_tier("team")
    ticket_id = await _submit(seed)
    request = _request(seed)
    principal = _principal(seed)  # sub=审批人 A
    # Act（决策端点自带短事务，无需请求会话）
    out = await decide_review(ticket_id, DecisionIn(action="approve", note="合规通过"), principal, request)
    # Assert：单审批人档一签集齐 → approved（08 §4 approved 不等于生效）
    assert out.ticket_id == ticket_id
    assert out.status == "approved" and out.complete and out.governance_tier == "team"
    assert out.signatures_required == 1 and out.signatures_collected == 1
    ticket = await seed.tickets.get_ticket(tenant_id=seed.tenant_id, ticket_id=ticket_id)
    assert ticket is not None and ticket["status"] == "approved"
    # 审批留痕可溯（宪法 5）：决策人=审批人 A，附理由
    trail = ticket["payload"]["approvals"]
    assert trail[0]["approver_id"] == str(seed.approver_a) and trail[0]["note"] == "合规通过"


async def test_POST_admin_reviews_decision_自批_4702_403(review_seed):
    # Arrange：提交人本人调裁决端点（team 档禁自批，08 §2.4）
    seed = review_seed
    await seed.set_tier("team")
    ticket_id = await _submit(seed)
    request = _request(seed)
    principal = _principal(seed, user_id=seed.submitter_id)
    # Act / Assert
    with pytest.raises(GatewayError) as exc:
        await decide_review(ticket_id, DecisionIn(action="approve"), principal, request)
    assert exc.value.code == 4702 and exc.value.status_code == 403


async def test_POST_admin_reviews_decision_单不存在_404(review_seed):
    # Arrange
    seed = review_seed
    request = _request(seed)
    principal = _principal(seed)
    # Act / Assert
    with pytest.raises(GatewayError) as exc:
        await decide_review(uuid.uuid4(), DecisionIn(action="reject", note="无此单"), principal, request)
    assert exc.value.status_code == 404


async def test_admin_reviews_scope门禁依赖_2001拒绝(review_seed):
    # Arrange：无 review scope 的主体（端点直调绕过 Depends 解析，故直测依赖工厂——plugin 同款）
    seed = review_seed
    principal = _principal(seed, scopes=["plugin:read"])
    # Act / Assert：deny-by-default（08 §2.5 PDP 第 3 步）
    with pytest.raises(GatewayError) as exc:
        require_scope("review:approve")(principal)
    assert exc.value.code == 2001 and exc.value.status_code == 403
    with pytest.raises(GatewayError) as exc:
        require_scope("review:read")(principal)
    assert exc.value.code == 2001
    # Assert：持权主体放行（返回同一主体）
    allowed = require_scope("review:approve")(_principal(seed))
    assert allowed.user_id == seed.approver_a


async def test_GET_admin_reviews_缺省待审队列_过滤与分页(review_seed):
    # Arrange：两条 plugin_listing 待审 + 一条 knowledge_instance 待审 + 一条 draft（未提交不入队）
    seed = review_seed
    t1 = await _submit(seed)
    t2 = await _submit(seed)
    t3 = await _submit(seed, target_type="knowledge_instance")
    await seed.tickets.submit_candidate(
        tenant_id=seed.tenant_id,
        target_type="plugin_listing",
        target_id=uuid.uuid4(),
        payload={},
        status="draft",
        submitter_id=seed.submitter_id,
    )
    principal = _principal(seed)
    async with seed.factory() as db:
        # Act：缺省口径=待审队列（pending_review）
        page = await list_reviews(principal, db)
        # Assert：draft 不入队，三条待审全可见
        assert {i.id for i in page.items} == {t1, t2, t3}
        assert page.offset == 0 and page.limit == 20
        # Act / Assert：target_type 过滤
        by_type = await list_reviews(principal, db, target_type="knowledge_instance")
        assert [i.id for i in by_type.items] == [t3]
        # Act / Assert：status 过滤（终态单经显式 status 查看）+ 分页
        await decide_review(t1, DecisionIn(action="approve"), principal, _request(seed))
        approved = await list_reviews(principal, db, status_filter="approved")
        assert [i.id for i in approved.items] == [t1] and approved.items[0].reviewer_id == seed.approver_a
        paged = await list_reviews(principal, db, offset=0, limit=2)
        assert len(paged.items) == 2 and {i.id for i in paged.items} <= {t2, t3}


def test_DecisionIn_extra字段拒绝_驳回必附理由_action枚举():
    """DTO 契约（02 篇 §6）：extra=forbid；驳回必附理由；action 二值枚举（不触 PG，恒跑）。"""
    # Assert：多余字段拒绝
    with pytest.raises(ValidationError):
        DecisionIn.model_validate({"action": "approve", "note": "ok", "unexpected": 1})
    # Assert：驳回空理由拒绝（08 §4 REJ 回边）
    with pytest.raises(ValidationError):
        DecisionIn(action="reject", note="")
    # Assert：action 越枚举拒绝
    with pytest.raises(ValidationError):
        DecisionIn.model_validate({"action": "hack"})
    # Assert：合法面放行
    assert DecisionIn(action="reject", note="证据不足").note == "证据不足"
    assert DecisionIn(action="approve").note == ""

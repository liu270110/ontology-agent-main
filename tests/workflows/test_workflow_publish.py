"""workflows 发布治理分流测试（15 §1.4：发布 solo 直发 head=1/再发布 version=2；
team 档 202+review 工单落库 target_type=workflow_publish；PG 不可达自动跳过）。

治理三档出处：27 篇 §3「solo 档直接发布，team/enterprise 档发布走审批（审批对象类型
workflow_publish，第七类对象候选，X16）」；档位读租户 settings.governance_tier
（08 §2.4 权威存储位，review PgGovernanceTierReader 同源）。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text
from starlette.requests import Request as StarletteRequest

from services.gateway.app import create_app
from services.platform.deps import Principal
from services.platform.errors import GatewayError
from services.review.business.candidates import ReviewTicketService, build_review_approval
from services.workflows.api.schemas.workflow import WorkflowCreateIn, WorkflowPublishIn, WorkflowPublishOut
from services.workflows.api.workflows import create_workflow, publish_workflow
from services.workflows.business.service import WorkflowService
from services.workflows.data.repo_impl.workflow_repo import PgWorkflowRepository
from services.workflows.domain.repo.workflow_repo import WorkflowRepository

if TYPE_CHECKING:
    from tests.workflows.conftest import WorkflowSeed

pytestmark = pytest.mark.integration


def _principal(seed: WorkflowSeed) -> Principal:
    return Principal(
        {
            "sub": str(seed.user_id),
            "tenant_id": str(seed.tenant_id),
            "roles": ["admin"],
            "scopes": ["workflow:read", "workflow:edit", "workflow:publish"],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


def _request(seed: WorkflowSeed, *, approvals: Any = None, review: Any = None) -> StarletteRequest:
    """携带 app 的最小 Request；审批/档位桩按需挂 app.state（发布装配面读取）。"""
    app = create_app(seed.settings)
    if approvals is not None:
        app.state.review_approvals = approvals
    if review is not None:
        app.state.candidate_review = review
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/workflows",
        "raw_path": b"/api/v1/workflows",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
    }
    request = StarletteRequest(scope)
    request.state.trace_id = "wf-pub-trace"
    return request


async def _create_draft(seed: WorkflowSeed, principal: Principal, request: StarletteRequest) -> uuid.UUID:
    async with seed.factory() as db, db.begin():  # 显式提交（跨会话块可见）
        created = await create_workflow(
            WorkflowCreateIn(name=f"pub-{uuid.uuid4().hex[:8]}", description="发布用"), principal, db, request
        )
        return created.id


async def test_发布solo直发_head1_再发布_version2(wf_seed):
    # Arrange：solo 档（种子租户缺省 solo）+ 服务直调（真实 PG 仓储）
    seed = wf_seed
    principal = _principal(seed)
    workflow_id = await _create_draft(seed, principal, _request(seed))
    async with seed.factory() as db, db.begin():
        repo: WorkflowRepository = PgWorkflowRepository(db, seed.tenant_id)
        service = WorkflowService(repo=repo, approvals=_SoloTier())  # type: ignore[arg-type]
        # Act：首次发布（solo 直发）
        outcome1 = await service.publish(workflow_id=workflow_id, note="首版", submitter_id=seed.user_id)
        # Assert：published + v1 + head 固化 + 版本行落库
        assert outcome1.status == "published" and outcome1.governance == "solo"
        assert outcome1.next_version == "v1"
        # Assert：审计行带操作人（全程可追溯——宪法 5；ocr 2026-10-07 归因修复锚点）
        audit = (
            await db.execute(
                text(
                    "SELECT actor_id::text FROM audit_logs"
                    " WHERE tenant_id = :tid AND resource_type = 'workflow' AND action = 'workflows.publish'"
                ),
                {"tid": str(seed.tenant_id)},
            )
        ).scalar_one()
        assert audit == str(seed.user_id)
        workflow = await repo.get(workflow_id)
        assert workflow is not None and workflow.head_version == 1 and workflow.status.value == "published"
        versions = await repo.list_versions(workflow_id)
        assert [v.version for v in versions] == [1]
        assert versions[0].snapshot["nodes"] and versions[0].note == "首版"
        # Act：再发布（同 draft 内容重发即版本顺延——15 §1.4 version=2 锚点）
        outcome2 = await service.publish(workflow_id=workflow_id, note="二版", submitter_id=seed.user_id)
        # Assert：version=2，版本行两行升序，head 顺延
        assert outcome2.status == "published" and outcome2.next_version == "v2"
        workflow2 = await repo.get(workflow_id)
        assert workflow2 is not None and workflow2.head_version == 2
        versions2 = await repo.list_versions(workflow_id)
        assert [v.version for v in versions2] == [1, 2]


async def test_发布team档_202_pending_工单落库workflow_publish(wf_seed):
    # Arrange：租户档位改 team（tenants.settings.governance_tier）+ 真实 review 工单服务
    seed = wf_seed
    await seed.set_governance_tier("team")
    principal = _principal(seed)
    workflow_id = await _create_draft(seed, principal, _request(seed))
    tickets = ReviewTicketService(seed.factory)
    async with seed.factory() as db, db.begin():
        repo: WorkflowRepository = PgWorkflowRepository(db, seed.tenant_id)
        service = WorkflowService(repo=repo, review=tickets, approvals=build_review_approval(seed.factory))
        # Act：team 档发布
        outcome = await service.publish(workflow_id=workflow_id, note="团队审", submitter_id=seed.user_id)
        # Assert：202 语义（pending_approval）+ 工单 id 回传 + head/状态不动
        assert outcome.status == "pending_approval" and outcome.governance == "team"
        assert outcome.ticket_id is not None and outcome.next_version == "v1"
        workflow = await repo.get(workflow_id)
        assert workflow is not None and workflow.head_version is None and workflow.status.value == "draft"
        assert await repo.list_versions(workflow_id) == []  # 未固化版本
        ticket_id = outcome.ticket_id
    # Assert：工单落库 target_type=workflow_publish（第七类对象候选，X16）
    async with seed.factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT target_type, target_id, status, payload FROM review_tickets WHERE id = CAST(:tid AS uuid)"
                ),
                {"tid": str(ticket_id)},
            )
        )
        ticket = row.mappings().one()
        assert ticket["target_type"] == "workflow_publish"
        assert ticket["status"] == "pending_review"
        assert ticket["target_id"] == workflow_id
        payload = dict(ticket["payload"] or {})
        assert payload["candidate_type"] == "workflow_publish"
        assert payload["workflow_id"] == str(workflow_id)
        assert payload["next_version"] == "v1"
        # 幂等：同对象重复提交返回既有单（uk_review_one_open）
        again = await tickets.submit_candidate(
            tenant_id=seed.tenant_id,
            target_type="workflow_publish",
            target_id=workflow_id,
            payload=payload,
            submitter_id=seed.user_id,
        )
        assert again == ticket_id


async def test_发布端点_直调_solo桩_200与team桩_202(wf_seed):
    # Arrange：端点直调（app.state 挂桩——组合根装配面同位）
    seed = wf_seed
    principal = _principal(seed)
    workflow_id = await _create_draft(seed, principal, _request(seed))
    from tests.workflows.conftest import FakeApprovals, FakeReview

    solo_review, solo_approvals = FakeReview(), FakeApprovals("solo")
    request_solo = _request(seed, approvals=solo_approvals, review=solo_review)
    team_review, team_approvals = FakeReview(), FakeApprovals("team")
    request_team = _request(seed, approvals=team_approvals, review=team_review)
    async with seed.factory() as db, db.begin():  # 显式提交（solo 直发固化版本跨块可见）
        # Act：solo 桩 → 200 published
        solo_resp = _FakeResponse()
        published = await publish_workflow(
            workflow_id, WorkflowPublishIn(note="直发"), principal, db, request_solo, solo_resp
        )
        response = WorkflowPublishOut.model_validate(published)
        # Assert：前端 PublishResult 形状（solo 路径无 approval_id/object_type/redirect）
        assert response.status == "published" and response.governance == "solo"
        assert response.next_version == "v1" and response.approval_id is None
        assert solo_review.submitted == []  # solo 不出工单
    async with seed.factory() as db, db.begin():
        repo: WorkflowRepository = PgWorkflowRepository(db, seed.tenant_id)
        workflow = await repo.get(workflow_id)
        assert workflow is not None and workflow.head_version == 1
    async with seed.factory() as db, db.begin():  # 审计行落库（全程可追溯）
        # Act：team 桩 → 202 pending（response.status_code 置 202）
        fake_resp = _FakeResponse()
        pending = await publish_workflow(
            workflow_id, WorkflowPublishIn(note="转审"), principal, db, request_team, fake_resp
        )
        response2 = WorkflowPublishOut.model_validate(pending)
        # Assert：pending_approval 形状（approval_id/object_type/redirect 齐备）
        assert fake_resp.status_code == 202
        assert response2.status == "pending_approval" and response2.governance == "team"
        assert response2.object_type == "workflow_publish" and response2.redirect == "/approvals"
        assert response2.approval_id is not None and response2.next_version == "v2"
        assert len(team_review.submitted) == 1
        assert team_review.submitted[0]["target_type"] == "workflow_publish"


async def test_发布端口未装配_503拒发(wf_seed):
    # Arrange：app.state 不挂审批/档位端口（未装配路径——硬门禁不静默跳过）
    seed = wf_seed
    principal = _principal(seed)
    workflow_id = await _create_draft(seed, principal, _request(seed))
    request = _request(seed)  # 不挂 approvals/review
    async with seed.factory() as db:
        # Act / Assert：503 + 5004（fail-closed）
        with pytest.raises(GatewayError) as exc:
            await publish_workflow(workflow_id, WorkflowPublishIn(), principal, db, request, _FakeResponse())
        assert exc.value.code == 5004 and exc.value.status_code == 503


class _SoloTier:
    """solo 档位桩（GovernanceTierPort 结构化满足；真实档位读路=build_review_approval）。"""

    async def tier(self, tenant_id: uuid.UUID) -> str:
        return "solo"


class _FakeResponse:
    """FastAPI Response 桩（端点直调时承载 status_code 置位）。"""

    def __init__(self) -> None:
        self.status_code = 200

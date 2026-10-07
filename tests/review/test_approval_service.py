"""审批服务集成测试（PG；review_tickets 六态 + 治理三档分派，不可达自动跳过）。

覆盖：team 禁自批/他人审批 approved；enterprise 双签齐才 approved（单签 pending）；
审批留痕（payload.approvals）；approved→published 联动；非 pending 单 4703 拒。
"""

from __future__ import annotations

import uuid

import pytest

from services.platform.kernel import DomainError
from services.review.data.orm import ReviewTicket as ReviewTicketORM

pytestmark = pytest.mark.integration


async def _open_ticket(seed, target_id: uuid.UUID | None = None) -> uuid.UUID:
    tid = target_id or uuid.uuid4()
    return await seed.tickets.submit_candidate(
        tenant_id=seed.tenant_id,
        target_type="plugin_listing",
        target_id=tid,
        payload={"envelope_version": 1, "candidate_type": "plugin_listing"},
        submitter_id=seed.submitter_id,
    )


async def test_审批服务_team档_自批4702拒绝_他人审批approved(review_seed):
    # Arrange
    seed = review_seed
    await seed.set_tier("team")
    ticket_id = await _open_ticket(seed)
    # Act / Assert：提交人自批 → 4702
    with pytest.raises(DomainError) as exc:
        await seed.approvals.decide(
            tenant_id=seed.tenant_id,
            ticket_id=ticket_id,
            action="approve",
            approver_id=seed.submitter_id,
            note="自批尝试",
        )
    assert int(str(exc.value)[:4]) == 4702
    # Act：他人审批
    result = await seed.approvals.decide(
        tenant_id=seed.tenant_id, ticket_id=ticket_id, action="approve", approver_id=seed.approver_a
    )
    # Assert：单审批人档一签完整 → approved
    assert result.status == "approved" and result.decision.complete


async def test_审批服务_enterprise档_第一签pending_第二签approved_四眼(review_seed):
    # Arrange
    seed = review_seed
    await seed.set_tier("enterprise")
    ticket_id = await _open_ticket(seed)
    # Act：第一签（审批人 A）
    first = await seed.approvals.decide(
        tenant_id=seed.tenant_id, ticket_id=ticket_id, action="approve", approver_id=seed.approver_a
    )
    # Assert：未齐 → 仍 pending_review（approved 不等于生效，08 §4）
    assert first.status == "pending_review" and not first.decision.complete
    # Act：同一人重复签 → 4702（四眼）
    with pytest.raises(DomainError) as exc:
        await seed.approvals.decide(
            tenant_id=seed.tenant_id, ticket_id=ticket_id, action="approve", approver_id=seed.approver_a
        )
    assert int(str(exc.value)[:4]) == 4702
    # Act：第二不同审批人签 → approved
    second = await seed.approvals.decide(
        tenant_id=seed.tenant_id, ticket_id=ticket_id, action="approve", approver_id=seed.approver_b
    )
    # Assert
    assert second.status == "approved" and second.decision.complete


async def test_审批服务_审批留痕approvals追加_决策人与档位可溯(review_seed):
    # Arrange
    seed = review_seed
    await seed.set_tier("enterprise")
    ticket_id = await _open_ticket(seed)
    # Act：两签走完
    await seed.approvals.decide(
        tenant_id=seed.tenant_id, ticket_id=ticket_id, action="approve", approver_id=seed.approver_a
    )
    await seed.approvals.decide(
        tenant_id=seed.tenant_id,
        ticket_id=ticket_id,
        action="approve",
        approver_id=seed.approver_b,
        note="合规复核通过",
    )
    # Assert：信封 approvals 两条，含档位与决策人（宪法 5 全程可追溯）
    ticket = await seed.tickets.get_ticket(tenant_id=seed.tenant_id, ticket_id=ticket_id)
    assert ticket is not None and ticket["status"] == "approved"
    approvals = ticket["payload"]["approvals"]
    assert [a["approver_id"] for a in approvals] == [str(seed.approver_a), str(seed.approver_b)]
    assert all(a["governance_tier"] == "enterprise" for a in approvals)


async def test_审批服务_approved后mark_published_未approved不可发布_4703(review_seed):
    # Arrange
    seed = review_seed
    await seed.set_tier("solo")  # solo：提交人即审批人（留痕自审放行）
    ticket_id = await _open_ticket(seed)
    # Act / Assert：未审批直接发布 → 拒（4703）
    with pytest.raises(ValueError) as exc:
        await seed.tickets.mark_published(tenant_id=seed.tenant_id, ticket_id=ticket_id, note="越级发布")
    assert "4703" in str(exc.value)
    # Act：solo 自批 → approved → 发布
    await seed.approvals.decide(
        tenant_id=seed.tenant_id, ticket_id=ticket_id, action="approve", approver_id=seed.submitter_id
    )
    await seed.tickets.mark_published(tenant_id=seed.tenant_id, ticket_id=ticket_id)
    # Assert：published（08 §4 approved 不等于生效）
    ticket = await seed.tickets.get_ticket(tenant_id=seed.tenant_id, ticket_id=ticket_id)
    assert ticket is not None and ticket["status"] == "published"


async def test_审批服务_非pending单拒绝决策_4703(review_seed):
    # Arrange：直接落一条 approved 单（绕过审批面，模拟终态）
    seed = review_seed
    ticket_id = await _open_ticket(seed)
    async with seed.factory() as db, db.begin():
        row = await db.get(ReviewTicketORM, ticket_id)
        assert row is not None
        row.status = "approved"
    # Act / Assert：对终态单再决策 → 4703
    with pytest.raises(DomainError) as exc:
        await seed.approvals.decide(
            tenant_id=seed.tenant_id, ticket_id=ticket_id, action="approve", approver_id=seed.approver_a
        )
    assert int(str(exc.value)[:4]) == 4703

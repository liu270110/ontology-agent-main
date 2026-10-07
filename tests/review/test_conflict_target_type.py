# tests/review/test_conflict_target_type.py
"""target_type=conflict 枚举扩展用例（KB-G1a 冲突分诊 T2 工单；PG 集成，不可达自动跳过）。

背景：review_tickets CheckConstraint 增设 'conflict'（OntRAG §8.1「对齐现行 review_workflow
状态机」——冲突工单复用 §7 单据机制）。本用例经 ReviewTicketService.submit_candidate 真连 PG
写入一单并读回，证明 ORM 词汇与库端约束（迁移 20260929_d3f6a9c1e2b7 原位重建
ck_review_tickets_target_type）一致；DTO TargetTypeFilter 同词汇随 services/review/api/schemas。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from services.review.business.candidates import ReviewTicketService

if TYPE_CHECKING:
    from .conftest import ReviewSeed


async def test_提交conflict工单_枚举扩展生效(review_seed: ReviewSeed) -> None:
    """T2 冲突工单 target_type=conflict 可落库可读回（uk_review_one_open 幂等同单返回）。"""
    # Arrange：冲突工单信封最小形（fact_a_id/fact_b_id + 并排摘要，kb/business/conflict_triage 同构）
    tickets: ReviewTicketService = review_seed.tickets
    target_id = uuid.uuid4()
    payload = {
        "envelope_version": "v1",
        "candidate_type": "conflict",
        "payload": {"conflict_type": "T2", "fact_a_id": str(uuid.uuid4()), "fact_b_id": str(uuid.uuid4())},
        "decision_options": ["winner_a", "winner_b", "t3_coexist", "pending"],
    }
    # Act
    ticket_id = await tickets.submit_candidate(
        tenant_id=review_seed.tenant_id, target_type="conflict", target_id=target_id, payload=payload
    )
    again = await tickets.submit_candidate(
        tenant_id=review_seed.tenant_id, target_type="conflict", target_id=target_id, payload=payload
    )
    stored = await tickets.get_ticket(tenant_id=review_seed.tenant_id, ticket_id=ticket_id)
    # Assert：写读一致 + open 单幂等（同对象不重开）
    assert again == ticket_id
    assert stored is not None and stored["target_type"] == "conflict" and stored["status"] == "pending_review"
    assert stored["payload"]["decision_options"] == ["winner_a", "winner_b", "t3_coexist", "pending"]

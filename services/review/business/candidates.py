"""review_candidates 服务 · review_tickets 写路径（08 §4 候选非成品门禁统一入口；review README M2.2）。

分层：本模块是 kb 抽取流水线写审核队列的唯一通道——review.data 模块私有（pyproject 契约六），
跨模块调用方只拿 Protocol：kb 走 services/platform/ports/review_port.py ``CandidateReviewPort``；
M5 治理三档审批链（08 §2.4）的消费方（plugin/gateway）只 import **本模块**（契约六唯一豁免边
``gateway.app -> review.business.candidates``），实现绑定在组合根（tests 可直接构造）。

事务纪律（03 §6.1 短事务）：每个方法自开一个短事务；``submit_candidate`` 幂等——同对象已有
open 单（uk_review_one_open 部分唯一：draft/pending_review）原样返回既有 id，不覆盖原信封，
断点续跑重放安全；信封一律整体重赋值（JSONB 不可原地变更）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.platform.kernel import DomainError
from services.review.data.governance import PgGovernanceTierReader
from services.review.data.orm import ReviewTicket
from services.review.domain.approval_chain import (
    ApprovalDecision,
    GovernanceTier,
    resolve_decision,
)

# 与 uk_review_one_open 的 postgresql_where 保持一致（08 §4：同对象唯一 open）
_OPEN_STATUSES: tuple[str, ...] = ("draft", "pending_review")

# 可决策状态（六态权威；draft 未提交/终态单不可审批——4703）
_DECIDABLE_STATUSES = ("pending_review",)


def _open_ticket_stmt(tenant_id: uuid.UUID, target_type: str, target_id: uuid.UUID):
    return (
        select(ReviewTicket)
        .where(
            ReviewTicket.tenant_id == tenant_id,
            ReviewTicket.target_type == target_type,
            ReviewTicket.target_id == target_id,
            ReviewTicket.status.in_(_OPEN_STATUSES),
        )
        .order_by(ReviewTicket.created_at)
        .limit(1)
    )


class ReviewTicketService:
    """review_tickets 写路径服务（结构化满足 CandidateReviewPort；组合根绑定）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

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
        """登记候选进审核队列，返回单据 id；同对象已有 open 单则幂等返回既有 id。"""
        async with self._factory() as session, session.begin():
            existing = (
                await session.execute(_open_ticket_stmt(tenant_id, target_type, target_id))
            ).scalar_one_or_none()
            if existing is not None:
                return existing.id
            ticket = ReviewTicket(
                tenant_id=tenant_id,
                target_type=target_type,
                target_id=target_id,
                payload=payload,
                submitter_id=submitter_id,
                status=status,
                sla_deadline=sla_deadline,
            )
            session.add(ticket)
            await session.flush()
            return ticket.id

    async def attach_gate_result(
        self, *, tenant_id: uuid.UUID, target_id: uuid.UUID, gate_result: dict[str, Any]
    ) -> None:
        """把门禁结论合并进该候选 open 单的信封；无 open 单时抛 LookupError（可追溯，不静默）。"""
        async with self._factory() as session, session.begin():
            ticket = (
                await session.execute(_open_ticket_stmt(tenant_id, "knowledge_instance", target_id))
            ).scalar_one_or_none()
            if ticket is None:
                raise LookupError(f"候选无 open 审核单: target_id={target_id}")
            merged = dict(ticket.payload or {})
            merged["gate_result"] = gate_result
            ticket.payload = merged  # JSONB 原地变更不可追踪，整体重赋值

    # ---- M5 治理三档审批链扩展（08 §2.4/§4；插件上架 target_type=plugin_listing 首个调用方）----

    async def get_open_ticket(
        self, *, tenant_id: uuid.UUID, target_type: str, target_id: uuid.UUID
    ) -> dict[str, Any] | None:
        """查同对象 open 单（uk_review_one_open 同口径）；返回 {id, status, submitter_id, payload}。"""
        async with self._factory() as session:
            ticket = (await session.execute(_open_ticket_stmt(tenant_id, target_type, target_id))).scalar_one_or_none()
            if ticket is None:
                return None
            return {
                "id": ticket.id,
                "status": ticket.status,
                "submitter_id": ticket.submitter_id,
                "payload": dict(ticket.payload or {}),
            }

    async def get_ticket(self, *, tenant_id: uuid.UUID, ticket_id: uuid.UUID) -> dict[str, Any] | None:
        """按 id 取单（租户过滤）；返回 {id, status, target_type, target_id, submitter_id, payload}。"""
        async with self._factory() as session:
            ticket = await session.get(ReviewTicket, ticket_id)
            if ticket is None or ticket.tenant_id != tenant_id:
                return None
            return {
                "id": ticket.id,
                "status": ticket.status,
                "target_type": ticket.target_type,
                "target_id": ticket.target_id,
                "submitter_id": ticket.submitter_id,
                "payload": dict(ticket.payload or {}),
            }

    async def mark_published(self, *, tenant_id: uuid.UUID, ticket_id: uuid.UUID, note: str = "") -> None:
        """approved → published（08 §4：approved 不等于生效，published 才生效——联动落库动作由调用方同事务编排）。"""
        async with self._factory() as session, session.begin():
            ticket = await session.get(ReviewTicket, ticket_id)
            if ticket is None or ticket.tenant_id != tenant_id:
                raise LookupError(f"审核单不存在: {ticket_id}")
            if ticket.status != "approved":
                raise ValueError(f"4703 REVIEW_TICKET_NOT_APPROVED: 仅 approved 单可发布（当前 {ticket.status}）")
            ticket.status = "published"
            if note:
                ticket.decision_note = note

    async def merge_payload(self, *, tenant_id: uuid.UUID, ticket_id: uuid.UUID, payload: dict[str, Any]) -> None:
        """信封整体重赋值（审批留痕追加面；JSONB 不可原地变更纪律同 attach_gate_result）。"""
        async with self._factory() as session, session.begin():
            ticket = await session.get(ReviewTicket, ticket_id)
            if ticket is None or ticket.tenant_id != tenant_id:
                raise LookupError(f"审核单不存在: {ticket_id}")
            ticket.payload = payload

    async def approve(
        self,
        *,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
        reviewer_id: uuid.UUID,
        decision_note: str = "",
    ) -> None:
        """pending_review → approved（签名集齐时由审批服务调用；服务端复查状态防并发双签穿透）。"""
        async with self._factory() as session, session.begin():
            ticket = await session.get(ReviewTicket, ticket_id)
            if ticket is None or ticket.tenant_id != tenant_id:
                raise LookupError(f"审核单不存在: {ticket_id}")
            if ticket.status != "pending_review":
                raise ValueError(f"4703 REVIEW_TICKET_NOT_OPEN: 单据非 pending_review 状态（当前 {ticket.status}）")
            ticket.status = "approved"
            ticket.reviewer_id = reviewer_id
            ticket.decision_note = decision_note or None

    async def reject(
        self,
        *,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
        reviewer_id: uuid.UUID,
        decision_note: str,
    ) -> None:
        """pending_review → rejected（必附理由，08 §4 REJ 回边）。"""
        if not decision_note:
            raise ValueError("3001 PARAM_INVALID: 驳回必附理由（08 §4）")
        async with self._factory() as session, session.begin():
            ticket = await session.get(ReviewTicket, ticket_id)
            if ticket is None or ticket.tenant_id != tenant_id:
                raise LookupError(f"审核单不存在: {ticket_id}")
            if ticket.status != "pending_review":
                raise ValueError(f"4703 REVIEW_TICKET_NOT_OPEN: 单据非 pending_review 状态（当前 {ticket.status}）")
            ticket.status = "rejected"
            ticket.reviewer_id = reviewer_id
            ticket.decision_note = decision_note


# ---------------------------------------------------------------- M5 治理三档审批链（08 §2.4/§4）


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """一次审批决策结果（API/编排层消费；complete=True 表示签名集齐可联动生效）。"""

    ticket_id: uuid.UUID
    status: str
    tier: GovernanceTier
    decision: ApprovalDecision


class ReviewApprovalService:
    """审核工单审批决策服务（三档审批链执行位；档位判定走 review.domain 收敛点）。

    档位读取经 ``GovernanceTierReader`` 端口注入（默认 :class:`PgGovernanceTierReader`，
    review.data 内部组装）——governance_tier 走 tenants.settings（08 §2.4 权威存储位）。
    """

    def __init__(self, tickets: ReviewTicketService, tier_reader: Any) -> None:
        self._tickets = tickets
        self._tier_reader = tier_reader

    async def tier(self, tenant_id: uuid.UUID) -> GovernanceTier:
        """租户档位读取透出面（编排方展示/幂等补齐路径用，判定仍走 resolve_decision 收敛点）。"""
        return await self._tier_reader.get_tier(tenant_id)

    async def decide(
        self,
        *,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
        action: str,
        approver_id: uuid.UUID,
        note: str = "",
    ) -> DecisionResult:
        """对 pending_review 单落一笔审批决策（approve/reject）；越档拒绝（4702）。

        approve 且签名集齐 → 单据 approved（**不自动生效**——published 由业务联动方显式
        mark_published，08 §4「approved 不等于生效」）；reject → 单据 rejected。
        """
        if action not in ("approve", "reject"):
            raise DomainError("3001 PARAM_INVALID: action 仅支持 approve/reject")
        ticket = await self._tickets.get_ticket(tenant_id=tenant_id, ticket_id=ticket_id)
        if ticket is None:
            raise LookupError(f"审核单不存在: {ticket_id}")
        if ticket["status"] not in _DECIDABLE_STATUSES:
            raise DomainError(f"4703 REVIEW_TICKET_NOT_OPEN: 单据非 pending_review 状态（当前 {ticket['status']}）")

        tier = await self._tier_reader.get_tier(tenant_id)
        prior = _prior_approvers(ticket["payload"])
        decision = resolve_decision(
            tier,
            action="approve" if action == "approve" else "reject",  # Literal 收窄
            submitter_id=ticket["submitter_id"],
            approver_id=approver_id,
            prior_approvers=prior,
        )
        if not decision.allowed:
            raise DomainError(f"4702 APPROVER_NOT_ALLOWED: {decision.reason}")

        await self._append_and_advance(
            tenant_id=tenant_id,
            ticket_id=ticket_id,
            action=action,
            approver_id=approver_id,
            note=note,
            tier=tier,
            complete=decision.complete,
        )
        final_status = ("approved" if action == "approve" else "rejected") if decision.complete else "pending_review"
        return DecisionResult(ticket_id=ticket_id, status=final_status, tier=tier, decision=decision)

    async def _append_and_advance(
        self,
        *,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
        action: str,
        approver_id: uuid.UUID,
        note: str,
        tier: GovernanceTier,
        complete: bool,
    ) -> None:
        """审批留痕（payload.approvals 追加）+ 终态推进（approved/rejected 仅在 complete 时落）。"""
        ticket = await self._tickets.get_ticket(tenant_id=tenant_id, ticket_id=ticket_id)
        if ticket is None:  # 理论不可达（decide 已校验）；防御性留痕
            raise LookupError(f"审核单不存在: {ticket_id}")
        payload = dict(ticket["payload"] or {})
        approvals = list(payload.get("approvals") or [])
        approvals.append(
            {
                "action": action,
                "approver_id": str(approver_id),
                "governance_tier": tier.value,
                "note": note,
                "decided_at": datetime.now(UTC).isoformat(),
            }
        )
        payload["approvals"] = approvals
        await self._tickets.merge_payload(tenant_id=tenant_id, ticket_id=ticket_id, payload=payload)
        if complete:
            if action == "approve":
                await self._tickets.approve(
                    tenant_id=tenant_id, ticket_id=ticket_id, reviewer_id=approver_id, decision_note=note
                )
            else:
                await self._tickets.reject(
                    tenant_id=tenant_id, ticket_id=ticket_id, reviewer_id=approver_id, decision_note=note
                )


def _prior_approvers(payload: dict[str, Any]) -> tuple[uuid.UUID, ...]:
    """从信封提取既有审批人（enterprise 四眼查重输入；缺省空）。"""
    result: list[uuid.UUID] = []
    for item in payload.get("approvals") or []:
        try:
            result.append(uuid.UUID(str(item["approver_id"])))
        except (KeyError, ValueError):  # 留痕畸形不炸审批链——跳过该条
            continue
    return tuple(result)


def build_review_approval(session_factory: async_sessionmaker[AsyncSession]) -> ReviewApprovalService:
    """组合根装配入口（gateway/_build_candidate_review 唯一 import 边；契约六豁免面）。"""
    tickets = ReviewTicketService(session_factory)
    return ReviewApprovalService(tickets, PgGovernanceTierReader(session_factory))

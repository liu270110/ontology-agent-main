"""审核工单端口（plugin 侧 Protocol；依赖倒置同 CandidateReviewPort 模式）。

review.data 模块私有（pyproject 契约六：plugin 侧禁入且无豁免），插件上架用例只依赖本
Protocol；实现=services/review/business/candidates.ReviewTicketService（组合根绑定
app.state.candidate_review，结构化满足本协议——submit_candidate 形状与平台端口一致）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class TicketView:
    """工单只读视图（审批编排输入）。"""

    id: uuid.UUID
    status: str
    target_type: str
    target_id: uuid.UUID
    submitter_id: uuid.UUID | None
    payload: dict[str, Any]


@runtime_checkable
class PluginReviewPort(Protocol):
    """插件上架审核工单端口（review_tickets 写路径的 plugin_listing 面）。"""

    async def submit_candidate(
        self,
        *,
        tenant_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
        payload: dict[str, Any],
        status: str = "pending_review",
        submitter_id: uuid.UUID | None = None,
        sla_deadline: Any = None,
    ) -> uuid.UUID:
        """登记候选进审（幂等：同对象唯一 open 单）；target_type 固定 plugin_listing。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def get_open_ticket(
        self, *, tenant_id: uuid.UUID, target_type: str, target_id: uuid.UUID
    ) -> dict[str, Any] | None: ...

    async def get_ticket(self, *, tenant_id: uuid.UUID, ticket_id: uuid.UUID) -> dict[str, Any] | None: ...

    async def mark_published(self, *, tenant_id: uuid.UUID, ticket_id: uuid.UUID, note: str = "") -> None: ...


@runtime_checkable
class ReviewDecisionPort(Protocol):
    """治理三档审批决策端口（依赖倒置：实现=review.business.candidates.ReviewApprovalService，
    组合根绑定；plugin 侧零 review.business import——契约六唯一豁免边只覆盖 candidates/gateway）。"""

    async def decide(
        self,
        *,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
        action: str,
        approver_id: uuid.UUID,
        note: str = "",
    ) -> Any:
        """落一笔审批决策；返回带 status/tier/decision(complete, signatures_*) 的结果值。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def tier(self, tenant_id: uuid.UUID) -> Any:
        """租户治理档位（GovernanceTier 枚举值）。"""
        ...  # pragma: no cover — Protocol 方法无实现

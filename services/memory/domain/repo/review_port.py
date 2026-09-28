"""审核工单端口（memory 侧 Protocol；依赖倒置同 plugin ReviewPort / kb CandidateReviewPort 模式）。

review.data 模块私有（pyproject 契约六：memory 侧禁入且无豁免，唯一豁免边=gateway.app 组合根），
L2→L3 升级用例只依赖本 Protocol；实现=services/review/business/candidates.ReviewTicketService
与 ReviewApprovalService（组合根 lifespan 装配 app.state.promotion_review / review_approvals，
结构化满足本协议——鸭子类型，memory 侧零 review import 边）。

target_type 固定 'memory_l2_upgrade'（review_tickets 枚举既有值；uk_review_one_open 同对象唯一
open 单口径与 kb/plugin 共享同一 ReviewTicketService 实例）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PromotionReviewPort(Protocol):
    """升级单审核工单端口（review_tickets 写路径的 memory_l2_upgrade 面）。"""

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
        """登记升级单进审（幂等：同对象唯一 open 单原样返回既有 id）。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def get_ticket(self, *, tenant_id: uuid.UUID, ticket_id: uuid.UUID) -> dict[str, Any] | None:
        """按 id 取单（租户过滤）——{id, status, target_type, target_id, submitter_id, payload}。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def mark_published(self, *, tenant_id: uuid.UUID, ticket_id: uuid.UUID, note: str = "") -> None:
        """approved → published（08 §4：approved 不等于生效，published 才生效）。"""
        ...  # pragma: no cover — Protocol 方法无实现


@runtime_checkable
class PromotionDecisionPort(Protocol):
    """治理三档审批决策端口（依赖倒置：实现=review.business.candidates.ReviewApprovalService，
    组合根绑定；memory 侧零 review.business import——契约六唯一豁免边只覆盖 candidates/gateway）。"""

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

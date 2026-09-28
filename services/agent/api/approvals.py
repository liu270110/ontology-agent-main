"""L2 运行中审批路由（H-0b ①；api/01 §5.15 ★ 行——2026-09-29 契约先行补录）。

对 waiting_tool 态 Run 的 approve/reject 呈现面 + 待审批动作视图（Agent服务设计 §5.2 /
边界契约 D6/B5）。纪律：全部写路径经 UoW+聚合方法（核验链在 business/approval_service，
路由只做装配与信封）；scope=POST session:chat / GET session:read（选择理由 api/01 §5.15
H-0b 注记①：审批对象是会话内 Run 的推进，复用 sessions 族既有 scope，授权依据=会话主体）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from services.agent.api.deps import SessionChatDep, SessionReadDep, UowDep
from services.agent.api.schemas.approval import (
    ApprovalDecisionIn,
    ApprovalDecisionOut,
    PendingApprovalOut,
)
from services.agent.business.approval_service import RunApprovalService

router = APIRouter(tags=["approvals"])


def _current_request(request: Request) -> Request:
    """Request 透传依赖：审批中心联动取 app.state.candidate_review；直调用例可缺省不传。"""
    return request


RequestOptDep = Annotated[Request | None, Depends(_current_request)]


def _ticket_port(request: Request | None) -> object | None:
    """审批中心端口装配（组合根 lifespan 已挂 app.state.candidate_review=CandidateReviewPort）。

    未装配（直调/单测）返回 None——服务侧按 H-0b 注记③降级为审计标注，不阻断裁决。
    """
    if request is None:
        return None
    return getattr(request.app.state, "candidate_review", None)


@router.post(
    "/tasks/{task_id}/runs/{run_id}/approvals",
    status_code=202,
    summary="运行中审批裁决（approve→resume / reject→终态；H-0b，api/01 §5.15）",
)
async def decide_run_approval(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    body: ApprovalDecisionIn,
    principal: SessionChatDep,
    uow: UowDep,
    request: RequestOptDep = None,
) -> dict:
    """裁决（202）：核验链=租户可见 → waiting_tool 态 → 锚点 param_hash 一致（B5 绑定）。

    409+4102（非 waiting_tool/无锚点）、409+3001（哈希不一致）详见 approval_service docstring。
    """
    service = RunApprovalService(uow, ticket_port=_ticket_port(request))
    result = await service.decide(
        tenant_id=principal.tenant_id,
        approver_id=principal.user_id,
        task_id=task_id,
        run_id=run_id,
        decision=body.decision,
        param_hash=body.param_hash,
        reason=body.reason,
        create_ticket=body.create_ticket,
    )
    return {"data": ApprovalDecisionOut(**vars(result)).model_dump(), "meta": {}}


@router.get(
    "/tasks/{task_id}/runs/{run_id}/approvals/pending",
    summary="当前待审批动作视图（waiting_tool 态；H-0b，api/01 §5.15）",
)
async def get_pending_run_approval(
    task_id: uuid.UUID, run_id: uuid.UUID, principal: SessionReadDep, uow: UowDep
) -> dict:
    """待审批动作投影（恒 200）：action_iri/param_hash/execution_mode/waiting_since。"""
    service = RunApprovalService(uow)
    view = await service.pending_view(tenant_id=principal.tenant_id, task_id=task_id, run_id=run_id)
    return {"data": PendingApprovalOut(**vars(view)).model_dump(), "meta": {}}

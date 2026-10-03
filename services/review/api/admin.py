"""admin 审核工单 REST 面（api/01 §5.8 ★ 两端点；M5 交付验收条件四 P2-1）。

    GET  /admin/reviews                 审核工单列表（缺省待审队列）        review:read     200
    POST /admin/reviews/{id}/decision   审批裁决（通过 / 驳回附理由）       review:approve  200 / 404、4701、4702、4703

分层与静态边纪律（本模块关键约束）：gateway.app 仅一行挂载本 router；import-linter 契约五/六
禁止 gateway **传递触达** ``review.data``/``review.domain``（pyproject 无 ``gateway.app ->
review.api.*`` 豁免边且本批禁改 pyproject）——故本文件零 review.data/business/domain 静态 import：

- 决策：审批服务经 ``app.state.review_approvals``（lifespan ``build_review_approval`` 装配单例，
  即 review.business.candidates 的 ReviewApprovalService）鸭子类型调用——三档审批链、禁自批（4702）、
  非待审单（4703）等约束全部由服务层/领域收敛点保证，路由层零状态机逻辑（plugin 先例同款）；
- 列表：``text()`` 原生 SQL 直查 review_tickets（services/review/data/governance.py 直查 tenants
  表的同款先例——零 ORM import 边，列名漂移风险由 tests/review 集成用例锚定）；
- 缺省口径：status 未指定时取 ``pending_review``（可决策待审队列；其余五态经 status 显式过滤）；
- R50 联调修复（2026-09-28）：status 额外接受前端别名 pending/done（STATUS_QUERY_MAP 收敛
  为工单六态 IN 过滤；5xx 根因=app.py 校验异常 handler 返回裸
  dict 在 Starlette≥0.50 下 'dict' object is not callable，已改 JSONResponse）；
- B1 批（2026-10-04）：列表信封改 api/01 §3.1 {data, meta:{page,page_size,total}}——
  {code,message,data} 旧信封废止（联调缺陷台账 A-5 证据端点），page/page_size 参数生效。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import TextClause, bindparam, text

from services.platform.deps import Principal, SessionDep, require_scope
from services.platform.errors import GatewayError
from services.platform.kernel import DomainError
from services.platform.schemas import PageMeta
from services.review.api.schemas.admin import (
    STATUS_QUERY_MAP,
    AdminReviewListOut,
    AdminReviewOut,
    DecisionIn,
    DecisionOut,
    StatusQuery,
    TargetTypeFilter,
)

router = APIRouter(prefix="/admin/reviews", tags=["admin"])

ReviewReadDep = Annotated[Principal, Depends(require_scope("review:read"))]
ReviewApproveDep = Annotated[Principal, Depends(require_scope("review:approve"))]

_QUEUE_DEFAULT_STATUS = "pending_review"  # 「open 工单」缺省口径=待审（可决策，08 §4）


# ---------------------------------------------------------------- 装配与异常映射


def _approvals(request: Request) -> Any:
    """审批服务装配单例（lifespan 挂 app.state；未装配=503，同 plugin 路由端口检查先例）。"""
    approvals = getattr(request.app.state, "review_approvals", None)
    if approvals is None:
        raise GatewayError(5004, "审批服务未装配", status_code=503)
    return approvals


def _domain_error(exc: DomainError) -> GatewayError:
    """DomainError → 统一错误体：码取消息前缀（47xx review 段），HTTP 按段映射（plugin 先例同款）。"""
    message = str(exc)
    head = message[:4]
    code = int(head) if head.isdigit() else 4702
    status_code = {3001: 422, 4701: 409, 4702: 403, 4703: 409}.get(code, 409)
    return GatewayError(code, message, status_code=status_code)


def _list_stmts(target_type: str | None, statuses: tuple[str, ...]) -> tuple[TextClause, TextClause, dict[str, Any]]:
    """工单列表 + 计数查询（词汇表经 Literal DTO 收敛后拼片，无注入面；租户隔离必带）。

    R50 联调修复（2026-09-28）：status 过滤改 IN 元组——前端审批中心传别名
    pending/done（approvals/api.ts），经 STATUS_QUERY_MAP 收敛为工单六态；expanding
    bindparam 渲染 IN 列表，注入面仍为零。
    """
    where = ["tenant_id = :tenant_id", "status IN :statuses"]
    params: dict[str, Any] = {"statuses": statuses}
    if target_type is not None:
        where.append("target_type = :target_type")
        params["target_type"] = target_type
    where_sql = f" WHERE {' AND '.join(where)}"
    stmt = text(
        "SELECT id, target_type, target_id, status, submitter_id, reviewer_id, decision_note,"
        " sla_deadline, created_at FROM review_tickets"
        f"{where_sql} ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
    ).bindparams(bindparam("statuses", expanding=True))
    count_stmt = text(f"SELECT count(*) FROM review_tickets{where_sql}").bindparams(
        bindparam("statuses", expanding=True)
    )
    return stmt, count_stmt, params


# ---------------------------------------------------------------- 端点


@router.get("", summary="审核工单列表（open 待审队列；target_type/status 过滤；api/01 §3.1 信封）")
async def list_reviews(
    principal: ReviewReadDep,
    db: SessionDep,
    target_type: Annotated[TargetTypeFilter | None, Query()] = None,
    status_filter: Annotated[StatusQuery, Query(alias="status")] = _QUEUE_DEFAULT_STATUS,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AdminReviewListOut:
    """分页列出本租户工单（最新优先）；审批工作台缺省看 pending_review。

    R50 联调修复（2026-09-28）：status 接受前端别名 pending/done（别名面保留不变）。
    B1 批（2026-10-04）：信封改 api/01 §3.1 {data, meta:{page,page_size,total}}——废止
    {code,message,data} 旧信封（A-5 证据端点）；offset/limit 参数改 page/page_size。
    """
    offset = (page - 1) * page_size
    statuses = STATUS_QUERY_MAP[status_filter]
    stmt, count_stmt, params = _list_stmts(target_type, statuses)
    base_params: dict[str, Any] = {"tenant_id": str(principal.tenant_id), "statuses": statuses, **params}
    rows = (await db.execute(stmt, {**base_params, "limit": page_size, "offset": offset})).mappings().all()
    total = (await db.execute(count_stmt, base_params)).scalar_one()
    items = [
        AdminReviewOut(
            id=row["id"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            status=row["status"],
            submitter_id=row["submitter_id"],
            reviewer_id=row["reviewer_id"],
            decision_note=row["decision_note"],
            sla_deadline=row["sla_deadline"],
            created_at=row["created_at"],
        )
        for row in rows
    ]
    return AdminReviewListOut(data=items, meta=PageMeta(page=page, page_size=page_size, total=int(total)))


@router.post(
    "/{ticket_id}/decision",
    summary="审批裁决（三档审批链；approved 不等于生效，published 由业务联动方显式推进）",
)
async def decide_review(
    ticket_id: uuid.UUID,
    body: DecisionIn,
    principal: ReviewApproveDep,
    request: Request,
) -> DecisionOut:
    """落一笔审批决策：禁自批/四眼/越档由 ReviewApprovalService.decide 保证（4702→403）。"""
    try:
        result = await _approvals(request).decide(
            tenant_id=principal.tenant_id,
            ticket_id=ticket_id,
            action=body.action,
            approver_id=principal.user_id,
            note=body.note,
        )
    except LookupError as exc:
        raise GatewayError(404, str(exc), status_code=404) from exc
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return DecisionOut(
        ticket_id=result.ticket_id,
        status=result.status,
        governance_tier=result.tier.value,
        signatures_required=result.decision.signatures_required,
        signatures_collected=result.decision.signatures_collected,
        complete=result.decision.complete,
    )

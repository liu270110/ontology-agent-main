"""workflows 工作流编排路由（api/01 §5.11 预登记端点实现；15 §1.3 行为面；scope deny-by-default）。

    GET    /workflow-templates          三静态模板目录            workflow:read    200
    GET    /workflows                   列表（query 模糊 name）   workflow:read    200
    POST   /workflows                   建草稿（模板实例化）      workflow:edit    201
    GET    /workflows/{id}              详情（实时校验结果）      workflow:read    200 / 404*
    PUT    /workflows/{id}              草稿保存（存前过校验）    workflow:edit    200 / 404*、4801/4802、4803
    DELETE /workflows/{id}              删除（仅草稿）            workflow:edit    204 / 404*、4803
    POST   /workflows/{id}/versions     提交发布（治理分流）      workflow:publish 200/202 / 404*、4801/4802
    GET    /workflows/{id}/versions     版本历史                  workflow:read    200 / 404*
    POST   /workflows/{id}/rollback     回滚（以目标版本新建草稿） workflow:edit   202 / 404*

router 不设统一 prefix：/workflow-templates 为顶层命名空间（api/01 §5.11 预登记逐字路径，
与 /workflows 段并列——40 篇 R3 /runs 顶层命名空间同款显式约定）。promote/test/runs/resume
四族=后续批（执行引擎/run→template 提升不含于本批；15 §1 边界裁决延续）。
分层：L2 router → workflows.business（用例）→ {L4 Protocol, L6 仓储}；路由层零校验/状态
逻辑。审批端口绑定发生在网关组合根（app.state.candidate_review / review_approvals，
review.business.candidates 单例；本模块经 workflows.domain.repo.review_port Protocol 鸭子
类型消费，零 review 模块 import——plugin 先例）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from services.platform.deps import Principal, SessionDep, require_scope
from services.platform.errors import GatewayError
from services.platform.kernel import DomainError
from services.platform.schemas import PageMeta
from services.workflows.api.schemas.workflow import (
    WfDetailOut,
    WfSummaryOut,
    WfVersionOut,
    WorkflowCreatedOut,
    WorkflowCreateIn,
    WorkflowDetailEnvelope,
    WorkflowListEnvelope,
    WorkflowPublishIn,
    WorkflowPublishOut,
    WorkflowRollbackIn,
    WorkflowRollbackOut,
    WorkflowSavedOut,
    WorkflowSaveIn,
    WorkflowTemplatesOut,
    WorkflowValidationOut,
    WorkflowVersionsOut,
)
from services.workflows.business.service import PublishOutcome, WorkflowService, validation_violations
from services.workflows.data.repo_impl.workflow_repo import PgWorkflowRepository
from services.workflows.domain.model.graph import WorkflowGraph, validate_graph
from services.workflows.domain.repo.review_port import GovernanceTierPort, WorkflowReviewPort
from services.workflows.domain.templates import list_templates

router = APIRouter(tags=["workflows"])

WorkflowReadDep = Annotated[Principal, Depends(require_scope("workflow:read"))]
WorkflowEditDep = Annotated[Principal, Depends(require_scope("workflow:edit"))]
WorkflowPublishDep = Annotated[Principal, Depends(require_scope("workflow:publish"))]

# 审批中心前端路由（mock PublishResult.redirect 同值；pending_approval 路径跳转提示用）
_APPROVALS_REDIRECT = "/approvals"


# ---------------------------------------------------------------- 装配与异常映射


def _service(db: SessionDep, principal: Principal) -> WorkflowService:
    """用例装配（每请求绑定请求会话；仓储构造期绑定租户——行级租户隔离）。"""
    return WorkflowService(repo=PgWorkflowRepository(db, principal.tenant_id))


def _publish_service(request: Request, db: SessionDep, principal: Principal) -> WorkflowService:
    """发布用例装配（审批/档位端口为 lifespan 单例；缺装配 fail-closed 503——硬门禁不静默）。"""
    review = getattr(request.app.state, "candidate_review", None)
    approvals = getattr(request.app.state, "review_approvals", None)
    if not isinstance(review, WorkflowReviewPort) or not isinstance(approvals, GovernanceTierPort):
        raise GatewayError(5004, "审核工单/治理档位端口未装配", status_code=503)
    return WorkflowService(repo=PgWorkflowRepository(db, principal.tenant_id), review=review, approvals=approvals)


def _domain_error(exc: DomainError, *, graph: WorkflowGraph | None = None) -> GatewayError:
    """DomainError → 统一错误体：码取消息前缀（48xx workflows 段），HTTP 按语义映射。

    4801/4802 → HTTP 409：api/01 §5.11 PUT/POST versions 主要错误码登记为 409*（图违规=
    与 DAG 不变量/资源现态冲突，4xxx 业务规则段 404/409/422 语义表——X16 存储批对齐契约
    登记，原 422 映射系 F1 偏差）。4801/4802 路径且调用方持有请求图时，detail 结构化重列
    全量违规项（校验三件纯函数同输入同输出——15 §1.1「4801 detail 结构化列违规项」）。
    """
    message = str(exc)
    head = message[:4]
    code = int(head) if head.isdigit() else 4801
    detail = validate_graph(graph) if (code in (4801, 4802) and graph is not None) else None
    return GatewayError(code, message, status_code=409, detail=detail)  # 48xx 全段=业务规则冲突（api/01 §4 语义表）


def _not_found(exc: LookupError) -> GatewayError:
    return GatewayError(404, str(exc), status_code=404)


# ---------------------------------------------------------------- 端点（§5.11 逐字路径）


@router.get("/workflow-templates", summary="工作流模板目录（三静态代码内置；形状=前端 listTemplates）")
async def list_workflow_templates(principal: WorkflowReadDep) -> WorkflowTemplatesOut:
    # 模板为代码内置常量（15 §1.3「模板=代码内置三静态」），零 IO；scope 与列表端点同 workflow:read
    _ = principal
    return WorkflowTemplatesOut(items=list_templates())


@router.get("/workflows", summary="工作流列表（query 模糊 name；api/01 §3.1 信封；WfSummary 11 字段）")
async def list_workflows(
    principal: WorkflowReadDep,
    db: SessionDep,
    query: Annotated[str | None, Query(max_length=128)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> WorkflowListEnvelope:
    service = _service(db, principal)
    items, total = await service.list_page(query=query, page=page, page_size=page_size)
    return WorkflowListEnvelope(
        data=[WfSummaryOut.from_domain(w) for w in items],
        meta=PageMeta(page=page, page_size=page_size, total=total),
    )


@router.post("/workflows", status_code=201, summary="建草稿（模板实例化；模板内置即过校验三件）")
async def create_workflow(
    body: WorkflowCreateIn, principal: WorkflowEditDep, db: SessionDep, request: Request
) -> WorkflowCreatedOut:
    service = _service(db, principal)
    try:
        workflow = await service.create(
            tenant_id=principal.tenant_id,
            name=body.name,
            description=body.description,
            template=body.template,
            created_by=principal.user_id,
            trace_id=getattr(request.state, "trace_id", "") or "",
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return WorkflowCreatedOut(id=workflow.id, draft_version=workflow.draft_version_label)


@router.get("/workflows/{workflow_id}", summary="详情（draft_diff v1 空对象；validation=实时校验结果）")
async def get_workflow(workflow_id: uuid.UUID, principal: WorkflowReadDep, db: SessionDep) -> WorkflowDetailEnvelope:
    service = _service(db, principal)
    try:
        workflow, versions = await service.list_versions(workflow_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    detail = WfDetailOut.from_domain(
        workflow,
        versions=[WfVersionOut.from_domain(v) for v in versions],
        validation=WorkflowValidationOut.model_validate(validation_violations(workflow.draft)),
    )
    return WorkflowDetailEnvelope(data=detail)


@router.put("/workflows/{workflow_id}", summary="草稿保存（仅草稿可改 4803；存前过校验三件 4801/4802）")
async def save_workflow(
    workflow_id: uuid.UUID,
    body: WorkflowSaveIn,
    principal: WorkflowEditDep,
    db: SessionDep,
    request: Request,
) -> WorkflowSavedOut:
    service = _service(db, principal)
    nodes = [n.to_domain() for n in body.nodes]
    edges = [e.to_domain() for e in body.edges]
    graph = WorkflowGraph(nodes=nodes, edges=edges)  # 4801/4802 时 detail 重列违规项（纯函数同输入同输出）
    try:
        workflow = await service.save_draft(
            workflow_id=workflow_id,
            nodes=nodes,
            edges=edges,
            name=body.name,
            description=body.description,
            actor_id=principal.user_id,
            trace_id=getattr(request.state, "trace_id", "") or "",
        )
    except LookupError as exc:
        raise _not_found(exc) from exc
    except DomainError as exc:
        raise _domain_error(exc, graph=graph) from exc
    return WorkflowSavedOut(id=workflow.id, draft_version=workflow.draft_version_label, saved_at=datetime.now(UTC))


@router.delete("/workflows/{workflow_id}", status_code=204, summary="删除（仅草稿；已发布 409+4803）")
async def delete_workflow(
    workflow_id: uuid.UUID, principal: WorkflowEditDep, db: SessionDep, request: Request
) -> Response:
    service = _service(db, principal)
    try:
        await service.delete(
            workflow_id=workflow_id,
            actor_id=principal.user_id,
            trace_id=getattr(request.state, "trace_id", "") or "",
        )
    except LookupError as exc:
        raise _not_found(exc) from exc
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return Response(status_code=204)


@router.post(
    "/workflows/{workflow_id}/versions",
    summary="提交发布（校验三件→治理分流：solo 直发 200 / team·enterprise 202 pending+工单）",
)
async def publish_workflow(
    workflow_id: uuid.UUID,
    body: WorkflowPublishIn,
    principal: WorkflowPublishDep,
    db: SessionDep,
    request: Request,
    response: Response,
) -> WorkflowPublishOut:
    service = _publish_service(request, db, principal)
    try:
        outcome: PublishOutcome = await service.publish(
            workflow_id=workflow_id,
            note=body.note,
            submitter_id=principal.user_id,
            trace_id=getattr(request.state, "trace_id", "") or "",
        )
    except LookupError as exc:
        raise _not_found(exc) from exc
    except DomainError as exc:
        raise _domain_error(exc) from exc
    if outcome.status == "pending_approval":
        response.status_code = 202
        return WorkflowPublishOut(
            status="pending_approval",
            governance=outcome.governance,
            approval_id=str(outcome.ticket_id),
            object_type="workflow_publish",
            redirect=_APPROVALS_REDIRECT,
            next_version=outcome.next_version,
        )
    return WorkflowPublishOut(status="published", governance=outcome.governance, next_version=outcome.next_version)


@router.post(
    "/workflows/{workflow_id}/rollback",
    status_code=202,
    summary="回滚（以目标版本内容新建草稿——27 篇 §3；版本行零触碰，head 历史保留）",
)
async def rollback_workflow(
    workflow_id: uuid.UUID,
    body: WorkflowRollbackIn,
    principal: WorkflowEditDep,
    db: SessionDep,
    request: Request,
) -> WorkflowRollbackOut:
    service = _service(db, principal)
    try:
        workflow = await service.rollback(
            workflow_id=workflow_id,
            to_version=body.version_number,
            actor_id=principal.user_id,
            trace_id=getattr(request.state, "trace_id", "") or "",
        )
    except LookupError as exc:
        raise _not_found(exc) from exc
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return WorkflowRollbackOut(
        draft_version=workflow.draft_version_label,
        copied_from=body.to_version.strip(),
        note="复制旧版全部节点与参数为新草稿，不影响已发布版本与运行历史",
    )


@router.get("/workflows/{workflow_id}/versions", summary="版本历史（不可变版本升序；含草稿标签与 diff 空对象）")
async def list_workflow_versions(
    workflow_id: uuid.UUID, principal: WorkflowReadDep, db: SessionDep
) -> WorkflowVersionsOut:
    service = _service(db, principal)
    try:
        workflow, versions = await service.list_versions(workflow_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return WorkflowVersionsOut(
        items=[WfVersionOut.from_domain(v) for v in versions],
        draft={"version": workflow.draft_version_label, "diff": {"add": 0, "del": 0, "mod": 0}},
    )

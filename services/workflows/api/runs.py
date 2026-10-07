"""X16 工作流运行路由（api/01 §5.11 预登记 runs 族六端点 + promote 提升端点；scope=workflow:run/read/edit）。

    POST /workflows/runs/{run_id}/promote    存为工作流草稿（40 篇 §6 提升）  workflow:edit 201/200 / 404*、409*
    POST /workflows/{id}/test                试运行（dry-run→task type=workflow_test） workflow:run  202 / 409*、4102
    POST /workflows/{id}/runs                正式运行（→task type=workflow_run）       workflow:run  202 / 409*、4102
    GET  /workflows/{id}/runs                  运行历史（对齐任务中心过滤）              workflow:read 200 / 404*
    GET  /workflows/{id}/runs/{run_id}         运行详情（节点状态聚合视图）              workflow:read 200 / 404*
    POST /workflows/{id}/runs/{run_id}/resume  断点恢复（审批通过/修参续跑）             workflow:run  202 / 404*、409*
    POST /workflows/{id}/runs/{run_id}/abort   运行中止（对齐 tasks/cancel 语义）        workflow:run  202 / 404*、409*

路由注册序（api/01 §5.11 promote 行显式约定）：promote 挂 workflows 命名空间下 runs
子资源（4 段路径），与 /workflows/{id} 段（≤3 段）无前缀交叠、方法面亦不冲突；本路由
仍按契约把 promote 定义置于 runs 族**首位**（含于本 router 的注册序即文档化约定）。

执行挂接：task+run 行入队后由 TaskRunWorker 认领（orchestrator_provider 同款 provider
注入工作流执行器形态——04 §3 管道零新通道；事件经 task_events 回放根 + hub 双通道）。
分层：L2 router → workflows.business.runs（用例）→ {L4 模型, tx.workflows/tx.tasks 仓储}；
审批中心工单联动经 app.state.candidate_review（CandidateReviewPort，workflows.py 同款注入）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from services.agent.domain.model.task import TaskError
from services.platform.deps import Principal, UowDep, domain_error, require_scope
from services.platform.schemas import PageMeta
from services.workflows.api.schemas.runs import (
    WorkflowAbortIn,
    WorkflowPromoteIn,
    WorkflowPromoteOut,
    WorkflowResumeIn,
    WorkflowRunAcceptedOut,
    WorkflowRunControlOut,
    WorkflowRunDetailEnvelope,
    WorkflowRunIn,
    WorkflowRunsOut,
    WorkflowRunSummaryOut,
    WorkflowTestIn,
)
from services.workflows.business.runs import PromoteOutcome, RunControlOutcome, SubmitOutcome, WorkflowRunControl

router = APIRouter(tags=["workflows"])

WorkflowRunScopeDep = Annotated[Principal, Depends(require_scope("workflow:run"))]
WorkflowReadDep = Annotated[Principal, Depends(require_scope("workflow:read"))]


# ---------------------------------------------------------------- 装配


def _control(request: Request, principal: Principal, uow: UowDep) -> WorkflowRunControl:
    """用例装配（uow 构造期注入；审批中心端口=lifespan 单例，未装配降级为审计标注）。"""
    review = getattr(request.app.state, "candidate_review", None)
    return WorkflowRunControl(uow=uow, review=review)


def _origin_trace(request: Request | None) -> str:
    """C4 trace 贯通（sessions._origin_trace 同款）：受理面记录网关原始 trace 供 worker 回溯。"""
    state = getattr(request, "state", None)
    return str(getattr(state, "trace_id", "") or "").strip()


# ---------------------------------------------------------------- 提升（40 篇 §6 run→template）


@router.post(
    "/workflows/runs/{run_id}/promote",
    summary="存为工作流草稿（40 篇 §6：端点新建草稿并带 source_run_id 血统；幂等键=run_id）",
)
async def promote_workflow_run(
    run_id: uuid.UUID,
    body: WorkflowPromoteIn,
    principal: Annotated[Principal, Depends(require_scope("workflow:edit"))],
    uow: UowDep,
    request: Request,
    response: Response,
) -> WorkflowPromoteOut:
    """执行成果提升为工作流草稿（api/01 §5.11 ★ 行）：图=本次 run 实际执行图（工作流族）
    或计划投影（chat 族 PLAN_UPDATED 回扫，origin=llm_candidate——发布必过审批，宪法 3）；
    幂等键=run_id，重复调用返回既有草稿 id（201 新建 / 200 幂等命中）；提升前 Kahn 环
    检测 + 孤儿节点剔除（4801→409）。"""
    control = _control(request, principal, uow)
    outcome: PromoteOutcome = await control.promote(
        run_id=run_id,
        tenant_id=principal.tenant_id,
        actor_id=principal.user_id,
        title=body.title,
        variable_hints=body.variable_hints,
        trace_id=_origin_trace(request),
    )
    if not outcome.created:  # 幂等命中 → 200（新建 201 由路由默认状态码承载）
        response.status_code = 200
    return WorkflowPromoteOut(
        workflow_id=outcome.workflow_id,
        status="draft_created" if outcome.created else "exists",
        draft_version=outcome.draft_version,
        source_run_id=outcome.source_run_id,
        origin=outcome.origin,  # type: ignore[arg-type]  # 字面量面=WorkflowOrigin 词汇（用例枚举收敛）
    )


# ---------------------------------------------------------------- 受理（test / runs）


@router.post(
    "/workflows/{workflow_id}/test",
    status_code=202,
    summary="试运行（dry-run→task type=workflow_test；执行草稿固化快照，支持节点断点）",
)
async def submit_workflow_test(
    workflow_id: uuid.UUID,
    body: WorkflowTestIn,
    principal: WorkflowRunScopeDep,
    uow: UowDep,
    request: Request,
) -> WorkflowRunAcceptedOut:
    """27 篇 §3「草稿可随意改→试运行→提交发布」：图=受理时草稿固化快照；breakpoints 命中
    暂停（waiting_approval 语义）。校验三件不过/同工作流活跃运行 → 409（4801/4802/4102）。"""
    return await _accept(
        workflow_id=workflow_id,
        kind="workflow_test",
        breakpoints=body.breakpoints,
        variables=body.variables,
        principal=principal,
        uow=uow,
        request=request,
    )


@router.post(
    "/workflows/{workflow_id}/runs",
    status_code=202,
    summary="正式运行（→task type=workflow_run；执行 head 不可变版本快照，仅 published）",
)
async def submit_workflow_run(
    workflow_id: uuid.UUID,
    body: WorkflowRunIn,
    principal: WorkflowRunScopeDep,
    uow: UowDep,
    request: Request,
) -> WorkflowRunAcceptedOut:
    """api/01 §5.11 正式运行：执行 head 不可变版本快照（版本不可变——27 篇 §3）；未发布
    4804→409。并发互斥 4102→409（同工作流至多一个活跃任务）。"""
    return await _accept(
        workflow_id=workflow_id,
        kind="workflow_run",
        breakpoints=None,
        variables=body.variables,
        principal=principal,
        uow=uow,
        request=request,
    )


async def _accept(
    *,
    workflow_id: uuid.UUID,
    kind: str,
    breakpoints: list[str] | None,
    variables: dict,
    principal: Principal,
    uow: UowDep,
    request: Request,
) -> WorkflowRunAcceptedOut:
    control = _control(request, principal, uow)
    outcome: SubmitOutcome = await control.submit(
        workflow_id=workflow_id,
        kind=kind,
        tenant_id=principal.tenant_id,
        triggered_by=principal.user_id,
        breakpoints=breakpoints,
        variables=variables,
        origin_trace_id=_origin_trace(request),
    )
    return WorkflowRunAcceptedOut(
        task_id=outcome.task_id, run_id=outcome.run_id, kind=outcome.kind, version=outcome.version
    )


# ---------------------------------------------------------------- 读面（runs 列表 / 详情）


@router.get("/workflows/{workflow_id}/runs", summary="运行历史（对齐任务中心过滤 type=workflow_run|workflow_test）")
async def list_workflow_runs(
    workflow_id: uuid.UUID,
    principal: WorkflowReadDep,
    uow: UowDep,
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> WorkflowRunsOut:
    control = _control(request, principal, uow)
    items, total = await control.list_runs(
        workflow_id=workflow_id,
        tenant_id=principal.tenant_id,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return WorkflowRunsOut(
        data=[
            WorkflowRunSummaryOut(
                run_id=task.runs[0].id if task.runs else task.active_run_id,
                task_id=task.id,
                kind=str((task.payload or {}).get("kind") or task.type),
                version=(task.payload or {}).get("version"),
                task_status=task.status.value,
                run_status=task.runs[0].status.value if task.runs else None,
                created_at=task.created_at,
            )
            for task in items
        ],
        meta=PageMeta(page=page, page_size=page_size, total=total),
    )


@router.get("/workflows/{workflow_id}/runs/{run_id}", summary="运行详情（节点状态聚合视图；试运行面板取数口）")
async def get_workflow_run(
    workflow_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: WorkflowReadDep,
    uow: UowDep,
    request: Request,
) -> WorkflowRunDetailEnvelope:
    """节点状态聚合视图（27 篇 §3 底部抽屉「节点状态时间线」）：投影 payload.workflow_state
    （执行器落账）+ run/task 行终态；断线重连快照兜底同源（40 篇 §4.4 R3 口径）。"""
    control = _control(request, principal, uow)
    detail = await control.run_detail(workflow_id=workflow_id, run_id=run_id, tenant_id=principal.tenant_id)
    return WorkflowRunDetailEnvelope(data=detail)


# ---------------------------------------------------------------- 控制（resume / abort）


@router.post(
    "/workflows/{workflow_id}/runs/{run_id}/resume",
    status_code=202,
    summary="断点恢复（审批通过/修参续跑；27 篇 §3 time-travel 语义；worker resume 队列认领）",
)
async def resume_workflow_run(
    workflow_id: uuid.UUID,
    run_id: uuid.UUID,
    body: WorkflowResumeIn,
    principal: WorkflowRunScopeDep,
    uow: UowDep,
    request: Request,
) -> WorkflowRunControlOut:
    """核验链同 H-0b（waiting_tool→锚点→param_hash 绑定）；approve=票仓+run.start()+
    修参 params_override；reject=run cancel+task failed。409+4102（非暂停态/无锚点）、
    409+3001（哈希不一致/审批缺哈希）。"""
    control = _control(request, principal, uow)
    try:
        outcome: RunControlOutcome = await control.resume(
            workflow_id=workflow_id,
            run_id=run_id,
            tenant_id=principal.tenant_id,
            approver_id=principal.user_id,
            decision=body.decision,
            param_hash=body.param_hash,
            params=body.params,
            note=body.note,
        )
    except TaskError as exc:  # 聚合状态机断言（04 §3）→ 4102 → 409（tasks/cancel 同款映射）
        raise domain_error(exc, fallback_code=4102) from exc
    return WorkflowRunControlOut(
        run_id=outcome.run_id,
        decision=outcome.decision,
        run_status=outcome.run_status,
        resumed_node=outcome.resumed_node,
    )


@router.post(
    "/workflows/{workflow_id}/runs/{run_id}/abort",
    status_code=202,
    summary="运行中止（对齐 tasks/cancel 语义；开放节点 FINISHED(cancelled) 标注）",
)
async def abort_workflow_run(
    workflow_id: uuid.UUID,
    run_id: uuid.UUID,
    body: WorkflowAbortIn,
    principal: WorkflowRunScopeDep,
    uow: UowDep,
    request: Request,
) -> WorkflowRunControlOut:
    control = _control(request, principal, uow)
    try:
        outcome: RunControlOutcome = await control.abort(
            workflow_id=workflow_id,
            run_id=run_id,
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            reason=body.reason,
        )
    except TaskError as exc:  # 终态不可逆断言（04 §3）→ 4102 → 409（tasks/cancel 同款映射）
        raise domain_error(exc, fallback_code=4102) from exc
    return WorkflowRunControlOut(
        run_id=outcome.run_id, decision=outcome.decision, run_status=outcome.run_status
    )

"""L2 tasks 路由（api/01 §5.2 ★ 行，M1 子集：列表 / 详情 / 取消）。

纪律：取消=聚合方法（04 §3 task/run 状态机唯一入口，禁直改 status）；任务受理侧在
sessions.send_message（豁免①）；run 终态级联保存经 TaskRepository.save（聚合内实体）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from services.agent.api.deps import SessionReadDep, SessionWriteDep, UowDep, domain_error
from services.agent.api.schemas.task import TaskDetailOut, TaskListOut, task_detail_from_domain, task_from_domain
from services.agent.domain.model.task import TaskError, TaskEvent
from services.platform.errors import GatewayError

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", summary="任务列表（按 session_id/status/type 过滤）")
async def list_tasks(
    principal: SessionReadDep,
    uow: UowDep,
    session_id: uuid.UUID | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    type_filter: Annotated[str | None, Query(alias="type")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TaskListOut:
    async with uow.for_tenant(principal.tenant_id) as tx:
        items = await tx.tasks.list(
            session_id=session_id,
            status=status_filter,
            task_type=type_filter,
            offset=offset,
            limit=limit,
        )
    return TaskListOut(items=[task_from_domain(t) for t in items], offset=offset, limit=limit)


@router.get("/{task_id}", summary="任务详情（状态 / 用量 / Run 历史）")
async def get_task(task_id: uuid.UUID, principal: SessionReadDep, uow: UowDep) -> TaskDetailOut:
    async with uow.for_tenant(principal.tenant_id) as tx:
        task = await tx.tasks.get(task_id)
    if task is None:
        raise GatewayError(404, "任务不存在", status_code=404)
    return task_detail_from_domain(task)


@router.post("/{task_id}/cancel", status_code=status.HTTP_202_ACCEPTED, summary="取消运行（202 / 4102）")
async def cancel_task(task_id: uuid.UUID, principal: SessionWriteDep, uow: UowDep) -> TaskDetailOut:
    """取消=聚合方法 task.cancel()（running→cancelled；非运行态抛 4102，api/01 §5.2 登记码）。

    cancelled 落终态前的取消清单化传播归执行编排（04 §3 注记，M3+）；本端点只做状态迁移与级联保存。
    """
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            task = await tx.tasks.get(task_id)
            if task is None:
                raise GatewayError(404, "任务不存在", status_code=404)
            task.cancel()  # 非法迁移/终态不可逆在聚合内断言（04 §2）
            await tx.tasks.save(task)  # 级联保存活跃 Run 的 cancelled 终态
            await tx.tasks.append_event(task.id, TaskEvent(task_id=task.id, event_type="task.cancelled", data={}))
            # run.cancelled 消费方=计量汇总/前端通知（04 §6.1）；M1 进程内缓冲，M4 起 outbox 同事务落库
            tx.enqueue_projection("run.cancelled", task.id, {"task_id": str(task.id)})
        return task_detail_from_domain(task)
    except TaskError as exc:
        raise domain_error(exc, fallback_code=4102) from exc

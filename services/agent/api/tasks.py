"""L2 tasks 路由（api/01 §5.2 ★ 行：列表 / 详情 / 取消 / 事件时间线）。

纪律：取消=聚合方法（04 §3 task/run 状态机唯一入口，禁直改 status）；任务受理侧在
sessions.send_message（豁免①）；run 终态级联保存经 TaskRepository.save（聚合内实体）。
事件时间线（2026-09-27 批）：task_events 按 seq 回放（Last-Event-ID=after_seq 口径），
`Accept: text/event-stream` 订阅 SSE（PG 回放+尾随轮询至终态），否则 JSON 游标分页；
只读零事务争议——流内短事务即用即弃（03 §6.1）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from services.agent.api.deps import SessionReadDep, SessionWriteDep, UowDep, domain_error
from services.agent.api.schemas.task import (
    TaskDetailOut,
    TaskEventOut,
    TaskEventPageOut,
    TaskListOut,
    task_detail_from_domain,
    task_from_domain,
)
from services.agent.domain.model.task import TaskError, TaskEvent
from services.platform.errors import GatewayError

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _current_request(request: Request) -> Request:
    """Request 透传依赖：SSE 分流读头/设置；端点直调用例可缺省不传（None→JSON 形态）。"""
    return request


RequestOptDep = Annotated[Request | None, Depends(_current_request)]

_SSE_MEDIA_TYPE = "text/event-stream"
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
_REPLAY_PAGE = 500  # 回放分页深取（PG 全量持久，无 4301 窗口语义）
_TAIL_MAX_S = 600.0  # SSE 尾随上限（客户端断线凭 Last-Event-ID 重连即续，防悬挂连接）


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


# ── 任务事件时间线（api/01 §5.2 ★：GET /tasks/{id}/events，JSON/SSE 双形态）──────────


@router.get(
    "/{task_id}/events",
    summary="任务事件时间线（Accept: text/event-stream 订阅，否则 JSON 游标分页）",
    response_model=None,  # 双形态返回（JSON TaskEventPageOut / SSE StreamingResponse），响应模型禁生成
)
async def list_task_events(
    task_id: uuid.UUID,
    principal: SessionReadDep,
    uow: UowDep,
    request: RequestOptDep = None,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> TaskEventPageOut | StreamingResponse:  # noqa: PYI041  # 形态注解仅文档用（response_model=None）
    """task_events 按 seq 回放（先落库后推送纪律的读侧；PG 全量持久、无 4301 窗口语义）。

    - JSON：`after_seq` 游标（=Last-Event-ID 口径）+ limit，返回 `next_after_seq` 续读；
    - SSE：Last-Event-ID 头/query 兜底 → 回放 + 尾随轮询至任务终态（心跳注释帧防代理断连，
      尾随上限 600s 防悬挂，客户端重连即续）。
    """
    wants_sse = request is not None and _SSE_MEDIA_TYPE in request.headers.get("accept", "")
    async with uow.for_tenant(principal.tenant_id) as tx:
        task = await tx.tasks.get(task_id)
        if task is None:
            raise GatewayError(404, "任务不存在", status_code=404)
    if wants_sse and request is not None:
        header_id = request.headers.get("last-event-id", "").strip()
        effective = int(header_id) if header_id.isdigit() else after_seq
        poll_interval = float(getattr(request.app.state.settings, "task_worker_poll_interval_s", 1.0))
        return StreamingResponse(
            _task_event_stream(uow, principal.tenant_id, task_id, effective, poll_interval),
            media_type=_SSE_MEDIA_TYPE,
            headers=dict(_SSE_HEADERS),
        )
    async with uow.for_tenant(principal.tenant_id) as tx:
        rows = await tx.tasks.list_events(task_id, after_seq=after_seq, limit=limit)
    return TaskEventPageOut(
        items=[_event_out(r) for r in rows],
        next_after_seq=rows[-1].seq if rows else None,
        limit=limit,
    )


def _event_out(event: TaskEvent) -> TaskEventOut:
    return TaskEventOut(seq=event.seq or 0, event_type=event.event_type, data=event.data, created_at=event.created_at)


def _encode_task_frame(seq: int, event_type: str, data: dict[str, Any]) -> bytes:
    """任务事件 → SSE 帧（格式权威=02 §5：id=task_events.seq，data 单行紧凑 JSON）。

    本层不 import gateway（契约「模块禁逆向依赖 gateway」零豁免），编码据此内联；
    与 gateway/sse/events.encode_frame 同构，收敛下沉 platform 随 M4 公共件批。
    """
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {seq}\nevent: {event_type}\ndata: {payload}\n\n".encode()


async def _task_event_stream(
    uow: Any, tenant_id: uuid.UUID, task_id: uuid.UUID, after_seq: int | None, poll_interval: float
) -> AsyncIterator[bytes]:
    """PG 回放 + 尾随轮询（事件源=task_events 行）。"""
    cursor = after_seq
    started = time.monotonic()
    last_frame_at = time.monotonic()
    terminal = False
    while True:
        async with uow.for_tenant(tenant_id) as tx:
            rows = await tx.tasks.list_events(task_id, after_seq=cursor, limit=_REPLAY_PAGE)
            if not rows:
                stored = await tx.tasks.get(task_id)
                terminal = stored is not None and stored.status.value in ("succeeded", "failed", "cancelled")
        for row in rows:
            cursor = row.seq
            frame = _encode_task_frame(row.seq or 0, row.event_type, row.data or {})
            last_frame_at = time.monotonic()
            yield frame
        if terminal or time.monotonic() - started > _TAIL_MAX_S:
            return
        if time.monotonic() - last_frame_at > 15.0:  # 心跳注释帧（02 §5；不计入事件序列）
            yield b": ping\n\n"
            last_frame_at = time.monotonic()
        await asyncio.sleep(poll_interval)

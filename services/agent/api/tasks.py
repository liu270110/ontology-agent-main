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
    TaskLogOut,
    TaskLogPageOut,
    TaskRetryIn,
    TaskRetryOut,
    task_detail_from_domain,
    task_from_domain,
)
from services.agent.domain.model.task import TaskError, TaskEvent
from services.platform.errors import GatewayError
from services.platform.schemas import PageMeta

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _current_request(request: Request) -> Request:
    """Request 透传依赖：SSE 分流读头/设置；端点直调用例可缺省不传（None→JSON 形态）。"""
    return request


RequestOptDep = Annotated[Request | None, Depends(_current_request)]

_SSE_MEDIA_TYPE = "text/event-stream"
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
_REPLAY_PAGE = 500  # 回放分页深取（PG 全量持久，无 4301 窗口语义）
_TAIL_MAX_S = 600.0  # SSE 尾随上限（客户端断线凭 Last-Event-ID 重连即续，防悬挂连接）


@router.get("", summary="任务列表（按 session_id/status/type 过滤；api/01 §3.1 信封）")
async def list_tasks(
    principal: SessionReadDep,
    uow: UowDep,
    session_id: uuid.UUID | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    type_filter: Annotated[str | None, Query(alias="type")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TaskListOut:
    """偏移分页改 page/page_size（B1 批，api/01 §3.1；offset=(page-1)*page_size 内部换算）。"""
    offset = (page - 1) * page_size
    async with uow.for_tenant(principal.tenant_id) as tx:
        items = await tx.tasks.list(
            session_id=session_id,
            status=status_filter,
            task_type=type_filter,
            offset=offset,
            limit=page_size,
        )
        total = await tx.tasks.count(session_id=session_id, status=status_filter, task_type=type_filter)
    return TaskListOut(
        data=[task_from_domain(t) for t in items],
        meta=PageMeta(page=page, page_size=page_size, total=total),
    )


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


# ── W1 缺口补齐批（2026-10-04）：logs 行视图 + retry（api/01 §5.2/§5.15 登记行实装）──────

_LOG_ERROR_MARKS = ("ERROR", "FAIL")  # event_type 关键词 → level 收敛（logs 行视图口径）
_LOG_WARN_MARKS = ("WARN",)


def _log_level(event_type: str) -> str:
    upper = event_type.upper()
    if any(mark in upper for mark in _LOG_ERROR_MARKS):
        return "error"
    if any(mark in upper for mark in _LOG_WARN_MARKS):
        return "warn"
    return "info"


def _log_line(event: TaskEvent) -> str:
    """事件 → 单行日志文案：event_type 打头 + 紧凑 data JSON（无 data 则仅 event_type）。"""
    data = event.data or {}
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")) if data else ""
    return f"{event.event_type} {payload}".strip()


@router.get("/{task_id}/logs", summary="run 日志行视图（task_events 投影 ts/level/line；区别于 /events 事件流）")
async def list_task_logs(
    task_id: uuid.UUID,
    principal: SessionReadDep,
    uow: UowDep,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> TaskLogPageOut:
    """api/01 §5.15 登记行 + 契约冻结 2026-10-04 ③（前端 tasks/api.ts listTaskLogs：
    `{items:[{ts,level,line}], next_cursor}`，items 为冻结口径）。数据源=task_events 只读
    投影（与 /events 同源不同视图）；next_cursor=after_seq 续读游标（字符串），取尽为 null。"""
    async with uow.for_tenant(principal.tenant_id) as tx:
        if await tx.tasks.get(task_id) is None:
            raise GatewayError(404, "任务不存在", status_code=404)
        rows = await tx.tasks.list_events(task_id, after_seq=after_seq, limit=limit + 1)
    has_more = len(rows) > limit
    page = rows[:limit]
    return TaskLogPageOut(
        items=[TaskLogOut(ts=row.created_at, level=_log_level(row.event_type), line=_log_line(row)) for row in page],
        next_cursor=str(page[-1].seq) if has_more and page else None,
    )


@router.post(
    "/{task_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    summary="重试（失败任务重建 Run：attempt_count+1、新 Run queued 交 worker 认领）",
)
async def retry_task(task_id: uuid.UUID, body: TaskRetryIn, principal: SessionWriteDep, uow: UowDep) -> TaskRetryOut:
    """api/01 §5.15 登记行 + 契约冻结 2026-10-04 ④（202 受理，`{id, status:"queued", scope}`）。

    重试=编排器主权动作的 REST 面（04 §3）：经聚合方法 start_retry_run 重建 Run——
    attempt_count+1、新 Run queued（TaskRunWorker 秒级认领）、任务行落 running（04 §3
    「重试期间 task 保持 RUNNING」，持久层任务五态无 queued，响应 status=queued 指重建
    Run 已入队，终态真值以 GET /tasks/{id} 轮询为准）。非失败/非重试任务拒绝：

    - 任务不存在 → 404；succeeded/cancelled 等非 running/failed 态 → 4102 → 409；
    - 活跃 Run 未终态 → 4102 TASK_ALREADY_RUNNING → 409；
    - 最近失败 retryable=false（RunRetryPolicy 口径：cancelled/判据满足/预算耗尽类）→ 409；
    - attempt_count 已达 3（含首次）→ 聚合内断言 4102 → 409。
    """
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            task = await tx.tasks.get(task_id)
            if task is None:
                raise GatewayError(404, "任务不存在", status_code=404)
            last = task.runs[-1] if task.runs else None
            if last is not None and last.error is not None and last.error.get("retryable") is False:
                raise TaskError("4102 TASK_NOT_RETRYABLE: 最近一次失败 retryable=false（RunRetryPolicy 口径，04 §3）")
            run = task.start_retry_run()  # 聚合方法：仅 running/failed；attempt≤3 断言在内
            await tx.tasks.save(task)  # 级联保存新 Run（queued）
            await tx.tasks.append_event(
                task.id,
                TaskEvent(
                    task_id=task.id,
                    event_type="task.retry_queued",
                    data={"run_id": str(run.id), "scope": body.scope},
                ),
            )
            tx.enqueue_projection("task.retry_queued", task.id, {"task_id": str(task.id), "run_id": str(run.id)})
    except TaskError as exc:
        raise domain_error(exc, fallback_code=4102) from exc
    return TaskRetryOut(id=task.id, status="queued", scope=body.scope, run_id=run.id)

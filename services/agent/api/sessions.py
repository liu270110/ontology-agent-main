"""L2 sessions 路由（api/01 §5.2 契约，M1 子集 + 计划 3.2 SSE 主干）。

纪律：全部写路径经 UoW+聚合方法——禁裸 SQL、禁绕过聚合直改 status（03 §6.1 / 04 §2）；
send_message 适用豁免①（消息落库 + 任务受理同一事务，03 §6.1）；close 为生命周期迁移唯一入口
（04 §3 状态机，聚合方法 session.close()）。计划 3.2：POST messages 按 Accept 分流——
`text/event-stream` → SSE 事件流（编排器 stream_chat，事务外长流程），否则 202+{run_id}
（api/01 §6.1）；GET events = SSE 订阅/断线重连（Last-Event-ID，02 §5）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from services.agent.api.deps import (
    SessionChatDep,
    SessionReadDep,
    SessionWriteDep,
    UowDep,
    domain_error,
)
from services.agent.api.schemas.session import (
    MessagePageOut,
    SendMessageIn,
    SessionCreateIn,
    SessionListOut,
    SessionOut,
    from_domain,
    message_from_domain,
    to_domain,
)
from services.agent.business.chat_events import ChatCommand, ChatOutcome
from services.agent.business.chat_orchestrator import build_chat_orchestrator
from services.agent.domain.model.session import Message, SessionError
from services.agent.domain.model.task import Task, TaskError, TaskEvent
from services.memory.business.runtime import build_l1_store  # memory 公开装配面（memory.data 模块私有，P2-2 收口）
from services.platform.db.uow import AsyncUnitOfWork
from services.platform.deps import get_redis, get_session_factory, get_uow
from services.platform.errors import GatewayError

router = APIRouter(prefix="/sessions", tags=["sessions"])

_SSE_MEDIA_TYPE = "text/event-stream"
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}  # 02 §5 响应头固定项


def _current_request(request: Request) -> Request:
    """Request 透传依赖：路由内可用 request；端点直调用例（test_sessions）可缺省不传。"""
    return request


RequestOptDep = Annotated[Request | None, Depends(_current_request)]


class SseHubProtocol(Protocol):
    """SSE 枢纽结构化协议（组合根装配于 app.state；本层不 import gateway——契约②）。

    实现 = services.gateway.sse.hub.SseHub（publish 返回 (seq, 帧)，open_stream 同步
    完成订阅——4301 在建流前抛出，转统一错误体而非断流中报错）。
    """

    def publish(self, session_id: uuid.UUID, name: str, data: dict[str, Any]) -> tuple[int, bytes]: ...

    def open_stream(
        self, session_id: uuid.UUID, *, last_event_id: int | None, heartbeat_s: float
    ) -> AsyncIterator[bytes]: ...


def _get_hub(request: Request) -> SseHubProtocol:
    hub = getattr(request.app.state, "sse_hub", None)
    if hub is None:
        raise GatewayError(5004, "SSE 枢纽未初始化", status_code=503)
    return hub  # type: ignore[no-any-return]


def _get_chat_orchestrator(request: Request) -> Any:
    """取/建对话编排器（模块级组合模式，同 kb.py get_model_port 先例；app.state 单例缓存）。

    组合内容：双适配器（builtin=ModelPort，claude=直连无 key 5002）+ 上下文组装器
    （kb 检索服务在 chat_context 工厂内装配）+ 结果汇（PG 短事务 UoW#2）。
    """
    cached = getattr(request.app.state, "chat_orchestrator", None)
    if cached is not None:
        return cached
    settings = request.app.state.settings
    l1_store = getattr(request.app.state, "l1_store", None)
    if l1_store is None:
        l1_store = build_l1_store(get_redis(settings), ttl_seconds=settings.memory_l1_ttl_seconds)
        request.app.state.l1_store = l1_store
    orchestrator = build_chat_orchestrator(
        model_port=getattr(request.app.state, "model_port", None),
        l1_store=l1_store,
        session_factory=get_session_factory(settings),
        ollama_base_url=settings.ollama_base_url,
        result_sink=build_chat_result_sink(get_uow(request)),
    )
    request.app.state.chat_orchestrator = orchestrator
    return orchestrator


@router.post("", status_code=status.HTTP_201_CREATED, summary="创建会话（绑定 agent）")
async def create_session(body: SessionCreateIn, principal: SessionWriteDep, uow: UowDep) -> SessionOut:
    async with uow.for_tenant(principal.tenant_id) as tx:
        session = to_domain(body, tenant_id=principal.tenant_id, user_id=principal.user_id)
        await tx.sessions.add(session, channel=body.channel)
    return from_domain(session)


@router.get("", summary="当前用户会话列表")
async def list_sessions(
    principal: SessionReadDep,
    uow: UowDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SessionListOut:
    async with uow.for_tenant(principal.tenant_id) as tx:
        items = await tx.sessions.list_for_user(principal.user_id, offset=offset, limit=limit)
    return SessionListOut(items=[from_domain(s) for s in items], offset=offset, limit=limit)


@router.get("/{session_id}", summary="会话详情与状态")
async def get_session(session_id: uuid.UUID, principal: SessionReadDep, uow: UowDep) -> SessionOut:
    async with uow.for_tenant(principal.tenant_id) as tx:
        session = await tx.sessions.get(session_id)
    if session is None:
        raise GatewayError(404, "会话不存在", status_code=404)
    return from_domain(session)


@router.post("/{session_id}/close", status_code=status.HTTP_202_ACCEPTED, summary="关闭会话（状态迁移）")
async def close_session(session_id: uuid.UUID, principal: SessionWriteDep, uow: UowDep) -> SessionOut:
    """close=生命周期状态迁移唯一入口（api/01 §5.2 裁决注记；触发 L1 归档/L2 沉淀走事件，M3+）。"""
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            session = await tx.sessions.get(session_id)
            if session is None:
                raise GatewayError(404, "会话不存在", status_code=404)
            session.close()  # 状态机断言在聚合方法内（04 §3）；created→closed 非法亦在此拒绝
            await tx.sessions.save_meta(session)
            # session.closed 消费方=memory_service（04 §6.1）；M1 进程内缓冲，M4 起 outbox 同事务落库
            tx.enqueue_projection("session.closed", session.id, {"session_id": str(session.id)})
        return from_domain(session)
    except SessionError as exc:
        raise domain_error(exc, fallback_code=4101) from exc


@router.post(
    "/{session_id}/messages",
    status_code=status.HTTP_202_ACCEPTED,
    summary="发送消息（Accept: text/event-stream → SSE 流；否则 202+占位 run）",
    response_model=None,  # 双形态返回（202 dict / SSE StreamingResponse），响应模型禁生成
)
async def send_message(
    session_id: uuid.UUID, body: SendMessageIn, principal: SessionChatDep, uow: UowDep, request: RequestOptDep = None
) -> dict | StreamingResponse:
    """受理（消息落库+任务受理同一事务）后按 Accept 分流（api/01 §5.2）：

    - SSE：编排器 stream_chat 全程事务外（03 §6.1），事件经网关 hub 发布并直接下发本连接
      （02 §5 写入与推送分离——本连接不消费自己的队列）；
    - 非 SSE：202 + {run_id, task_id, status}（api/01 §6.1；request=None 的直调路径同此）。

    预检次序=03 §3：先聚合状态（closed→4101，不变式只在 Session.append_message 断言一次），
    再会话级活跃任务预检（4102；并发硬保证=uk_tasks_one_active_run，04 §2.1）。
    """
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            session = await tx.sessions.get(session_id)
            if session is None:
                raise GatewayError(404, "会话不存在", status_code=404)
            seq = session.append_message("user", body.content)  # 4101：closed 后拒新消息（04 §2）
            if await tx.tasks.find_running_by_session(session_id) is not None:
                raise TaskError("4102 TASK_ALREADY_RUNNING: 会话存在运行中的任务（03 篇 §3 预检）")
            message = Message(
                session_id=session_id, seq=seq, role="user", content=body.content, content_type=body.content_type
            )
            await tx.sessions.append_message(session_id, message)
            await tx.sessions.save_meta(session)  # created→active（首条用户消息，04 §3）随标量保存
            task = Task(tenant_id=principal.tenant_id, type="chat", session_id=session_id, payload={"message_seq": seq})
            run = task.start_run()  # 聚合方法：pending→running + 活跃 Run（queued）
            await tx.tasks.save(task)
            event = TaskEvent(
                task_id=task.id,
                event_type="task.created",
                data={"session_id": str(session_id), "message_seq": seq},
            )
            await tx.tasks.append_event(task.id, event)
    except SessionError as exc:
        raise domain_error(exc, fallback_code=4101) from exc
    except TaskError as exc:
        raise domain_error(exc, fallback_code=4102) from exc

    if request is None or _SSE_MEDIA_TYPE not in request.headers.get("accept", ""):
        return {"data": {"run_id": str(run.id), "task_id": str(task.id), "status": run.status.value}, "meta": {}}
    command = ChatCommand(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        session_id=session_id,
        task_id=task.id,
        run_id=run.id,
        agent_id=session.agent_id,
        message=body.content,
        trace_id=getattr(request.state, "trace_id", "") or f"req-{run.id}",
        adapter=body.adapter,
    )
    return _chat_stream_response(request, session_id, command)


@router.get("/{session_id}/events", summary="SSE 订阅 / 断线重连（Last-Event-ID，02 §5）")
async def stream_events(
    session_id: uuid.UUID,
    principal: SessionChatDep,
    uow: UowDep,
    request: Request,
    last_event_id: Annotated[int | None, Query(ge=0)] = None,
) -> StreamingResponse:
    """SSE 订阅/重订阅：Last-Event-ID 头优先（EventSource 语义），query 兜底（api/01 §5.2）。

    超出回放窗口 → 4301 SSE_REPLAY_EXPIRED（HTTP 410，api/01 §4.3；在建流前同步抛出）。
    订阅检查（会话存在性）用短事务即用即弃，流内零事务（03 §6.1）。
    """
    async with uow.for_tenant(principal.tenant_id) as tx:
        if await tx.sessions.get(session_id) is None:
            raise GatewayError(404, "会话不存在", status_code=404)
    header_id = request.headers.get("last-event-id", "").strip()
    effective = int(header_id) if header_id.isdigit() else last_event_id
    heartbeat_s = float(getattr(request.app.state.settings, "sse_heartbeat_seconds", 15))
    stream = _get_hub(request).open_stream(session_id, last_event_id=effective, heartbeat_s=heartbeat_s)
    return StreamingResponse(stream, media_type=_SSE_MEDIA_TYPE, headers=dict(_SSE_HEADERS))


def _chat_stream_response(request: Request, session_id: uuid.UUID, command: ChatCommand) -> StreamingResponse:
    """编排器事件流 → hub 发布 + 本连接直发（生产连接不消费自身队列，02 §5）。"""
    hub = _get_hub(request)
    orchestrator = _get_chat_orchestrator(request)

    async def event_stream() -> AsyncIterator[bytes]:
        async for event in orchestrator.stream_chat(command):
            _, frame = hub.publish(session_id, event.name.value, event.data)
            yield frame

    return StreamingResponse(event_stream(), media_type=_SSE_MEDIA_TYPE, headers=dict(_SSE_HEADERS))


def build_chat_result_sink(uow: AsyncUnitOfWork) -> Callable[[ChatOutcome], Awaitable[None]]:
    """对话结果汇工厂（组合根 lifespan 注入 chat_orchestrator；03 §3 步骤 8 UoW#2 短事务）。

    职责：assistant 消息落库（聚合方法分配 seq）+ run 终态审计事件（含 citations/usage，
    「有引用」的可回放留痕；citations 列=messages.citations jsonb 待 DDL 随 M4）。
    本函数是编排器（业务层）与 UoW 之间的注入边界——编排器自身不 import ORM/UoW。
    """

    async def sink(outcome: ChatOutcome) -> None:
        async with uow.for_tenant(outcome.tenant_id) as tx:
            session = await tx.sessions.get(outcome.session_id)
            if session is None:
                return
            seq = session.append_message("assistant", outcome.answer)  # assistant 不触发状态迁移
            await tx.sessions.append_message(
                outcome.session_id,
                Message(session_id=outcome.session_id, seq=seq, role="assistant", content=outcome.answer),
            )
            finished = outcome.error_code is None
            await tx.tasks.append_event(
                outcome.task_id,
                TaskEvent(
                    task_id=outcome.task_id,
                    event_type="run.finished" if finished else "run.error",
                    data={
                        "run_id": str(outcome.run_id),
                        "status": outcome.status,
                        "answer": outcome.answer,
                        "citations": outcome.citations,
                        "usage": outcome.usage,
                        "degraded": outcome.degraded,
                        "code": outcome.error_code,
                        "message": outcome.error_message,
                        "cost_ms": outcome.cost_ms,
                    },
                ),
            )

    return sink


@router.get("/{session_id}/messages", summary="历史消息（before_id 游标分页）")
async def list_messages(
    session_id: uuid.UUID,
    principal: SessionReadDep,
    uow: UowDep,
    before_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MessagePageOut:
    async with uow.for_tenant(principal.tenant_id) as tx:
        if await tx.sessions.get(session_id) is None:
            from services.platform.errors import GatewayError

            raise GatewayError(404, "会话不存在", status_code=404)
        # 多取一条探测后续页（游标分页）；游标语义见 SessionRepository.list_messages
        rows = await tx.sessions.list_messages(session_id, before_id=before_id, limit=limit + 1)
    has_more = len(rows) > limit
    page = rows[:limit]
    return MessagePageOut(
        items=[message_from_domain(m) for m in page],
        next_before_id=page[-1].id if has_more and page else None,
    )

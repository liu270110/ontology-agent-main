"""L2 sessions 路由（api/01 §5.2 契约，M1 子集 + 计划 3.2 SSE 主干）。

纪律：全部写路径经 UoW+聚合方法——禁裸 SQL、禁绕过聚合直改 status（03 §6.1 / 04 §2）；
send_message 适用豁免①（消息落库 + 任务受理同一事务，03 §6.1）；close 为生命周期迁移唯一入口
（04 §3 状态机，聚合方法 session.close()）。计划 3.2：POST messages 按 Accept 分流——
`text/event-stream` → SSE 事件流（编排器 stream_chat，事务外长流程），否则 202+{run_id}
（api/01 §6.1）；GET events = SSE 订阅/断线重连（Last-Event-ID，02 §5）。
"""

from __future__ import annotations

import inspect
import logging
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
    GroupMemberIn,
    MemberListOut,
    MemberUpdateIn,
    MessagePageOut,
    SendMessageIn,
    SessionCancelIn,
    SessionCreateIn,
    SessionListOut,
    SessionOut,
    SessionPatchIn,
    from_domain,
    member_from_domain,
    message_from_domain,
    to_domain,
)
from services.agent.business.chat_events import ChatCommand, ChatOutcome
from services.agent.business.chat_group import stream_group_turn
from services.agent.business.chat_orchestrator import build_chat_orchestrator
from services.agent.domain.model.agent import AgentError
from services.agent.domain.model.kernel_context import KernelEvent
from services.agent.domain.model.session import MemberRole, Message, RoutingMode, SessionError
from services.agent.domain.model.task import Task, TaskError, TaskEvent, TaskStatus
from services.memory.business.runtime import build_l1_store  # memory 公开装配面（memory.data 模块私有，P2-2 收口）
from services.platform.db.uow import AsyncUnitOfWork
from services.platform.deps import get_redis, get_session_factory
from services.platform.errors import GatewayError
from services.platform.schemas import PageMeta

router = APIRouter(prefix="/sessions", tags=["sessions"])

_SSE_MEDIA_TYPE = "text/event-stream"
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}  # 02 §5 响应头固定项

logger = logging.getLogger(__name__)


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


def _build_spill_store(settings: Any) -> Any:
    """spill 存储装配（02 §11.2-11）：task_spill_dir 未配置=关闭（None）；M4 切 MinIO 实现。"""
    spill_dir = getattr(settings, "task_spill_dir", None)
    if not spill_dir:
        return None
    from services.agent.data.spill_store import LocalDirSpillStore

    return LocalDirSpillStore(spill_dir)


def build_kernel_ledger_sink_factory(
    uow: AsyncUnitOfWork,
) -> Callable[[uuid.UUID, uuid.UUID], Callable[[KernelEvent], Awaitable[None]]]:
    """C1 内核账本投影工厂（组合根，2026-09-27 批）：kernel.* 锚点事件 → PG task_events 行。

    每轮对话经 factory(task_id, run_id) 取得闭包 sink；内核账本在终态前排水
    （先落库后终态，C1 可追溯口径）。落库失败由内核账本结构化转义（审计不阻断主流程）。
    本函数是编排器（业务层）与 UoW 之间的注入边界——编排器自身不 import ORM/UoW。
    投影 data 一致性注入（只补缺不覆盖）：run_id（对账四元组）+ trace_id（ocr 整改
    B-②：KernelEvent 顶层必填 trace_id 落进行 data，崩溃恢复合成行经 resume_repair
    回声投影行 trace，C2 链在崩溃恢复行上不断链；SSE 投影行不受影响）。
    """

    def factory(task_id: uuid.UUID, run_id: uuid.UUID) -> Callable[[KernelEvent], Awaitable[None]]:
        async def sink(event: KernelEvent) -> None:
            data = dict(event.data)
            data.setdefault("run_id", str(run_id))
            data.setdefault("trace_id", event.trace_id)  # C2：只补缺不覆盖，resume_repair 回声源
            async with uow.for_tenant(event.tenant_id) as tx:
                # H-0b 接线：approval_pending 锚点事件 → task.payload（审批呈现端点的核验锚，
                # approval_service PENDING_KEY 同款键；人工批准后 worker resume 通道携票消费该锚）
                if event.event_type == "kernel.approval_pending":
                    task = await tx.tasks.get(task_id)
                    if task is not None:
                        task.payload = {
                            **(task.payload or {}),
                            "approval_pending": {
                                "run_id": str(run_id),
                                "step_seq": data.get("step_seq"),
                                "param_hash": data.get("param_hash"),
                                "action_iri": data.get("action_iri"),
                                "execution_mode": data.get("execution_mode"),
                            },
                        }
                        await tx.tasks.save(task)
                await tx.tasks.append_event(
                    task_id,
                    TaskEvent(task_id=task_id, event_type=event.event_type, data=data),
                )

        return sink

    return factory


def build_llm_event_emitter_factory(
    uow: AsyncUnitOfWork, hub: Any = None
) -> Callable[[ChatCommand], Callable[[str, dict[str, Any]], Awaitable[None]]]:
    """M4.5-C llm.* 事件汇工厂（组合根，docs/Agent/12 §3）：llm.failover / llm.retry_* → task_events。

    每次对话经 factory(command) 取得闭包 emitter；编排器在 Run 生命周期内绑定
    （platform.llm.events ContextVar），模型韧性层（platform.llm.resilience）在降级/
    重试调度点调用。落库经 UoW 短事务（**先落库**），随后 hub 尽力推送（**后推送**；
    hub 二态：进程内 publish=同步二元组 / Redis Stream publish=协程，
    _chat_stream_response 同款收敛）。本函数是编排器（业务层）与 UoW/hub 之间的注入
    边界（build_kernel_ledger_sink_factory 先例）；落库/推送失败只告警——事件留痕不
    阻断模型调用（审计不阻塞主流程，02 §3 ⑥）。
    """

    def factory(command: ChatCommand) -> Callable[[str, dict[str, Any]], Awaitable[None]]:
        async def emit(event_type: str, data: dict[str, Any]) -> None:
            try:  # 先落库（task_events 只追加行；时间线端点取数口）
                async with uow.for_tenant(command.tenant_id) as tx:
                    await tx.tasks.append_event(
                        command.task_id,
                        TaskEvent(task_id=command.task_id, event_type=event_type, data=dict(data)),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("llm 事件落库失败（task=%s type=%s）: %s", command.task_id, event_type, exc)
            if hub is not None:  # 后推送（尽力；失败不影响已落库行）
                try:
                    published = hub.publish(command.session_id, event_type, dict(data))
                    if inspect.isawaitable(published):
                        await published
                except Exception as exc:  # noqa: BLE001
                    logger.warning("llm 事件推送失败（session=%s type=%s）: %s", command.session_id, event_type, exc)

        return emit

    return factory


def get_or_build_chat_orchestrator(state: Any) -> Any:
    """取/建对话编排器（模块级组合模式，同 kb.py get_model_port 先例；app.state 单例缓存）。

    组合内容：双适配器（builtin=ModelPort，claude=直连无 key 5002）+ 上下文组装器
    （kb 检索服务在 chat_context 工厂内装配）+ 结果汇（PG 短事务 UoW#2）。
    供端点请求（request.app.state）与 gateway lifespan（TaskRunWorker）共用同一装配面。
    M4.5-A：运行注册表 + estop 探针工厂随编排器装配（spawn 注册/终态注销；惰性单例）。
    """
    cached = getattr(state, "chat_orchestrator", None)
    if cached is not None:
        return cached
    settings = state.settings
    l1_store = getattr(state, "l1_store", None)
    if l1_store is None:
        l1_store = build_l1_store(get_redis(settings), ttl_seconds=settings.memory_l1_ttl_seconds)
        state.l1_store = l1_store
    from services.agent.api.control import get_or_build_estop_store, get_or_build_run_registry

    estop_store = get_or_build_estop_store(state, redis=get_redis(settings))  # Redis 优先、不可用内存兜底
    orchestrator = build_chat_orchestrator(
        model_port=getattr(state, "model_port", None),
        l1_store=l1_store,
        session_factory=get_session_factory(settings),
        ollama_base_url=settings.ollama_base_url,
        result_sink=build_chat_result_sink(state.uow),  # lifespan 装配于 app.state（06 §1）
        kernel_ledger_sink_factory=build_kernel_ledger_sink_factory(state.uow),  # C1 锚点投影（2026-09-27 批）
        llm_event_emitter_factory=build_llm_event_emitter_factory(
            state.uow, getattr(state, "sse_hub", None)
        ),  # M4.5-C：llm.* 事件 → task_events（先落库后推送）
        spill_store=_build_spill_store(settings),  # spill（02 §11.2-11）：未配置目录=关闭
        run_registry=get_or_build_run_registry(state),  # M4.5-A：运行中输入面注册表
        estop_probe_factory=estop_store.probe,  # M4.5-A：estop 步边界闸门探针工厂
    )
    state.chat_orchestrator = orchestrator
    return orchestrator


def _get_chat_orchestrator(request: Request) -> Any:
    """端点侧入口：转发到 state 级构建函数（worker 与请求共享同一编排器实例）。"""
    return get_or_build_chat_orchestrator(request.app.state)


@router.post("", status_code=status.HTTP_201_CREATED, summary="创建会话（绑定 agent）")
async def create_session(body: SessionCreateIn, principal: SessionWriteDep, uow: UowDep) -> SessionOut:
    """会话不变式前置校验（Agent 服务设计 §2）：agent 必须存在（404）且未禁用（disabled 不得被新会话引用，409）。"""
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            agent = await tx.agents.get(body.agent_id)
            if agent is None:
                raise GatewayError(404, "agent 不存在", status_code=404)
            agent.ensure_usable_for_new_session()
            session = to_domain(body, tenant_id=principal.tenant_id, user_id=principal.user_id)
            for m in session.members:  # 成员 agent 存在性校验（FK 之外的业务 404 口径）
                member_agent = await tx.agents.get(m.agent_id)
                if member_agent is None:
                    raise GatewayError(404, f"群成员 agent 不存在: {m.agent_id}", status_code=404)
                member_agent.ensure_usable_for_new_session()
            await tx.sessions.add(session, channel=body.channel)
    except AgentError as exc:
        raise GatewayError(409, str(exc), status_code=409) from exc
    return from_domain(session)


@router.get("", summary="当前用户会话列表（api/01 §3.1 信封）")
async def list_sessions(
    principal: SessionReadDep,
    uow: UowDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    session_type: Annotated[str | None, Query(alias="type", pattern="^(single|group)$")] = None,
) -> SessionListOut:
    """偏移分页改 page/page_size（B1 批，api/01 §3.1；offset=(page-1)*page_size 内部换算）。"""
    offset = (page - 1) * page_size
    async with uow.for_tenant(principal.tenant_id) as tx:
        items = await tx.sessions.list_for_user(
            principal.user_id, offset=offset, limit=page_size, session_type=session_type
        )
        total = await tx.sessions.count_for_user(principal.user_id, session_type=session_type)
    return SessionListOut(
        data=[from_domain(s) for s in items],
        meta=PageMeta(page=page, page_size=page_size, total=total),
    )


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


@router.get("/{session_id}/members", summary="群成员列表（27 篇 X15）")
async def list_members(session_id: uuid.UUID, principal: SessionReadDep, uow: UowDep) -> MemberListOut:
    async with uow.for_tenant(principal.tenant_id) as tx:
        session = await tx.sessions.get(session_id)
        if session is None:
            raise GatewayError(404, "会话不存在", status_code=404)
    return MemberListOut(items=[member_from_domain(m) for m in session.members])


@router.post("/{session_id}/members", status_code=status.HTTP_201_CREATED, summary="群成员添加（27 篇 X15）")
async def add_member(
    session_id: uuid.UUID, body: GroupMemberIn, principal: SessionWriteDep, uow: UowDep
) -> MemberListOut:
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            session = await tx.sessions.get(session_id)
            if session is None:
                raise GatewayError(404, "会话不存在", status_code=404)
            member_agent = await tx.agents.get(body.agent_id)
            if member_agent is None:
                raise GatewayError(404, "群成员 agent 不存在", status_code=404)
            member_agent.ensure_usable_for_new_session()
            session.add_member(
                agent_id=body.agent_id,
                display_name=body.display_name,
                system_prompt=body.system_prompt,
                model=body.model,
                routing_role=MemberRole(body.routing_role),
            )
            await tx.sessions.save_meta(session)  # 成员随 save_meta 同步（删全量插）
    except SessionError as exc:
        raise domain_error(exc, fallback_code=4103) from exc
    return MemberListOut(items=[member_from_domain(m) for m in session.members])


@router.patch("/{session_id}/members/{member_id}", summary="群成员更新（角色/模型/提示词）")
async def update_member(
    session_id: uuid.UUID,
    member_id: uuid.UUID,
    body: MemberUpdateIn,
    principal: SessionWriteDep,
    uow: UowDep,
) -> MemberListOut:
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            session = await tx.sessions.get(session_id)
            if session is None:
                raise GatewayError(404, "会话不存在", status_code=404)
            role = MemberRole(body.routing_role) if body.routing_role else None
            session.update_member(
                member_id,
                routing_role=role,
                model=body.model,
                system_prompt=body.system_prompt,
                display_name=body.display_name,
            )
            await tx.sessions.save_meta(session)
    except SessionError as exc:
        raise domain_error(exc, fallback_code=4103) from exc
    return MemberListOut(items=[member_from_domain(m) for m in session.members])


@router.delete("/{session_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT, summary="群成员移除")
async def remove_member(session_id: uuid.UUID, member_id: uuid.UUID, principal: SessionWriteDep, uow: UowDep) -> None:
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            session = await tx.sessions.get(session_id)
            if session is None:
                raise GatewayError(404, "会话不存在", status_code=404)
            session.remove_member(member_id)
            await tx.sessions.save_meta(session)
    except SessionError as exc:
        raise domain_error(exc, fallback_code=4103) from exc


@router.patch("/{session_id}", summary="会话元信息更新（routing 切换仅 group；不含归档）")
async def patch_session(
    session_id: uuid.UUID, body: SessionPatchIn, principal: SessionWriteDep, uow: UowDep
) -> SessionOut:
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            session = await tx.sessions.get(session_id)
            if session is None:
                raise GatewayError(404, "会话不存在", status_code=404)
            if body.routing is not None:
                session.set_routing(RoutingMode(body.routing))
            if body.title is not None:
                session.title = body.title
            await tx.sessions.save_meta(session)
    except SessionError as exc:
        raise domain_error(exc, fallback_code=4103) from exc
    return from_domain(session)


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除会话（硬删级联：消息/成员/任务/Run/事件；W1 缺口批追认前端）",
)
async def delete_session(session_id: uuid.UUID, principal: SessionWriteDep, uow: UowDep) -> None:
    """删除会话及其从属数据（契约源=SessionList.tsx DELETE + mocks/handlers 204；契约冻结
    2026-10-04 ①「级联删除会话消息与证据引用」）：

    - 级联面（本批不动表结构，FK 无 ondelete 逐表逆序删）：task_events → runs → tasks →
      messages（证据引用 citations/ag_ui_events 内嵌其中，随行清除）→ session_members → sessions；
    - 范围注记：outbox 投影行（enqueue_projection 已落库者）与本地 spill 文件不随删——前者
      为审计/投影留痕（04 §6.1 消费方处理），后者随 M4 MinIO spill 清理批；
    - 权限：会话所有者本人（user_id 比对，非所有者一律 404 防存在性探测）；幂等性无——
      二次删除 404（前端 deleteSession 对 204 空体按成功放行，见 SessionList 注释）。
    """
    async with uow.for_tenant(principal.tenant_id) as tx:
        session = await tx.sessions.get(session_id)
        if session is None or session.user_id != principal.user_id:
            raise GatewayError(404, "会话不存在", status_code=404)
        await tx.tasks.delete_by_session(session_id)  # FK 逆序：task_events→runs→tasks
        await tx.sessions.delete_cascade(session_id)  # messages→session_members→sessions
        # session.deleted 消费方=审计/检索下线（04 §6.1）；进程内缓冲，M4 起 outbox 同事务落库
        tx.enqueue_projection("session.deleted", session_id, {"session_id": str(session_id)})


@router.post(
    "/{session_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    summary="停止生成（标记 run 终态 cancelled；幂等——已终态同样 202；W1 缺口批追认前端）",
)
async def cancel_session_run(
    session_id: uuid.UUID, body: SessionCancelIn, principal: SessionWriteDep, uow: UowDep
) -> dict:
    """IX-CHT-06 停止生成（契约源=ChatPage.handleStop body {run_id} → 202；契约冻结 2026-10-04 ②）。

    语义=定位 run 并标记终态 cancelled（幂等：已终态/无活跃 run 同样 202，前端 .catch 兜底
    不依赖响应体）；无 run_manager 实例——进程内编排器中断（CancellationCoordinator 清单化
    传播）归执行编排 M3+（04 §3 cancelled 注记，同 tasks/cancel 端点口径），本端点只做
    聚合状态迁移与级联保存，worker 侧对已 cancelled 的 run 不再认领。

    定位：run_id 给定→按 run 反查任务（跨会话 404）；缺省→会话活跃任务（find_running_by_session）。
    """
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            if await tx.sessions.get(session_id) is None:
                raise GatewayError(404, "会话不存在", status_code=404)
            task = (
                await tx.tasks.find_by_run(body.run_id)
                if body.run_id is not None
                else await tx.tasks.find_running_by_session(session_id)
            )
            if task is not None and task.session_id != session_id:
                raise GatewayError(404, "run 不属于该会话", status_code=404)
            run = None
            if task is not None:
                if body.run_id is not None:
                    run = next((r for r in task.runs if r.id == body.run_id), None)
                else:
                    run = await tx.tasks.find_active_run(task.id)
                if run is not None and run.is_active:
                    if task.status is TaskStatus.RUNNING and task.active_run_id == run.id:
                        task.cancel()  # 聚合方法：活跃 Run cancelled + task cancelled（04 §3）
                    else:
                        run.cancel()
                    await tx.tasks.save(task)  # 级联保存 Run 终态
                    await tx.tasks.append_event(
                        task.id,
                        TaskEvent(
                            task_id=task.id,
                            event_type="run.cancelled",
                            data={"run_id": str(run.id), "source": "session.cancel"},
                        ),
                    )
                    tx.enqueue_projection(
                        "run.cancelled", task.id, {"task_id": str(task.id), "run_id": str(run.id)}
                    )
            effective_run_id = run.id if run is not None else body.run_id
    except TaskError as exc:
        raise domain_error(exc, fallback_code=4102) from exc
    return {"run_id": str(effective_run_id) if effective_run_id is not None else None, "status": "cancelled"}


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
    SSE 分流在受理事务**前**判定：SSE 路径受理即认领（run queued→running，04 §3），
    防 TaskRunWorker 对同一 queued Run 重复认领；非 SSE 路径保持 queued 交 worker。
    """
    wants_sse = request is not None and _SSE_MEDIA_TYPE in request.headers.get("accept", "")
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
            task = Task(
                tenant_id=principal.tenant_id,
                type="chat",
                session_id=session_id,
                agent_id=session.agent_id,  # 任务归属 agent（api/01 §5.1 DELETE /agents 占用检查依据）
                payload={"message_seq": seq},
            )
            run = task.start_run()  # 聚合方法：pending→running + 活跃 Run（queued）
            if wants_sse:
                run.start()  # 受理即认领（内联执行，04 §3「适配器 spawn 成功」）
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

    if not wants_sse:
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
    return _chat_stream_response(
        request,
        session_id,
        command,
        group_members=tuple(session.members),  # 受理事务内快照（27 篇 X15）
        group_routing=session.routing.value,
    )


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


def _chat_stream_response(
    request: Request,
    session_id: uuid.UUID,
    command: ChatCommand,
    *,
    group_members: tuple[Any, ...] = (),
    group_routing: str = "round_robin",
) -> StreamingResponse:
    """编排器事件流 → hub 发布 + 本连接直发（生产连接不消费自身队列，02 §5）。

    群聊会话（27 篇 X15）：先路由解算（mention/round_robin/all/orchestrator）→
    ROUTING_DECISION 审计事件 → 逐成员顺序执行（1 Task/1 Run，成员人格经 command 注入）。
    """
    hub = _get_hub(request)
    orchestrator = _get_chat_orchestrator(request)
    uow = request.app.state.uow
    settings = request.app.state.settings

    async def count_assistant() -> int:
        async with uow.for_tenant(command.tenant_id) as tx:
            return await tx.sessions.count_messages_by_role(session_id, "assistant")

    async def event_stream() -> AsyncIterator[bytes]:
        if group_members:
            source = stream_group_turn(
                orchestrator,
                command=command,
                members=group_members,
                routing=group_routing,
                count_assistant=count_assistant,
                resolve_model=getattr(request.app.state, "model_port", None),
            )
        else:
            source = orchestrator.stream_chat(command)
        async for event in source:
            # hub 形态二态：进程内 publish=同步二元组，Redis Stream publish=协程（双副本形态）——
            # 双副本压测（批次 B-①）暴露的形态差异 bug，统一在此收敛
            published = hub.publish(session_id, event.name.value, event.data)
            if inspect.isawaitable(published):
                published = await published
            _, frame = published
            yield frame

    _ = settings
    return StreamingResponse(event_stream(), media_type=_SSE_MEDIA_TYPE, headers=dict(_SSE_HEADERS))


def build_chat_result_sink(uow: AsyncUnitOfWork) -> Callable[[ChatOutcome], Awaitable[None]]:
    """对话结果汇工厂（组合根 lifespan 注入 chat_orchestrator；03 §3 步骤 8 UoW#2 短事务）。

    职责：assistant 消息落库（聚合方法分配 seq）+ run 终态审计事件（含 citations/usage，
    「有引用」的可回放留痕）+ **run/task 行终态回写**（04 §3 状态机收口：run completed/
    failed/timeout + usage；task succeeded；失败侧 retryable 且 attempt<3 保持 running——
    重试期间不落 failed，监督者=TaskRunWorker 继续，04 §3「Run failed 且重试耗尽」）。
    本函数是编排器（业务层）与 UoW 之间的注入边界——编排器自身不 import ORM/UoW。
    """

    async def sink(outcome: ChatOutcome) -> None:
        from services.agent.business.task_worker import finalize_outcome_on_task

        async with uow.for_tenant(outcome.tenant_id) as tx:
            session = await tx.sessions.get(outcome.session_id)
            if session is None:
                return
            # B-④ 联调修复：失败 run（error_code 非空）或空答案不落史——对齐编排器 L1
            # 「if outcome.answer」守卫口径（chat_orchestrator 步骤④回写）；此前失败路径
            # 无条件 append_message 产生空 assistant 行，污染历史消息流。run 终态审计
            # 事件与 task/run 行回写不受影响（失败仍留痕）。
            if outcome.error_code is None and outcome.answer:
                seq = session.append_message("assistant", outcome.answer)  # assistant 不触发状态迁移
                await tx.sessions.append_message(
                    outcome.session_id,
                    Message(
                        session_id=outcome.session_id,
                        seq=seq,
                        role="assistant",
                        agent_id=outcome.agent_id,  # 群聊发言归属（27 篇 X15）
                        content=outcome.answer,
                    ),
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
            task = await tx.tasks.get(outcome.task_id)  # 终态回写（run/task 行，04 §3 收口）
            if task is not None:
                finalize_outcome_on_task(task, outcome)
                await tx.tasks.save(task)

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

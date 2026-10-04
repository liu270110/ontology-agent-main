"""L2 运行中输入面路由（M4.5-A；docs/Agent/12-M4.5运行中输入面与模型韧性设计（主仓本地）§1.4）。

    POST   /sessions/{sid}/runs/{rid}/inbox  inbox 提交（三通道）          session:chat  202/404/409+4105/429+4203
    POST   /admin/estop                      紧急停止激活（覆盖写、幂等）   admin:write   202
    DELETE /admin/estop                      紧急停止解除（幂等）           admin:write   204
    GET    /admin/estop                      紧急停止状态视图               admin:read    200

纪律：inbox 提交是**同进程直达**（注册表命中→KernelInbox.submit→SSE 回执），注册表
未命中=Run 不在本进程（终态已注销/他副本执行）→ 409+4105 RUN_NOT_LOCAL；estop 语义
=闸门（只挡新工作，A-7），激活恒同步镜像内存（内核步边界探针零 Redis 依赖），Redis
写入 best-effort（不可用内存兜底，见 platform/ports/estop.py）。scope 先例=require_scope
（platform/deps）；admin:read/admin:write 为种子角色既有词表（迁移 c9e3a7f1b5d2）。
"""

from __future__ import annotations

import inspect
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status

from services.agent.api.deps import SessionChatDep, UowDep
from services.agent.api.schemas.control import EStopActivateIn, EStopStateOut, InboxSubmitIn, InboxSubmitOut
from services.agent.business.chat_events import ChatEventName
from services.agent.business.kernel.errors import KernelError
from services.agent.business.run_registry import RunRegistry
from services.platform.deps import Principal, require_scope
from services.platform.errors import ErrorCode, GatewayError
from services.platform.ports.estop import EStopStore, build_estop_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["control"])

EStopWriteDep = Annotated[Principal, Depends(require_scope("admin:write"))]
EStopReadDep = Annotated[Principal, Depends(require_scope("admin:read"))]


def _current_request(request: Request) -> Request:
    """Request 透传依赖：注册表/estop 存储取 app.state；直调用例可显式传桩。"""
    return request


RequestOptDep = Annotated[Request | None, Depends(_current_request)]


# ── 装配（app.state 惰性单例，get_or_build_chat_orchestrator 同款组合模式）────────
def get_or_build_run_registry(state: Any) -> RunRegistry:
    """进程内运行注册表（编排器 spawn 注册/终态注销；API inbox 提交取数口）。"""
    registry = getattr(state, "run_registry", None)
    if registry is None:
        registry = RunRegistry()
        state.run_registry = registry
    return registry


def get_or_build_estop_store(state: Any, *, redis: Any = None) -> EStopStore:
    """紧急停止存储（Redis 可缺省=内存兜底；惰性构建缓存 app.state，后到的客户端就地补挂）。"""
    store = getattr(state, "estop_store", None)
    if store is None:
        store = build_estop_store(redis)
        state.estop_store = store
    elif redis is not None and getattr(store, "_redis", None) is None:
        store.attach_redis(redis)
    return store


def _require_request(request: Request | None, what: str) -> Request:
    if request is None:
        raise GatewayError(5004, f"{what}未装配（直调路径缺 Request）", status_code=503)
    return request


# ── inbox 提交（§1.4 新端点）──────────────────────────────────────────────────────
@router.post(
    "/sessions/{session_id}/runs/{run_id}/inbox",
    status_code=status.HTTP_202_ACCEPTED,
    summary="运行中输入提交（followup/steer/inject 三通道；M4.5-A，§1.4）",
)
async def submit_run_inbox(
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    body: InboxSubmitIn,
    principal: SessionChatDep,
    uow: UowDep,
    request: RequestOptDep = None,
) -> dict:
    """提交一笔运行中输入：session 归属校验（租户内存在 + 活跃 Run 绑定）→ 注册表直达。

    409+4105=Run 不在本进程（终态已注销/他副本执行/与该会话活跃 Run 不符）；
    429+4203=收件箱容量超限（每 Run 待处理上限，Settings.kernel_inbox_max_per_run）。
    受理即发布 INBOX_SPLICED 回执（用户可见；hub 未装配仅记日志不阻断）。
    """
    req = _require_request(request, "运行注册表")
    async with uow.for_tenant(principal.tenant_id) as tx:
        if await tx.sessions.get(session_id) is None:
            raise GatewayError(404, "会话不存在", status_code=404)
        task = await tx.tasks.find_running_by_session(session_id)
        if task is None or task.active_run_id != run_id:
            raise GatewayError(ErrorCode.RUN_NOT_LOCAL, "RUN_NOT_LOCAL: 该会话无此活跃 Run", status_code=409)
    registry = get_or_build_run_registry(req.app.state)
    handle = registry.get(run_id)
    if handle is None:
        raise GatewayError(ErrorCode.RUN_NOT_LOCAL, "RUN_NOT_LOCAL: 运行不在本进程注册表", status_code=409)
    try:
        seq = handle.inbox.submit(body.kind, body.text, source=str(principal.user_id))
    except KernelError as exc:
        if exc.code == int(ErrorCode.INBOX_CAPACITY):
            raise GatewayError(exc.code, exc.message, status_code=429) from exc
        raise GatewayError(exc.code, exc.message, status_code=422) from exc
    await _publish_inbox_receipt(req, session_id, run_id, seq, body.kind, str(principal.user_id), body.text)
    out = InboxSubmitOut(run_id=str(run_id), seq=seq, kind=body.kind)
    return {"data": out.model_dump(), "meta": {}}


async def _publish_inbox_receipt(
    request: Request, session_id: uuid.UUID, run_id: uuid.UUID, seq: int, kind: str, source: str, text: str
) -> None:
    """SSE 回执（INBOX_SPLICED，用户可见）：hub 双形态（同步/协程 publish）同 sessions 先例；
    发布失败只告警不阻断（审计不阻塞主流程，02 §3 ⑥——账本侧 kernel.inbox_spliced 已留痕）。
    """
    hub = getattr(request.app.state, "sse_hub", None)
    if hub is None:
        logger.warning("SSE hub 未装配，INBOX_SPLICED 回执跳过（run=%s）", run_id)
        return
    try:
        published = hub.publish(
            session_id,
            ChatEventName.INBOX_SPLICED.value,
            {"run_id": str(run_id), "seq": seq, "kind": kind, "source": source, "text": text},
        )
        if inspect.isawaitable(published):
            await published
    except Exception as exc:  # noqa: BLE001 ——回执发布失败不阻断受理
        logger.warning("INBOX_SPLICED 回执发布失败（run=%s）: %s", run_id, exc)


# ── 紧急停止三端点（§1.2；A-7 只挡新工作）────────────────────────────────────────
@router.post(
    "/admin/estop",
    status_code=status.HTTP_202_ACCEPTED,
    summary="紧急停止激活（覆盖写、幂等；闸门语义=拒新工作，在途自然收敛）",
)
async def activate_estop(body: EStopActivateIn, principal: EStopWriteDep, request: RequestOptDep = None) -> dict:
    req = _require_request(request, "紧急停止存储")
    store = get_or_build_estop_store(req.app.state)
    await store.activate(principal.tenant_id, reason=body.reason, by=principal.user_id)
    out = EStopStateOut(tenant_id=str(principal.tenant_id), active=True, reason=body.reason, by=str(principal.user_id))
    return {"data": out.model_dump(), "meta": {}}


@router.delete(
    "/admin/estop",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="紧急停止解除（幂等；解除后新 Run 恢复受理）",
)
async def deactivate_estop(principal: EStopWriteDep, request: RequestOptDep = None) -> None:
    req = _require_request(request, "紧急停止存储")
    store = get_or_build_estop_store(req.app.state)
    await store.deactivate(principal.tenant_id)


@router.get("/admin/estop", summary="紧急停止状态视图（active=false 时详情为 None）")
async def get_estop_state(principal: EStopReadDep, request: RequestOptDep = None) -> dict:
    req = _require_request(request, "紧急停止存储")
    store = get_or_build_estop_store(req.app.state)
    reason = await store.active_reason(principal.tenant_id)
    detail = store.memory_entry(principal.tenant_id) if reason is not None else None
    out = EStopStateOut(
        tenant_id=str(principal.tenant_id),
        active=reason is not None,
        reason=reason,
        by=(detail or {}).get("by"),
        at=(detail or {}).get("at"),
    )
    return {"data": out.model_dump(), "meta": {}}

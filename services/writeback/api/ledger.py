"""writeback 台账 REST 薄路由（api/01 §5.8 ★ GET /admin/writeback/ledger/{id}；api/03 §3.9 REST 对应）。

薄路由纪律：三键定位/租户过滤/状态机前向迁移/未找到 404 语义全在 ``ActionDispatcher.status``
（业务回写设计权威），路由层零业务逻辑——MCP tool ``writeback.status``（api/03 §3.9 ★）与本
端点为**同一 dispatcher 单口径**（gateway lifespan 组合根挂 app.state.action_dispatcher）。

scope 裁决：本批按 api/03 §3.9 writeback.status ★ 行登记 ``action:invoke``（2026-09-27 M4 实现
暂按口径，独立 scope 待上游定稿）；api/01 §5.8 同路径行登记 admin:read——两处不一致随登记册
主持人裁决收敛（见模块报告「登记册欠账」节），路由只改 Depends 参数一行。

2026-09-28 补齐 §5.8 另两行（同 router，gateway 挂载点零改动）：
- GET  /admin/writeback/ledger              台账分页查询（status/needs_human 过滤）  admin:read  200
- POST /admin/writeback/ledger/{id}/dispose 人工处置（重发/标记冲正/关闭，§3.3）    admin:write 202 409*
scope 按契约行用 admin:read/admin:write；与既有详情端点 action:invoke 的并存口径
（同族台账查询面两种 scope）随登记册主持人裁决收敛，本批不擅改既有端点。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from services.platform.deps import Principal, require_scope
from services.platform.errors import GatewayError
from services.writeback.api.schemas.ledger import (
    LedgerStatusFilter,
    WritebackLedgerDisposeIn,
    WritebackLedgerOut,
    WritebackLedgerPageOut,
)
from services.writeback.domain.model import WritebackError

router = APIRouter(prefix="/admin/writeback", tags=["writeback"])

LedgerReadDep = Annotated[Principal, Depends(require_scope("action:invoke"))]  # 既有详情行（登记册欠账，留裁决）
LedgerListDep = Annotated[Principal, Depends(require_scope("admin:read"))]  # §5.8 台账查询行
LedgerDisposeDep = Annotated[Principal, Depends(require_scope("admin:write"))]  # §5.8 人工处置行

# WritebackError → 统一错误体 HTTP 映射（02 §7 段语义；404 为 api/03 §2 显式登记的「未找到」口径）
_WRITEBACK_HTTP_STATUS: dict[int, int] = {404: 404, 3001: 400, 3003: 409, 5003: 503, 5004: 503}


def _dispatcher(request: Request) -> Any:
    """回写查询面装配单例（lifespan 经 mcp.bootstrap 装配并挂 app.state；未装配=503）。"""
    dispatcher = getattr(request.app.state, "action_dispatcher", None)
    if dispatcher is None:
        raise GatewayError(5004, "回写查询面未装配（组合根缺位）", status_code=503)
    return dispatcher


def _domain_error(exc: WritebackError) -> GatewayError:
    """WritebackError → GatewayError：码原样透传（已登记段），HTTP 按段映射。"""
    return GatewayError(exc.code, exc.message, status_code=_WRITEBACK_HTTP_STATUS.get(exc.code, 500), detail=exc.detail)


@router.get(
    "/ledger/{ledger_id}",
    summary="回写台账单条详情（writeback.status 的 REST 对应；含 receipt 凭证与 attempts）",
)
async def get_writeback_ledger(
    ledger_id: uuid.UUID,
    principal: LedgerReadDep,
    request: Request,
) -> WritebackLedgerOut:
    """按 id 查本租户台账行（租户过滤在仓储层；跨租户一律「不存在」不泄露存在性）。"""
    try:
        result = await _dispatcher(request).status(tenant_id=principal.tenant_id, ledger_id=ledger_id)
    except WritebackError as exc:
        raise _domain_error(exc) from exc
    return WritebackLedgerOut(**result)


@router.get("/ledger", summary="回写台账分页查询（status/needs_human 过滤；§3.3 人工队列经 needs_human=true）")
async def list_writeback_ledger(
    principal: LedgerListDep,
    request: Request,
    status: Annotated[LedgerStatusFilter | None, Query()] = None,
    needs_human: Annotated[bool | None, Query()] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> WritebackLedgerPageOut:
    """分页列出本租户台账行（api/01 §5.8；租户过滤在仓储层，updated_at 倒序最新优先）。"""
    try:
        result = await _dispatcher(request).list_ledger(
            tenant_id=principal.tenant_id,
            status=status,
            needs_human=needs_human,
            offset=offset,
            limit=limit,
        )
    except WritebackError as exc:
        raise _domain_error(exc) from exc
    return WritebackLedgerPageOut(
        items=[WritebackLedgerOut(**item) for item in result["items"]],
        total=result["total"],
        offset=result["offset"],
        limit=result["limit"],
    )


@router.post(
    "/ledger/{ledger_id}/dispose",
    summary="人工处置：redispatch 重发（同幂等键新 attempt）/ mark_compensated 标记冲正 / close 关闭（§3.3）",
    status_code=202,
)
async def dispose_writeback_ledger(
    ledger_id: uuid.UUID,
    body: WritebackLedgerDisposeIn,
    principal: LedgerDisposeDep,
    request: Request,
) -> WritebackLedgerOut:
    """人工处置台账行（§3.3 三动作；处置人取 principal，处置理由/注记由 dispatcher 追加留痕）。

    202=处置受理（redispatch 的外部投递异步完成，响应体为落账后投影）；不可处置态 409（3003）、
    未找到 404、缺必填 note 3001——沿 _domain_error 单口径映射。
    """
    try:
        result = await _dispatcher(request).dispose(
            tenant_id=principal.tenant_id,
            ledger_id=ledger_id,
            action=body.action,
            note=body.note,
            actor_id=str(principal.user_id),
        )
    except WritebackError as exc:
        raise _domain_error(exc) from exc
    return WritebackLedgerOut(**result)

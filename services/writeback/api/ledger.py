"""writeback 台账 REST 薄路由（api/01 §5.8 ★ GET /admin/writeback/ledger/{id}；api/03 §3.9 REST 对应）。

薄路由纪律：三键定位/租户过滤/状态机前向迁移/未找到 404 语义全在 ``ActionDispatcher.status``
（业务回写设计权威），路由层零业务逻辑——MCP tool ``writeback.status``（api/03 §3.9 ★）与本
端点为**同一 dispatcher 单口径**（gateway lifespan 组合根挂 app.state.action_dispatcher）。

scope 裁决：本批按 api/03 §3.9 writeback.status ★ 行登记 ``action:invoke``（2026-09-27 M4 实现
暂按口径，独立 scope 待上游定稿）；api/01 §5.8 同路径行登记 admin:read——两处不一致随登记册
主持人裁决收敛（见模块报告「登记册欠账」节），路由只改 Depends 参数一行。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from services.platform.deps import Principal, require_scope
from services.platform.errors import GatewayError
from services.writeback.api.schemas.ledger import WritebackLedgerOut
from services.writeback.domain.model import WritebackError

router = APIRouter(prefix="/admin/writeback", tags=["writeback"])

LedgerReadDep = Annotated[Principal, Depends(require_scope("action:invoke"))]

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

"""tools 工具集市路由（docs/Agent/14 §3 端点契约；api/01 §5.6 tools 域登记口径；scope deny-by-default）。

    GET  /tools                     集市列表（query/通道/状态过滤+count）  tool:read   200
    GET  /tools/{tool_id}           详情（含语义标注/通道/版本/健康）      tool:read   200 / 404*
    POST /tools                     注册（清单校验→v1 直通 listed）       tool:write  201 / 4601、4602
    POST /tools/{tool_id}/lifecycle 生命周期动作（下架/恢复/撤销+审计行） tool:write  200 / 404*、4603

scope 采词：tool:read / tool:write 均为 iam 种子既有词表（m1 迁移 f0f79f84dce4 角色矩阵，
super_admin/admin/ontologist/member 持 read；super_admin/admin/member 持 write）——不新增词表。
分层：L2 router → tools.business（用例）→ {L4 Protocol, L6 仓储}；路由层零状态机/清单逻辑。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from services.platform.deps import Principal, SessionDep, require_scope
from services.platform.errors import GatewayError
from services.platform.kernel import DomainError
from services.platform.schemas import PageMeta
from services.tools.api.schemas.tool import (
    ToolCreateIn,
    ToolEnvelope,
    ToolLifecycleIn,
    ToolListEnvelope,
    ToolOut,
)
from services.tools.business.registry import ToolRegistryService
from services.tools.data.repo_impl.tool_repo import PgToolRepository
from services.tools.domain.model.tool_entry import SourceChannel, ToolStatus

router = APIRouter(prefix="/tools", tags=["tools"])

ToolReadDep = Annotated[Principal, Depends(require_scope("tool:read"))]
ToolWriteDep = Annotated[Principal, Depends(require_scope("tool:write"))]


# ---------------------------------------------------------------- 装配与异常映射


def _registry(db: SessionDep, principal: Principal) -> ToolRegistryService:
    """用例装配（每请求绑定请求会话；仓储构造期绑定租户——行级租户隔离）。"""
    return ToolRegistryService(repo=PgToolRepository(db, principal.tenant_id))


def _domain_error(exc: DomainError) -> GatewayError:
    """DomainError → 统一错误体：码取消息前缀（46xx tools 段），HTTP 按语义映射（plugin 先例同款）。"""
    message = str(exc)
    head = message[:4]
    code = int(head) if head.isdigit() else 4601
    status_code = {
        4601: 422,  # 清单校验拒绝（缺语义标注等，可修复）
        4602: 409,  # 同名工具已登记
        4603: 409,  # 非法状态迁移
    }.get(code, 409)
    return GatewayError(code, message, status_code=status_code)


def _not_found(exc: LookupError) -> GatewayError:
    return GatewayError(404, str(exc), status_code=404)


# ---------------------------------------------------------------- 端点


@router.get("", summary="集市列表（query/来源通道/状态过滤+count；api/01 §3.1 信封）")
async def list_tools(
    principal: ToolReadDep,
    db: SessionDep,
    query: Annotated[str | None, Query(max_length=128)] = None,
    source_channel: Annotated[SourceChannel | None, Query(alias="channel")] = None,
    status_filter: Annotated[ToolStatus | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ToolListEnvelope:
    registry = _registry(db, principal)
    items, total = await registry.list_tools(
        query=query, source_channel=source_channel, status=status_filter, page=page, page_size=page_size
    )
    return ToolListEnvelope(
        data=[ToolOut.from_domain(e) for e in items], meta=PageMeta(page=page, page_size=page_size, total=total)
    )


@router.get("/{tool_id}", summary="详情（含语义标注/来源通道 L0~L3/版本/健康提示）")
async def get_tool(tool_id: uuid.UUID, principal: ToolReadDep, db: SessionDep) -> ToolEnvelope:
    registry = _registry(db, principal)
    try:
        entry = await registry.get(tool_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return ToolEnvelope(data=ToolOut.from_domain(entry))


@router.post("", status_code=201, summary="注册工具条目（清单校验→v1 直通 listed；14 §2）")
async def create_tool(
    body: ToolCreateIn, principal: ToolWriteDep, db: SessionDep, request: Request
) -> ToolEnvelope:
    registry = _registry(db, principal)
    try:
        entry = await registry.register(
            tenant_id=principal.tenant_id,
            name=body.name,
            action_iri=body.action_iri,
            source_channel=body.source_channel,
            semantic_annotation=body.semantic_annotation,
            version=body.version,
            health_hint=body.health_hint,
            evidence_uri=body.evidence_uri,
            registrant_id=principal.user_id,
            trace_id=getattr(request.state, "trace_id", "") or "",
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return ToolEnvelope(data=ToolOut.from_domain(entry))


@router.post("/{tool_id}/lifecycle", summary="生命周期动作（下架/恢复/撤销；迁移合法+审计行，14 §2）")
async def tool_lifecycle(
    tool_id: uuid.UUID, body: ToolLifecycleIn, principal: ToolWriteDep, db: SessionDep, request: Request
) -> ToolEnvelope:
    registry = _registry(db, principal)
    try:
        entry = await registry.lifecycle(
            tool_id=tool_id,
            action=body.action,
            reason=body.reason,
            operator_id=principal.user_id,
            trace_id=getattr(request.state, "trace_id", "") or "",
        )
    except LookupError as exc:
        raise _not_found(exc) from exc
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return ToolEnvelope(data=ToolOut.from_domain(entry))

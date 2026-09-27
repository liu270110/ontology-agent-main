"""plugin 市场路由（api/01 §5.6 plugins 与 tools 登记册口径；scope deny-by-default）。

    GET  /plugins                    市场列表            plugin:read     200
    GET  /plugins/{id}               详情（含版本树）    plugin:read     200 / 404*
    POST /plugins                    上传插件包登记      plugin:write    201 / 3001
    POST /plugins/{id}/versions      新增版本（版本管理；登记册外补充端点，漂移见模块报告）
    POST /plugins/{id}/submit        提交上架审核        review:submit   202 / 4701
    POST /plugins/{id}/install       安装已发布版本      plugin:install  202 / 45xx
    POST /plugins/{id}/enable        启用                plugin:admin    200 / 45xx
    POST /plugins/{id}/disable       停用                plugin:admin    200 / 45xx

审批决策 REST 面归 admin 路由（api/01 §5.8 POST /admin/reviews/{id}/decision）——本批不建
未登记端点，编排经 PluginMarketService.review_decision（测试覆盖三档全链）。
分层：L2 router → plugin.business（用例）→ {L4 Protocol, L6 仓储}；路由层零状态机/门禁逻辑。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from services.platform.deps import Principal, SessionDep, require_scope
from services.platform.errors import GatewayError
from services.platform.kernel import DomainError
from services.plugin.api.schemas.plugin import (
    InstallOut,
    PluginCreateIn,
    PluginDetailOut,
    PluginOut,
    PluginPageOut,
    PluginVersionIn,
    PluginVersionOut,
    SubmitIn,
    SubmitOut,
    ToggleOut,
    ToolBindingOut,
)
from services.plugin.business.lifecycle import PluginMarketService
from services.plugin.data.repo_impl.plugin_repo import PgPluginRepository, PgToolBindingRepository
from services.plugin.domain.model.plugin import PluginKind, PluginStatus
from services.plugin.domain.repo.review_port import PluginReviewPort, ReviewDecisionPort

router = APIRouter(prefix="/plugins", tags=["plugins"])

PluginReadDep = Annotated[Principal, Depends(require_scope("plugin:read"))]
PluginWriteDep = Annotated[Principal, Depends(require_scope("plugin:write"))]
ReviewSubmitDep = Annotated[Principal, Depends(require_scope("review:submit"))]
PluginInstallDep = Annotated[Principal, Depends(require_scope("plugin:install"))]
PluginAdminDep = Annotated[Principal, Depends(require_scope("plugin:admin"))]


# ---------------------------------------------------------------- 装配与异常映射


def _market(request: Request, db: SessionDep, principal: Principal) -> PluginMarketService:
    """用例装配（每请求绑定请求会话；审批/工单端口为 lifespan 单例）。"""
    runtime = getattr(request.app.state, "plugin_runtime", None)
    review = getattr(request.app.state, "plugin_review", None)
    approvals = getattr(request.app.state, "review_approvals", None)
    if not isinstance(review, PluginReviewPort) or not isinstance(approvals, ReviewDecisionPort):
        raise GatewayError(5004, "审核工单端口未装配", status_code=503)
    return PluginMarketService(
        repo=PgPluginRepository(db, principal.tenant_id),
        bindings=PgToolBindingRepository(db, principal.tenant_id),
        review=review,
        approvals=approvals,
        runtime=runtime,
    )


def _domain_error(exc: DomainError) -> GatewayError:
    """DomainError → 统一错误体：码取消息前缀（45xx plugin / 47xx review 段），HTTP 按段映射。"""
    message = str(exc)
    head = message[:4]
    code = int(head) if head.isdigit() else 4501
    status_code = {4503: 422, 4506: 503, 4701: 409, 4702: 403, 4703: 409}.get(code, 409)
    return GatewayError(code, message, status_code=status_code)


def _conflict(exc: Exception) -> GatewayError:
    return GatewayError(4501, str(exc), status_code=409)


def _not_found(exc: LookupError) -> GatewayError:
    return GatewayError(404, str(exc), status_code=404)


# ---------------------------------------------------------------- 端点


@router.get("", summary="市场列表")
async def list_plugins(
    principal: PluginReadDep,
    db: SessionDep,
    request: Request,
    status_filter: Annotated[PluginStatus | None, Query(alias="status")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PluginPageOut:
    market = _market(request, db, principal)
    plugins = await market.list_market(status=status_filter, offset=offset, limit=limit)
    return PluginPageOut(items=[PluginOut.from_domain(p) for p in plugins], offset=offset, limit=limit)


@router.get("/{plugin_id}", summary="详情（含版本树）")
async def get_plugin(
    plugin_id: uuid.UUID, principal: PluginReadDep, db: SessionDep, request: Request
) -> PluginDetailOut:
    market = _market(request, db, principal)
    try:
        plugin, versions = await market.get_detail(plugin_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    return PluginDetailOut(
        **PluginOut.from_domain(plugin).model_dump(), versions=[PluginVersionOut.from_domain(v) for v in versions]
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="上传插件包（登记 draft + 首版本）")
async def create_plugin(
    body: PluginCreateIn, principal: PluginWriteDep, db: SessionDep, request: Request
) -> PluginDetailOut:
    market = _market(request, db, principal)
    try:
        plugin, _ver = await market.create_listing(
            tenant_id=principal.tenant_id,
            publisher_id=principal.user_id,
            slug=body.slug,
            name=body.name,
            kind=PluginKind(body.kind),
            version=body.version,
            server_json=body.server_json,
            artifact_key=body.artifact_key,
            checksum=body.checksum,
            scope_required=tuple(body.scope_required),
            compat_mcp=body.compat_mcp,
        )
        _plugin, versions = await market.get_detail(plugin.id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except DomainError as exc:
        raise _domain_error(exc) from exc
    except Exception as exc:
        raise _conflict(exc) from exc
    return PluginDetailOut(
        **PluginOut.from_domain(plugin).model_dump(), versions=[PluginVersionOut.from_domain(v) for v in versions]
    )


@router.post("/{plugin_id}/versions", status_code=status.HTTP_201_CREATED, summary="新增版本（版本管理）")
async def add_plugin_version(
    plugin_id: uuid.UUID, body: PluginVersionIn, principal: PluginWriteDep, db: SessionDep, request: Request
) -> PluginVersionOut:
    market = _market(request, db, principal)
    try:
        ver = await market.add_version(
            plugin_id=plugin_id,
            version=body.version,
            server_json=body.server_json,
            artifact_key=body.artifact_key,
            checksum=body.checksum,
            scope_required=tuple(body.scope_required),
            compat_mcp=body.compat_mcp,
        )
    except LookupError as exc:
        raise _not_found(exc) from exc
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return PluginVersionOut.from_domain(ver)


@router.post("/{plugin_id}/submit", status_code=status.HTTP_202_ACCEPTED, summary="提交上架审核（门禁 1 前置）")
async def submit_plugin(
    plugin_id: uuid.UUID, body: SubmitIn, principal: ReviewSubmitDep, db: SessionDep, request: Request
) -> SubmitOut:
    market = _market(request, db, principal)
    try:
        ticket_id = await market.submit_for_review(
            tenant_id=principal.tenant_id,
            plugin_id=plugin_id,
            version_id=body.version_id,
            submitter_id=principal.user_id,
            trace_id=getattr(request.state, "trace_id", "") or "",
        )
    except LookupError as exc:
        raise _not_found(exc) from exc
    except DomainError as exc:
        raise _domain_error(exc) from exc
    plugin, versions = await market.get_detail(plugin_id)
    ver = next((v for v in versions if v.id == body.version_id), None)
    if ver is None:  # 理论不可达（submit 已校验）
        raise GatewayError(404, "插件版本不存在", status_code=404)
    return SubmitOut(ticket_id=ticket_id, plugin_status=plugin.status, version_status=ver.status.value)


@router.post("/{plugin_id}/install", status_code=status.HTTP_202_ACCEPTED, summary="安装已发布版本")
async def install_plugin(
    plugin_id: uuid.UUID, principal: PluginInstallDep, db: SessionDep, request: Request, version: str | None = None
) -> InstallOut:
    market = _market(request, db, principal)
    try:
        bindings = await market.install(tenant_id=principal.tenant_id, plugin_id=plugin_id, version=version)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return InstallOut(tools=[ToolBindingOut.from_domain(b) for b in bindings])


@router.post("/{plugin_id}/enable", summary="启用（published↔suspended 的 enable 向）")
async def enable_plugin(plugin_id: uuid.UUID, principal: PluginAdminDep, db: SessionDep, request: Request) -> ToggleOut:
    market = _market(request, db, principal)
    try:
        bindings = await market.enable(tenant_id=principal.tenant_id, plugin_id=plugin_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return ToggleOut(tools=[ToolBindingOut.from_domain(b) for b in bindings])


@router.post("/{plugin_id}/disable", summary="停用（published→suspended；tools.enabled=false 持久真相）")
async def disable_plugin(
    plugin_id: uuid.UUID, principal: PluginAdminDep, db: SessionDep, request: Request
) -> ToggleOut:
    market = _market(request, db, principal)
    try:
        bindings = await market.disable(tenant_id=principal.tenant_id, plugin_id=plugin_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return ToggleOut(tools=[ToolBindingOut.from_domain(b) for b in bindings])

"""L2 网关 · mcp 管理域路由（api/01 §5.7 行 + §5.7 ★ 预登记行；mock platform-handlers.ts §5.7 契约）。

    POST   /mcp/discover                 预注册发现（tools/list 预览，不落库）   mcp:write  200/502
    GET    /mcp/servers                  外部 Server 列表                        mcp:read   200
    POST   /mcp/servers                  上架（探测失败不上架）                  mcp:write  201/422、502
    GET    /mcp/servers/{id}             详情                                    mcp:read   200/404
    POST   /mcp/servers/{id}/refresh     重新探测更新工具缓存                    mcp:write  200/502
    GET    /mcp/servers/{id}/tools       该 Server 工具清单                      mcp:read   200/404
    POST   /mcp/tools/{tool_id}/enable   审核开启外部工具（默认不可信）          mcp:write  200/404
    POST   /mcp/tools/{tool_id}/disable  审核工具回退停用（★ 预登记）            mcp:write  200/404
    DELETE /mcp/servers/{id}             下架（级联其工具行；★ 预登记）          mcp:write  204/404

scope 按 api/01 §5.7 声明（mcp:read/mcp:write 为本批迁移 b2c4d6f8a1e3 新种子，super_admin/admin）。
审计纪律：全部写操作由网关 AuditLogMiddleware 自动落 audit_logs（admin.py 同款，本文件零审计代码）。
业务薄壳：编排=services/mcp/business/management.py，状态推进=mcp_repo；探测传输经
app.state.mcp_client_factory 注入（组合根可选装配位，缺省 fastmcp 真传输；测试注入内存桩零网络）。
已知形状差异见 schemas/management.py 头注。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from services.mcp.api.schemas.management import (
    DiscoverIn,
    DiscoverOut,
    McpServerRow,
    McpToolRow,
    ProbeWindowOut,
    RefreshOut,
    ServerCreatedOut,
    ServerCreateIn,
    ServerListOut,
    ServerToolsListOut,
    ToolEnabledOut,
)
from services.mcp.business import management
from services.mcp.business import probe as probe_mod
from services.mcp.data.repo_impl import mcp_repo
from services.platform.deps import Principal, SessionDep, require_scope
from services.platform.errors import GatewayError

router = APIRouter(prefix="/mcp", tags=["mcp"])

McpReadDep = Annotated[Principal, Depends(require_scope("mcp:read"))]
McpWriteDep = Annotated[Principal, Depends(require_scope("mcp:write"))]


# ---------------------------------------------------------------- 投影（mock McpServer/McpTool 逐字段）


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt is not None else None


def _tool_out(row) -> McpToolRow:
    return McpToolRow(
        tool_id=row.tool_id,
        name=row.name,
        desc=row.desc,
        write=row.write,
        read_only=row.read_only,
        adopted=row.adopted,
        enabled=row.enabled,
    )


def _server_out(row, tools) -> McpServerRow:
    return McpServerRow(
        id=str(row.id),
        name=row.name,
        desc=row.desc,
        transport="streamable http" if row.transport == "streamable_http" else row.transport,
        # url 掩码（probe.mask_url）或 stdio 展示串（mock url_masked/command 双形态）
        url_masked=probe_mod.mask_url(row.url or "")
        if row.transport == "streamable_http"
        else f"stdio · {row.command or ''}",
        command=row.command if row.transport == "stdio" else None,
        auth=row.auth,
        token_masked=row.token_masked,
        protocol=row.protocol,
        server_version=row.server_version,
        status=row.status,
        latency_ms=row.latency_ms,
        consecutive_failures=row.consecutive_failures,
        last_probe=_iso(row.last_probe),
        probes_24h=[ProbeWindowOut(ok=bool(item.get("ok"))) for item in (row.probes_24h or [])],
        adopted_count=row.adopted_count,
        discovered_count=row.discovered_count,
        added_by=row.added_by,
        added_at=_iso(row.created_at) or "",
        tools=[_tool_out(t) for t in tools],
    )


async def _tools_of(db: AsyncSession, server_id: uuid.UUID) -> list:
    return await mcp_repo.tools_of_server(db, server_id=server_id)


def _factory(request: Request):
    return management.client_factory_of(request.app.state)


# ================================================================ discover（★ 预登记）


@router.post(
    "/discover", response_model=DiscoverOut, summary="预注册发现（连接目标+tools/list 预览，不落库；失败 5003/502）"
)
async def discover(body: DiscoverIn, principal: McpWriteDep, request: Request) -> DiscoverOut:
    return await management.discover(body, client_factory=_factory(request))


# ================================================================ servers（§5.7）


@router.get("/servers", response_model=ServerListOut, summary="外部 Server 列表（最新在前；tools 内嵌全量缓存行）")
async def list_servers(principal: McpReadDep, db: SessionDep) -> ServerListOut:
    rows = await mcp_repo.list_servers(db, tenant_id=principal.tenant_id)
    items = [_server_out(row, await _tools_of(db, row.id)) for row in rows]
    return ServerListOut(items=items)


@router.post(
    "/servers",
    response_model=ServerCreatedOut,
    status_code=201,
    summary="上架外部 Server（探测失败不上架 5003；adopt_tool_ids=discover 勾选集）",
)
async def create_server(
    body: ServerCreateIn, principal: McpWriteDep, request: Request, db: SessionDep
) -> ServerCreatedOut:
    server, tools = await management.register_server(
        db, tenant_id=principal.tenant_id, user_id=principal.user_id, body=body, client_factory=_factory(request)
    )
    return ServerCreatedOut(**_server_out(server, tools).model_dump(), token_sentinel=True)


@router.get(
    "/servers/{server_id}", response_model=McpServerRow, summary="Server 详情（api/01 §5.7 ★；跨租户同口径 404）"
)
async def get_server(server_id: uuid.UUID, principal: McpReadDep, db: SessionDep) -> McpServerRow:
    row = await mcp_repo.get_server(db, tenant_id=principal.tenant_id, server_id=server_id)
    if row is None:
        raise GatewayError(404, "Server 不存在", status_code=404)
    return _server_out(row, await _tools_of(db, row.id))


@router.post(
    "/servers/{server_id}/refresh",
    response_model=RefreshOut,
    summary="重新探测并更新工具缓存（api/01 §5.7；失败落 failing 后 5003/502）",
)
async def refresh_server(
    server_id: uuid.UUID, principal: McpWriteDep, request: Request, db: SessionDep
) -> RefreshOut:
    server, tools = await management.refresh_server(
        db, tenant_id=principal.tenant_id, server_id=server_id, client_factory=_factory(request)
    )
    return RefreshOut(
        latency_ms=server.latency_ms, tools=[_tool_out(t) for t in tools], discovered_count=server.discovered_count
    )


@router.get(
    "/servers/{server_id}/tools", response_model=ServerToolsListOut, summary="该 Server 的工具清单（api/01 §5.7）"
)
async def list_server_tools(server_id: uuid.UUID, principal: McpReadDep, db: SessionDep) -> ServerToolsListOut:
    row = await mcp_repo.get_server(db, tenant_id=principal.tenant_id, server_id=server_id)
    if row is None:
        raise GatewayError(404, "Server 不存在", status_code=404)
    tools = await _tools_of(db, row.id)
    return ServerToolsListOut(items=[_tool_out(t) for t in tools])


@router.delete(
    "/servers/{server_id}",
    status_code=204,
    summary="下架 Server（api/01 §5.7 ★；级联其工具行，X-Removed-Tools/X-Affected-Agents 提示头）",
)
async def delete_server(server_id: uuid.UUID, principal: McpWriteDep, db: SessionDep) -> Response:
    _server, removed_tools, affected_agents = await management.delete_server(
        db, tenant_id=principal.tenant_id, server_id=server_id
    )
    return Response(
        status_code=204,
        headers={"X-Removed-Tools": str(removed_tools), "X-Affected-Agents": str(affected_agents)},
    )


# ================================================================ tools 启停（§5.7 + ★ disable）


@router.post(
    "/tools/{tool_id}/enable",
    response_model=ToolEnabledOut,
    summary="审核开启外部工具（外部默认不可信；api/01 §5.7）",
)
async def enable_tool(tool_id: str, principal: McpWriteDep, db: SessionDep) -> ToolEnabledOut:
    tool = await management.set_tool_enabled(db, tenant_id=principal.tenant_id, tool_id=tool_id, enabled=True)
    return ToolEnabledOut(tool_id=tool.tool_id, enabled=tool.enabled)


@router.post(
    "/tools/{tool_id}/disable",
    response_model=ToolEnabledOut,
    summary="审核工具回退停用（enable 反向；api/01 §5.7 ★ 预登记）",
)
async def disable_tool(tool_id: str, principal: McpWriteDep, db: SessionDep) -> ToolEnabledOut:
    tool = await management.set_tool_enabled(db, tenant_id=principal.tenant_id, tool_id=tool_id, enabled=False)
    return ToolEnabledOut(tool_id=tool.tool_id, enabled=tool.enabled)

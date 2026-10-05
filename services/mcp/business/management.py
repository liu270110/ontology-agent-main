"""mcp 管理域用例编排（L5）：discover / 上架 / 刷新 / 下架 / 工具启停。

传输探测=probe.py（client_factory 注入点，测试桩同接口零网络）；持久化=mcp_repo；
探测失败族（超时/不可达/协议错误）统一 5003/502 结构化错误且不上架（mock refresh
err(5003,'Server 探活失败',502) 同语义；api/01 POST /mcp/servers 主要错误码 3001、5003）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from services.mcp.api.schemas.management import DiscoverIn, DiscoverOut, ProbeToolOut, ServerCreateIn
from services.mcp.business import probe as probe_mod
from services.mcp.business.probe import ClientFactory, ProbeReport
from services.mcp.data.orm import McpServerORM, McpToolORM
from services.mcp.data.repo_impl import mcp_repo
from services.platform.errors import ErrorCode, GatewayError


def client_factory_of(app_state: Any) -> ClientFactory | None:
    """组合根装配位（app.state.mcp_client_factory；未装配=None→default_client_factory）。"""
    return getattr(app_state, "mcp_client_factory", None)


def _build_target_or_422(**kw: Any):
    """请求字段 → McpTargetConfig（词表/http(s)/stdio 必填项校验；ValueError→3001/422）。"""
    try:
        return probe_mod.build_target(**kw)
    except ValueError as exc:
        raise GatewayError(ErrorCode.PARAM_INVALID, str(exc), status_code=422) from exc


def _probe_failed(exc: Exception, server_name: str) -> GatewayError:
    return GatewayError(
        ErrorCode.MCP_TARGET_UNAVAILABLE,
        f"Server 探活失败: {server_name}（{exc}）",
        status_code=502,
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _probe(target, *, server_name: str, client_factory: ClientFactory | None) -> ProbeReport:
    try:
        return await probe_mod.probe_target(target, client_factory=client_factory)
    except Exception as exc:  # noqa: BLE001 ——外部协议异常族不稳定，统一 5003/502（connectors 同口径）
        raise _probe_failed(exc, server_name) from exc


# ---------------------------------------------------------------- discover（不落库）


async def discover(body: DiscoverIn, *, client_factory: ClientFactory | None = None) -> DiscoverOut:
    """预注册发现：tools/list 预览（不落库；失败 5003/502——IX-MCP-01「先测后注册」第①步）。"""
    target = _build_target_or_422(
        name=body.name, transport=body.transport, url=body.url, command=body.command, auth=body.auth, token=body.token
    )
    report = await _probe(target, server_name=body.name, client_factory=client_factory)
    return DiscoverOut(
        latency_ms=report.latency_ms,
        protocol=report.protocol_version,
        server_version=report.server_version,
        tools=_discovered_rows(report),
    )


def _discovered_rows(report: ProbeReport) -> list[ProbeToolOut]:
    rows: list[ProbeToolOut] = []
    for remote in report.tools:
        name = str(remote.get("name") or "")
        if not name:
            continue
        write, read_only = probe_mod.derive_tool_flags(dict(remote.get("annotations") or {}))
        rows.append(
            ProbeToolOut(
                tool_id=probe_mod.discover_tool_id(name),
                name=name,
                desc=str(remote.get("description") or ""),
                write=write,
                read_only=read_only,
            )
        )
    return rows


# ---------------------------------------------------------------- 上架（探测失败不上架）


async def register_server(
    db: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID, body: ServerCreateIn,
    client_factory: ClientFactory | None = None,
) -> tuple[McpServerORM, list[McpToolORM]]:
    """上架：校验→重探（真实必要：登记必须验证可达并拉工具全集）→落库。

    adopt_tool_ids=discover 返回的 nd- 短 id 勾选集：命中者 adopted=true（enabled 恒
    false=外部默认不可信，审核开启走 enable 端点）；未勾选工具同样入缓存
    （adopted=false），对账远端真集。返回 (server 行, 全量工具行)。
    """
    target = _build_target_or_422(
        name=body.name, transport=body.transport, url=body.url, command=body.command, auth=body.auth, token=body.token
    )
    if await mcp_repo.server_name_taken(db, tenant_id=tenant_id, name=body.name):
        raise GatewayError(ErrorCode.PARAM_INVALID, f"Server 名已存在: {body.name}", status_code=422)
    report = await _probe(target, server_name=body.name, client_factory=client_factory)

    adopt_ids = set(body.adopt_tool_ids)
    server = McpServerORM(
        tenant_id=tenant_id,
        name=body.name,
        desc=body.desc.strip(),
        transport=target.transport,
        url=target.url,
        command=target.command,
        args=list(target.args),
        env=dict(target.env),
        auth=body.auth.strip() or "none",
        token_masked=probe_mod.mask_token(body.token),
        token_secret=body.token,
        protocol=report.protocol_version,
        server_version=report.server_version,
        status="unknown",  # mock 同口径：上架成功仍待刷新确认（refresh 成功才 healthy）
        latency_ms=report.latency_ms,
        consecutive_failures=0,
        last_probe=_utcnow(),  # mock 同口径：登记时刻即最近探测（登记探测本身）
        probes_24h=[],
        adopted_count=0,
        discovered_count=0,
        added_by=await mcp_repo.user_display_name(db, user_id=user_id),
    )
    db.add(server)
    await db.flush()  # uuid7 主键在 INSERT 时赋值；工具行 server_id 需先取 id
    tools: list[McpToolORM] = []
    for remote in report.tools:
        name = str(remote.get("name") or "")
        if not name:
            continue
        write, read_only = probe_mod.derive_tool_flags(dict(remote.get("annotations") or {}))
        adopted = probe_mod.discover_tool_id(name) in adopt_ids
        tools.append(
            McpToolORM(
                tenant_id=tenant_id,
                server_id=server.id,
                tool_id=probe_mod.persisted_tool_id(body.name, name),
                name=name,
                desc=str(remote.get("description") or ""),
                write=write,
                read_only=read_only,
                adopted=adopted,
                enabled=False,
            )
        )
    server.adopted_count = sum(1 for t in tools if t.adopted)
    server.discovered_count = len(tools)
    await mcp_repo.create_server_with_tools(db, server=server, tools=tools)
    return server, tools


# ---------------------------------------------------------------- 刷新（失败落 failing 再上抛）


async def refresh_server(
    db: AsyncSession, *, tenant_id: uuid.UUID, server_id: uuid.UUID,
    client_factory: ClientFactory | None = None,
) -> tuple[McpServerORM, list[McpToolORM]]:
    """重新探测并更新工具缓存；失败推进 failing 状态后抛 5003/502（状态不因异常路径丢失）。"""
    server = await mcp_repo.get_server(db, tenant_id=tenant_id, server_id=server_id)
    if server is None:
        raise GatewayError(404, "Server 不存在", status_code=404)
    target = _build_target_or_422(
        name=server.name,
        transport=server.transport,
        url=server.url,
        command=server.command,
        auth=server.auth,
        token=server.token_secret,
    )
    try:
        report = await probe_mod.probe_target(target, client_factory=client_factory)
    except Exception as exc:
        await mcp_repo.record_probe_failure(db, server=server)
        raise _probe_failed(exc, server.name) from exc
    tools = await mcp_repo.refresh_tool_cache(db, server=server, remote_tools=report.tools)
    await mcp_repo.record_probe_success(
        db,
        server=server,
        latency_ms=report.latency_ms,
        protocol=report.protocol_version,
        server_version=report.server_version,
        discovered_count=len(tools),
    )
    return server, tools


# ---------------------------------------------------------------- 下架 / 启停 / 读取


async def delete_server(
    db: AsyncSession, *, tenant_id: uuid.UUID, server_id: uuid.UUID
) -> tuple[McpServerORM, int, int]:
    """下架：级联工具行。返回 (server 行, 删除工具数, 在用 Agent 提示数)。"""
    server = await mcp_repo.get_server(db, tenant_id=tenant_id, server_id=server_id)
    if server is None:
        raise GatewayError(404, "Server 不存在", status_code=404)
    removed_tools = await mcp_repo.delete_server(db, server=server)
    affected_agents = await mcp_repo.affected_agents_count(db, tenant_id=tenant_id, server_name=server.name)
    return server, removed_tools, affected_agents


async def set_tool_enabled(
    db: AsyncSession, *, tenant_id: uuid.UUID, tool_id: str, enabled: bool
) -> McpToolORM:
    """工具级审核启停（外部默认不可信：纳管后 enabled=false，逐项开启）。"""
    tool = await mcp_repo.get_tool(db, tenant_id=tenant_id, tool_id=tool_id)
    if tool is None:
        raise GatewayError(404, "工具不存在", status_code=404)
    return await mcp_repo.set_tool_enabled(db, tool=tool, enabled=enabled)

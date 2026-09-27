"""L2 agents 路由（api/01 §5.1 契约，M3.1「注册一条 Agent 全层贯通」出口条件）。

纪律：全部写路径经 UoW+聚合方法——禁裸 SQL、禁绕过聚合直改 status（03 §6.1 / 04 §2）；
DTO 领域转换在 schemas/agent.py；AgentError → 3001（统一错误体经全局异常中间件）。
DELETE 不变式（§5.1）：存在 running task → 409（4102 同义场景）；存在会话引用 → 409。
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Query, status

from services.agent.api.deps import UowDep
from services.agent.api.schemas.agent import (
    AgentCreateIn,
    AgentDetailOut,
    AgentHealthOut,
    AgentListOut,
    AgentOut,
    AgentToolsIn,
    AgentUpdateIn,
    agent_detail_from_domain,
    agent_from_domain,
)
from services.agent.domain.model.agent import Agent, AgentError
from services.platform.deps import Principal, require_scope
from services.platform.errors import GatewayError

router = APIRouter(prefix="/agents", tags=["agents"])

AgentReadDep = Annotated[Principal, Depends(require_scope("agent:read"))]
AgentWriteDep = Annotated[Principal, Depends(require_scope("agent:write"))]

_HEALTH_TIMEOUT_S = 3.0


def _agent_error(exc: AgentError) -> GatewayError:
    """AgentError → GatewayError：消息前缀即登记错误码（3xxx 参数校验=HTTP 400，其余=409）。"""
    head = str(exc)[:4]
    code = int(head) if head.isdigit() else 3001
    return GatewayError(code, str(exc), status_code=400 if 3000 <= code < 4000 else 409)


@router.post("", status_code=status.HTTP_201_CREATED, summary="注册 agent（绑定适配器）")
async def create_agent(body: AgentCreateIn, principal: AgentWriteDep, uow: UowDep) -> AgentOut:
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            adapter_id = body.adapter_id or await tx.agents.ensure_platform_adapter(body.agent_tool)
            agent = Agent.register(
                tenant_id=principal.tenant_id,
                name=body.name,
                agent_tool=body.agent_tool,
                adapter_id=adapter_id,
                system_prompt=body.system_prompt,
                config=body.config,
            )
            await tx.agents.add(agent)
    except AgentError as exc:
        raise _agent_error(exc) from exc
    return agent_from_domain(agent)


@router.get("", summary="租户内 agent 列表（分页/状态筛选）")
async def list_agents(
    principal: AgentReadDep,
    uow: UowDep,
    agent_status: Annotated[str | None, Query(alias="status", pattern="^(enabled|disabled)$")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AgentListOut:
    async with uow.for_tenant(principal.tenant_id) as tx:
        items = await tx.agents.list(status=agent_status, offset=offset, limit=limit)
    return AgentListOut(items=[agent_from_domain(a) for a in items], offset=offset, limit=limit)


@router.get("/{agent_id}", summary="agent 详情（含适配器绑定信息）")
async def get_agent(agent_id: uuid.UUID, principal: AgentReadDep, uow: UowDep) -> AgentDetailOut:
    async with uow.for_tenant(principal.tenant_id) as tx:
        agent = await tx.agents.get(agent_id)
        if agent is None:
            raise GatewayError(404, "agent 不存在", status_code=404)
        adapter = await tx.agents.get_adapter(agent.adapter_id)
    return agent_detail_from_domain(agent, adapter=adapter)


@router.patch("/{agent_id}", summary="更新提示词/配置（改 agent_tool 视为重建，不支持）")
async def update_agent(agent_id: uuid.UUID, body: AgentUpdateIn, principal: AgentWriteDep, uow: UowDep) -> AgentOut:
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            agent = await tx.agents.get(agent_id)
            if agent is None:
                raise GatewayError(404, "agent 不存在", status_code=404)
            agent.update(system_prompt=body.system_prompt, config=body.config)
            await tx.agents.save_meta(agent)
    except AgentError as exc:
        raise _agent_error(exc) from exc
    return agent_from_domain(agent)


@router.put("/{agent_id}/tools", summary="绑定/解绑工具白名单（覆盖式）")
async def set_agent_tools(agent_id: uuid.UUID, body: AgentToolsIn, principal: AgentWriteDep, uow: UowDep) -> AgentOut:
    try:
        async with uow.for_tenant(principal.tenant_id) as tx:
            agent = await tx.agents.get(agent_id)
            if agent is None:
                raise GatewayError(404, "agent 不存在", status_code=404)
            agent.set_tools(body.tools)
            await tx.agents.save_meta(agent)
    except AgentError as exc:
        raise _agent_error(exc) from exc
    return agent_from_domain(agent)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT, summary="注销 agent（占用检查后删除）")
async def delete_agent(agent_id: uuid.UUID, principal: AgentWriteDep, uow: UowDep) -> None:
    async with uow.for_tenant(principal.tenant_id) as tx:
        agent = await tx.agents.get(agent_id)
        if agent is None:
            raise GatewayError(404, "agent 不存在", status_code=404)
        if await tx.tasks.find_running_by_agent(agent_id) is not None:
            # api/01 §5.1：存在 running task 时 409（4102 同义场景）
            raise GatewayError(4102, "agent 存在运行中的任务，禁止删除", status_code=409)
        if await tx.sessions.count_by_agent(agent_id) > 0:
            raise GatewayError(4103, "agent 存在会话引用，禁止删除", status_code=409)
        await tx.agents.delete(agent_id)


@router.post("/{agent_id}/health-check", summary="适配器探活（无端点=进程内置适配器）")
async def health_check(agent_id: uuid.UUID, principal: AgentReadDep, uow: UowDep) -> AgentHealthOut:
    async with uow.for_tenant(principal.tenant_id) as tx:
        agent = await tx.agents.get(agent_id)
        if agent is None:
            raise GatewayError(404, "agent 不存在", status_code=404)
        adapter = await tx.agents.get_adapter(agent.adapter_id)
    if adapter is None:
        # 注册面保证 adapter_id 恒有绑定行；防御性兜底按目标不可用上报（api/01 §5.1：5003）
        raise GatewayError(5003, "适配器绑定行缺失", status_code=503)
    if adapter.health_endpoint is None:
        return AgentHealthOut(status="inprocess", latency_ms=None)  # builtin/claude：进程内，无独立探活端点
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT_S) as client:
            response = await client.get(adapter.health_endpoint)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise GatewayError(5003, f"适配器探活失败: {exc}", status_code=503) from exc
    return AgentHealthOut(status="ok", latency_ms=int((time.monotonic() - started) * 1000))

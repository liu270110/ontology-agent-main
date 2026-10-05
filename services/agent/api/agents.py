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
from fastapi import APIRouter, Depends, Query, Request, status

from services.agent.api.deps import UowDep
from services.agent.api.schemas.agent import (
    AdapterSchemaListOut,
    AdapterSchemaOut,
    AgentCreateIn,
    AgentDetailOut,
    AgentHealthOut,
    AgentListOut,
    AgentOut,
    AgentStatusOut,
    AgentToolsIn,
    AgentUpdateIn,
    ConnectionTestIn,
    ConnectionTestOut,
    DebugChatIn,
    DebugChatOut,
    agent_detail_from_domain,
    agent_from_domain,
)
from services.agent.business import agent_admin
from services.agent.business.agent_health import record_adapter_health_outcome
from services.agent.domain.model.agent import Agent, AgentError
from services.platform.deps import Principal, require_scope
from services.platform.errors import GatewayError
from services.platform.ports.model_port import ModelPortError
from services.platform.schemas import PageMeta

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


@router.get("", summary="租户内 agent 列表（分页/状态筛选；api/01 §3.1 信封）")
async def list_agents(
    principal: AgentReadDep,
    uow: UowDep,
    agent_status: Annotated[str | None, Query(alias="status", pattern="^(enabled|disabled)$")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AgentListOut:
    """偏移分页改 page/page_size（B1 批，api/01 §3.1；offset=(page-1)*page_size 内部换算）。"""
    offset = (page - 1) * page_size
    async with uow.for_tenant(principal.tenant_id) as tx:
        items = await tx.agents.list(status=agent_status, offset=offset, limit=page_size)
        total = await tx.agents.count(status=agent_status)
    return AgentListOut(
        data=[agent_from_domain(a) for a in items],
        meta=PageMeta(page=page, page_size=page_size, total=total),
    )


# ── 管理面扩展（api/01 §5.1 ★ 预登记；静态路径先于 /{agent_id} 注册——FastAPI 首匹配）────


@router.get("/adapter-schemas", summary="适配器 config schema 下发（RJSF 渲染源；★ 预登记）")
async def list_adapter_schemas(principal: AgentReadDep, uow: UowDep) -> AdapterSchemaListOut:
    """枚举=agent_adapters 表行（按 agent_tool 去重）；无库表数据回落 pydantic 常量 schema
    （business._ADAPTER_REGISTRY，键集与领域 config 白名单同源）。"""
    async with uow.for_tenant(principal.tenant_id) as tx:
        adapters = await tx.agents.list_adapters()
    entries = agent_admin.adapter_schema_items(adapters)
    items = [AdapterSchemaOut.model_validate(entry.model_dump(by_alias=True)) for entry in entries]
    return AdapterSchemaListOut(items=items)


@router.post("/connection-test", summary="预注册连接测试（一次最小补全；失败结构化 200 不上 500）")
async def connection_test(body: ConnectionTestIn, principal: AgentWriteDep, request: Request) -> ConnectionTestOut:
    """注册向导「先测后注册」步（实例尚不存在故无 {id} 路径）：对目标 base_url/model 发一次
    1-token 最小补全（platform/llm 端口）；传输经 app.state.llm_probe_factory 注入（缺省真传输，
    测试注桩）。provider 当前仅登记（唯一 OpenAI 兼容通道，M0 口径）。"""
    probe = getattr(request.app.state, "llm_probe_factory", None)
    result = await agent_admin.probe_connection(
        provider=body.provider,
        base_url=body.base_url,
        api_key=body.api_key,
        model=body.model,
        probe=probe,
    )
    return ConnectionTestOut(**result)


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
        # 进程内适配器恒健康（builtin/claude）：按成功口径清零计数（H-0c ③ 端点接线）
        await record_adapter_health_outcome(uow, tenant_id=principal.tenant_id, agent_id=agent_id, healthy=True)
        return AgentHealthOut(status="inprocess", latency_ms=None)  # builtin/claude：进程内，无独立探活端点
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT_S) as client:
            response = await client.get(adapter.health_endpoint)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        await record_adapter_health_outcome(uow, tenant_id=principal.tenant_id, agent_id=agent_id, healthy=False)
        raise GatewayError(5003, f"适配器探活失败: {exc}", status_code=503) from exc
    await record_adapter_health_outcome(uow, tenant_id=principal.tenant_id, agent_id=agent_id, healthy=True)
    return AgentHealthOut(status="ok", latency_ms=int((time.monotonic() - started) * 1000))


# ── 启停与调试面（api/01 §5.15 定稿动词 start/stop→enable/disable；mock §5.1 R 预登记）──────


@router.post("/{agent_id}/disable", summary="禁用 agent（幂等；disabled=拒绝新会话绑定）")
async def disable_agent(agent_id: uuid.UUID, principal: AgentWriteDep, uow: UowDep) -> AgentStatusOut:
    """agents.status → disabled（聚合方法唯一写路径，幂等=已在目标状态零操作）。

    disabled 语义=**拒绝新会话绑定**（Agent.ensure_usable_for_new_session，创建会话端点强制
    409）——不级联改 sessions：存量会话与运行中 Run 跑完不中断（04 §10 degraded 同裁决），
    terminated_sessions 恒 0（前端 mock 形状占位字段）。"""
    return await _set_enabled(agent_id, principal=principal, uow=uow, enabled=False)


@router.post("/{agent_id}/enable", summary="启用 agent（幂等；disabled/degraded 离场）")
async def enable_agent(agent_id: uuid.UUID, principal: AgentWriteDep, uow: UowDep) -> AgentStatusOut:
    """agents.status → enabled（状态机：disabled 仅 enable 出口；degraded 可自愈同款通道）。"""
    return await _set_enabled(agent_id, principal=principal, uow=uow, enabled=True)


async def _set_enabled(agent_id: uuid.UUID, *, principal: Principal, uow: UowDep, enabled: bool) -> AgentStatusOut:
    """启停共用写路径：聚合翻转 + save_meta（同一 UoW 事务）；幂等短路在聚合侧。
    terminated_sessions 仅 disable 语义携带（恒 0，不强改 sessions）；enable 回 None。"""
    async with uow.for_tenant(principal.tenant_id) as tx:
        agent = await tx.agents.get(agent_id)
        if agent is None:
            raise GatewayError(404, "agent 不存在", status_code=404)
        terminated = agent_admin.set_agent_enabled(agent, enabled=enabled)
        await tx.agents.save_meta(agent)
    return AgentStatusOut(id=agent.id, status=agent.status.value, terminated_sessions=None if enabled else terminated)


@router.post("/{agent_id}/debug-chat", summary="调试对话（单轮生成；调试面不落会话/消息/任务行）")
async def debug_chat(
    agent_id: uuid.UUID, body: DebugChatIn, principal: AgentWriteDep, uow: UowDep, request: Request
) -> DebugChatOut:
    """IX-AGT-02 调试窗：对该 agent 配置走一次单轮生成（复用编排器最小生成面
    ChatAdapter.stream_chat，与 chat 主链同 builtin/claude 通道）。

    调试面声明：**不落 sessions/messages/tasks/runs 行**（调试对话不计正式历史，trace 随
    网关中间件留痕）；LLM 失败映射已登记 5xxx（5001→504，其余→502），不裸 500。"""
    async with uow.for_tenant(principal.tenant_id) as tx:
        agent = await tx.agents.get(agent_id)
        if agent is None:
            raise GatewayError(404, "agent 不存在", status_code=404)
    try:
        result = await agent_admin.debug_reply(
            agent=agent,
            message=body.message,
            params=body.params,
            model_port=getattr(request.app.state, "model_port", None),
            user_id=principal.user_id,
        )
    except ModelPortError as exc:  # 5xxx 已登记码结构化上抛（02 §4.1 ④：禁裸异常逃逸）
        code = int(getattr(exc, "code", 5999))
        raise GatewayError(code, str(exc), status_code=504 if code == 5001 else 502) from exc
    return DebugChatOut(**result)

"""agents 端点集成测试（api/01 §5.1 契约；marker=integration，直连本地 PG，端点函数直调）。

覆盖 M3.1 出口条件「注册一条 Agent 全层贯通」：注册→列表→详情（适配器绑定投影）→
PATCH/PUT tools→health-check→删除占用检查（running task 409 / 会话引用 409 / 空闲 204）
→ disabled 会话引用不变式（Agent 服务设计 §2）。
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.agent.api.agents import (
    create_agent,
    delete_agent,
    get_agent,
    health_check,
    list_agents,
    set_agent_tools,
    update_agent,
)
from services.agent.api.schemas.agent import AgentCreateIn, AgentToolsIn, AgentUpdateIn
from services.agent.api.schemas.session import SendMessageIn, SessionCreateIn
from services.agent.api.sessions import create_session, send_message
from services.agent.api.tasks import cancel_task
from services.agent.data.orm import Agent as AgentORM
from services.agent.data.orm import Message as MessageORM
from services.agent.data.orm import Run as RunORM
from services.agent.data.orm import Session as SessionORM
from services.agent.data.orm import Task as TaskORM
from services.agent.data.orm import TaskEvent as TaskEventORM
from services.gateway.middlewares import GatewayError
from services.platform.config import Settings
from services.platform.deps import Principal
from services.writeback.data.orm import OutboxEventORM

pytestmark = pytest.mark.integration


def agent_principal(seed_principal: Principal) -> Principal:
    """在 session 三 scope 之上补 agent:read/agent:write（api/01 §5.1）。"""
    claims = dict(seed_principal.raw)
    claims["scopes"] = [*claims["scopes"], "agent:read", "agent:write"]
    return Principal(claims)


@pytest.fixture
async def extra_agent_cleanup(seed):  # noqa: ANN001  # 复用 conftest seed（租户/主体/清理基座）
    """登记测试内创建的 agent id；结束按 FK 逆序清理本租户业务行 + extra agent 行。"""
    principal, _ = seed
    tenant_id = principal.tenant_id
    created: list[uuid.UUID] = []
    yield principal, created
    settings = Settings()
    engine = create_async_engine(settings.pg_dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        for stmt in (
            delete(OutboxEventORM).where(OutboxEventORM.tenant_id == tenant_id),
            delete(MessageORM).where(MessageORM.tenant_id == tenant_id),
            delete(TaskEventORM).where(TaskEventORM.tenant_id == tenant_id),
            delete(RunORM).where(RunORM.tenant_id == tenant_id),
            delete(TaskORM).where(TaskORM.tenant_id == tenant_id),
            delete(SessionORM).where(SessionORM.tenant_id == tenant_id),
            delete(AgentORM).where(AgentORM.tenant_id == tenant_id),
        ):
            await db.execute(stmt)
    await engine.dispose()


async def _new_agent(gateway_uow, principal: Principal, created: list[uuid.UUID], **kw: object):
    body = AgentCreateIn(name=kw.pop("name", f"it-agent-{uuid.uuid4().hex[:8]}"), agent_tool="builtin", **kw)  # type: ignore[arg-type]
    agent = await create_agent(body=body, principal=principal, uow=gateway_uow)
    created.append(agent.id)
    return agent


async def test_agent_注册_列表_详情_全层贯通(gateway_uow, extra_agent_cleanup):
    principal, created = extra_agent_cleanup
    principal = agent_principal(principal)
    agent = await _new_agent(gateway_uow, principal, created, config={"model": "qwen3", "temperature": 0.2})
    assert agent.status == "enabled" and agent.agent_tool == "builtin"
    # 列表（含状态筛选；api/01 §3.1 信封 {data, meta:{page,page_size,total}}）
    listing = await list_agents(principal=principal, uow=gateway_uow, page=1, page_size=20)
    assert agent.id in {a.id for a in listing.data}
    assert listing.meta.page == 1 and listing.meta.page_size == 20 and listing.meta.total >= 1
    disabled_only = await list_agents(
        principal=principal, uow=gateway_uow, page=1, page_size=20, agent_status="disabled"
    )
    assert agent.id not in {a.id for a in disabled_only.data}
    # 详情：平台级适配器绑定投影（ensure_platform_adapter 版本=platform）
    detail = await get_agent(agent.id, principal=principal, uow=gateway_uow)
    assert detail.adapter is not None
    assert detail.adapter.agent_tool == "builtin" and detail.adapter.version == "platform"
    # 健康检查：无探活端点=进程内置适配器（Agent 服务设计 §3.2 运行形态）
    health = await health_check(agent.id, principal=principal, uow=gateway_uow)
    assert health.status == "inprocess"


async def test_agent_注册非法载荷_3001与DTO校验(gateway_uow, extra_agent_cleanup):
    principal, _ = extra_agent_cleanup
    principal = agent_principal(principal)
    with pytest.raises(ValidationError):  # agent_tool 枚举在 DTO pattern 收窄（api/01 §3 未知值拒绝）
        AgentCreateIn(name="x", agent_tool="nanobot")  # type: ignore[arg-type]
    with pytest.raises(GatewayError) as ei:  # config 未登记键 → 聚合校验 → 3001
        await _new_agent(gateway_uow, principal, [], config={"weapon": "knife"})
    assert (ei.value.code, ei.value.status_code) == (3001, 400)


async def test_agent_更新与工具白名单_覆盖式(gateway_uow, extra_agent_cleanup):
    principal, created = extra_agent_cleanup
    principal = agent_principal(principal)
    agent = await _new_agent(gateway_uow, principal, created)
    updated = await update_agent(
        agent.id,
        body=AgentUpdateIn(system_prompt="你是电网运维专家", config={"model": "glm-4.7"}),
        principal=principal,
        uow=gateway_uow,
    )
    assert updated.system_prompt == "你是电网运维专家" and updated.config["model"] == "glm-4.7"
    tools_body = AgentToolsIn(tools=["kb.search", "memory.read", "kb.search"])
    with_tools = await set_agent_tools(agent.id, body=tools_body, principal=principal, uow=gateway_uow)
    assert with_tools.config["tool_whitelist"] == ["kb.search", "memory.read"]  # 覆盖式+去重保序
    # agent_tool 不在 PATCH 通道（DTO extra=forbid：改适配器类型=重建，§5.1）
    with pytest.raises(ValidationError):
        AgentUpdateIn.model_validate({"agent_tool": "claude"})


async def test_agent_禁用后_创建会话409_启用恢复(gateway_uow, extra_agent_cleanup):
    principal, created = extra_agent_cleanup
    principal = agent_principal(principal)
    agent = await _new_agent(gateway_uow, principal, created)
    session = await create_session(
        body=SessionCreateIn(agent_id=agent.id, title="停电分析"), principal=principal, uow=gateway_uow
    )
    assert session.agent_id == agent.id
    # 禁用（仓储全路径：聚合方法 + save_meta）
    async with gateway_uow.for_tenant(principal.tenant_id) as tx:
        stored = await tx.agents.get(agent.id)
        assert stored is not None
        stored.disable()
        await tx.agents.save_meta(stored)
    with pytest.raises(GatewayError) as ei:  # 不变式：disabled 不得被新会话引用（§2）
        await create_session(body=SessionCreateIn(agent_id=agent.id), principal=principal, uow=gateway_uow)
    assert (ei.value.code, ei.value.status_code) == (409, 409)


async def test_agent_删除_占用检查_运行任务与会话引用409_空闲204(gateway_uow, extra_agent_cleanup):
    principal, created = extra_agent_cleanup
    principal = agent_principal(principal)
    free = await _new_agent(gateway_uow, principal, created)
    busy = await _new_agent(gateway_uow, principal, created)
    # 会话 + 运行任务（send_message 受理即建 running Task，agent_id 随会话落库）
    session = await create_session(body=SessionCreateIn(agent_id=busy.id), principal=principal, uow=gateway_uow)
    msg_body = SendMessageIn(content="分析停电原因")
    accepted = await send_message(session.id, msg_body, principal=principal, uow=gateway_uow)
    task_id = uuid.UUID(accepted["data"]["task_id"])
    with pytest.raises(GatewayError) as ei:  # 存在 running task → 409（4102 同义场景，§5.1）
        await delete_agent(busy.id, principal=principal, uow=gateway_uow)
    assert (ei.value.code, ei.value.status_code) == (4102, 409)
    # 取消任务后无 running task，但会话引用仍在 → 409
    await cancel_task(task_id, principal=principal, uow=gateway_uow)
    with pytest.raises(GatewayError) as ei2:
        await delete_agent(busy.id, principal=principal, uow=gateway_uow)
    assert (ei2.value.code, ei2.value.status_code) == (4103, 409)
    # 空闲 agent（无会话无任务）→ 204 删除成功，get 404
    await delete_agent(free.id, principal=principal, uow=gateway_uow)
    with pytest.raises(GatewayError) as ei3:
        await get_agent(free.id, principal=principal, uow=gateway_uow)
    assert ei3.value.status_code == 404

"""L2 网关 DTO：Agent（与领域模型严格分离，转换函数同文件）。

铁律：extra="forbid"、snake_case、只数据无行为；api/01 §5.1 agents 契约。
agent_tool 的合法集与领域层 ALLOWED_AGENT_TOOLS 同源（M3 落地顺序裁决：builtin/claude）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from services.agent.domain.model.agent import Agent, AgentAdapterInfo
from services.platform.schemas import PageMeta

# G2 批（2026-10-07，docs/Agent/20 §3.3）：+http-generic|cli-generic（F3/F2 通用适配器）；
# acp 归 G1 批；与领域层 ALLOWED_AGENT_TOOLS 同源。
_AGENT_TOOL_PATTERN = "^(builtin|claude|acp|http-generic|cli-generic)$"  # G1(acp)+G2(http/cli) 并集


class AgentCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    agent_tool: str = Field(pattern=_AGENT_TOOL_PATTERN)
    system_prompt: str | None = Field(default=None, max_length=65_536)
    config: dict[str, Any] = Field(default_factory=dict)
    adapter_id: uuid.UUID | None = None  # 缺省=取/建平台级 (agent_tool, 'platform') 绑定行


class AgentUpdateIn(BaseModel):
    """PATCH 语义：字段缺省=不更新；agent_tool 不在变更通道（改适配器类型视为重建，§5.1）。"""

    model_config = ConfigDict(extra="forbid")
    system_prompt: str | None = Field(default=None, max_length=65_536)
    config: dict[str, Any] | None = None


class AgentToolsIn(BaseModel):
    """工具白名单覆盖式更新（api/01 §5.1 PUT /tools：绑定/解绑=整表替换）。"""

    model_config = ConfigDict(extra="forbid")
    tools: list[str] = Field(max_length=64)


class AgentAdapterOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    agent_tool: str
    version: str
    health_endpoint: str | None = None


class AgentOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    name: str
    agent_tool: str
    status: str
    system_prompt: str | None = None
    config: dict[str, Any]
    created_at: datetime | None = None


class AgentDetailOut(AgentOut):
    model_config = ConfigDict(extra="forbid")
    adapter: AgentAdapterOut | None = None


class AgentListOut(BaseModel):
    """agent 列表（api/01 §3.1 信封：{data, meta:{page,page_size,total}}，B1 批统一）。"""

    model_config = ConfigDict(extra="forbid")
    data: list[AgentOut]
    meta: PageMeta


class AgentHealthOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str  # inprocess | ok
    latency_ms: int | None = None


# ── 管理面扩展四端点（api/01 §5.1 ★ 预登记；契约源=mock platform-handlers.ts §5.1）──────


class AdapterSchemaOut(BaseModel):
    """适配器 config schema 下发单项（mock ADAPTER_SCHEMAS 逐字段；schema=JSON Schema，
    RJSF 渲染源——字段别名序列化，内部名 config_schema 避让 pydantic v1 遗留方法名）。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    key: str
    name: str
    vendor: str
    capability: str
    config_schema: dict[str, Any] = Field(default_factory=dict, alias="schema")


class AdapterSchemaListOut(BaseModel):
    """适配器 schema 列表（非分页静态清单：{items} 裸信封，mcp ServerListOut 同款）。"""

    model_config = ConfigDict(extra="forbid")
    items: list[AdapterSchemaOut] = Field(default_factory=list)


class ConnectionTestIn(BaseModel):
    """预注册连接测试入参（注册向导「先测后注册」步；实例尚不存在故无 {id} 路径）。"""

    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=64)  # 当前仅登记（唯一 OpenAI 兼容通道）
    base_url: str = Field(min_length=1, max_length=512)
    api_key: str | None = Field(default=None, max_length=512)  # 本地渠道可缺省（占位 EMPTY）
    model: str = Field(min_length=1, max_length=128)


class ConnectionTestOut(BaseModel):
    """连接测试结果：失败**结构化 200**（ok=false + error），不上 500（探测面非服务故障）。"""

    model_config = ConfigDict(extra="forbid")
    ok: bool
    latency_ms: int
    model: str
    error: str | None = None


class AgentStatusOut(BaseModel):
    """启停结果（enable：{id,status}；disable 附 terminated_sessions——恒 0，调试：语义=新会话
    拒绑，不强改 sessions，字段为前端 mock 形状占位）。status 取领域三态值（enabled/disabled/
    degraded），前端 fe1-F2 双口径映射已收敛。"""

    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    status: str
    terminated_sessions: int | None = None


class DebugChatIn(BaseModel):
    """调试对话入参（单轮；params 预留——当前仅消费 timeout_ms）。"""

    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=8192)
    params: dict[str, Any] | None = None


class DebugChatOut(BaseModel):
    """调试对话结果（usage=平台用量上下文形状 token_in/token_out/cache_read_tokens；桩不回填报
    空对象）。调试面不落 sessions/messages/tasks 行——响应不含 trace 会话语义字段。"""

    model_config = ConfigDict(extra="forbid")
    reply: str
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int


def adapter_from_domain(a: AgentAdapterInfo) -> AgentAdapterOut:
    return AgentAdapterOut(id=a.id, agent_tool=a.agent_tool, version=a.version, health_endpoint=a.health_endpoint)


def agent_from_domain(agent: Agent) -> AgentOut:
    return AgentOut(
        id=agent.id,
        name=agent.name,
        agent_tool=agent.agent_tool,
        status=agent.status.value,
        system_prompt=agent.system_prompt,
        config=agent.config,
        created_at=agent.created_at,
    )


def agent_detail_from_domain(agent: Agent, *, adapter: AgentAdapterInfo | None = None) -> AgentDetailOut:
    return AgentDetailOut(
        id=agent.id,
        name=agent.name,
        agent_tool=agent.agent_tool,
        status=agent.status.value,
        system_prompt=agent.system_prompt,
        config=agent.config,
        created_at=agent.created_at,
        adapter=adapter_from_domain(adapter) if adapter is not None else None,
    )

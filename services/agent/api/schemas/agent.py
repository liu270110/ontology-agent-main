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

_AGENT_TOOL_PATTERN = "^(builtin|claude)$"


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
    model_config = ConfigDict(extra="forbid")
    items: list[AgentOut]
    offset: int
    limit: int


class AgentHealthOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str  # inprocess | ok
    latency_ms: int | None = None


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

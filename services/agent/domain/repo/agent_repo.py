"""仓储接口（04 篇 §4 约定同源）：租户作用域构造期绑定，方法级不传 tenant_id。

约定：`get` 未命中返回 None；`save_meta` 全量保存聚合标量；适配器绑定行（agent_adapters，
平台级无租户列）经独立只读面/ensure 面触达。查询方法按用例定制（04 §4）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from services.agent.domain.model.agent import Agent, AgentAdapterInfo


@runtime_checkable
class AgentRepository(Protocol):
    """agent 聚合仓储：CRUD + 状态筛选列表（api/01 §5.1 用例）。"""

    async def get(self, agent_id: UUID) -> Agent | None: ...

    async def add(self, agent: Agent) -> None: ...

    async def save_meta(self, agent: Agent) -> None:
        """仅标量（name/system_prompt/config/status）；adapter_id/agent_tool 注册后不可变。"""
        ...

    async def delete(self, agent_id: UUID) -> None: ...

    async def list(self, *, status: str | None = None, offset: int = 0, limit: int = 20) -> list[Agent]: ...

    async def count(self, *, status: str | None = None) -> int:
        """列表总数（api/01 §3.1 分页 meta.total；筛选条件与 list 同口径）。"""
        ...

    async def get_adapter(self, adapter_id: UUID) -> AgentAdapterInfo | None:
        """适配器绑定行只读投影（详情/健康检查）；未命中返回 None。"""
        ...

    async def ensure_platform_adapter(self, agent_tool: str) -> UUID:
        """取/建平台级适配器行（agent_tool, version='platform'）：注册用例的缺省绑定。"""
        ...

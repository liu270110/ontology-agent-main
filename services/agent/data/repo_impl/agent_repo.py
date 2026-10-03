"""AgentRepository 的 L6 PG 实现（06 篇 §1 repo_impl 纪律，与 session_repo 同源）。

- 强制租户过滤（tenant_id 构造期绑定）；agent_adapters 为平台级表（无租户列），只读/ensure 面；
- typed SQLAlchemy 2.0，无裸 SQL 字符串；`get` 未命中返回 None。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.agent.data.orm import Agent as AgentORM
from services.agent.data.orm import AgentAdapter as AgentAdapterORM
from services.agent.domain.model.agent import Agent, AgentAdapterInfo, AgentStatus

_PLATFORM_ADAPTER_VERSION = "platform"


def _agent_to_domain(row: AgentORM) -> Agent:
    return Agent(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        agent_tool=row.agent_tool,
        adapter_id=row.adapter_id,
        system_prompt=row.system_prompt,
        config=row.config or {},
        status=AgentStatus(row.status),
        adapter_failure_count=row.adapter_failure_count,
        created_at=row.created_at,
    )


def _adapter_to_domain(row: AgentAdapterORM) -> AgentAdapterInfo:
    return AgentAdapterInfo(
        id=row.id,
        agent_tool=row.agent_tool,
        version=row.version,
        health_endpoint=row.health_endpoint,
    )


class PgAgentRepository:
    """AgentRepository 的 PG 实现。"""

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def get(self, agent_id: uuid.UUID) -> Agent | None:
        stmt = select(AgentORM).where(AgentORM.id == agent_id, AgentORM.tenant_id == self._tenant_id)
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        return _agent_to_domain(row) if row is not None else None

    async def add(self, agent: Agent) -> None:
        self._db.add(
            AgentORM(
                id=agent.id,
                tenant_id=agent.tenant_id,
                name=agent.name,
                agent_tool=agent.agent_tool,
                adapter_id=agent.adapter_id,
                system_prompt=agent.system_prompt,
                config=agent.config,
                status=agent.status.value,
                adapter_failure_count=agent.adapter_failure_count,
            )
        )
        await self._db.flush()

    async def save_meta(self, agent: Agent) -> None:
        row = await self._db.get(AgentORM, agent.id)
        if row is None:
            raise ValueError(f"agent 不存在，拒绝 save_meta: {agent.id}")
        if row.tenant_id != self._tenant_id:  # 防御：禁止跨租户写（06 篇 §1 repo_impl 纪律）
            raise ValueError("租户不匹配：拒绝保存他租户 agent 行")
        row.name = agent.name
        row.system_prompt = agent.system_prompt
        row.config = agent.config
        row.status = agent.status.value
        row.adapter_failure_count = agent.adapter_failure_count
        await self._db.flush()

    async def delete(self, agent_id: uuid.UUID) -> None:
        row = await self._db.get(AgentORM, agent_id)
        if row is not None and row.tenant_id == self._tenant_id:
            await self._db.delete(row)
            await self._db.flush()

    async def list(self, *, status: str | None = None, offset: int = 0, limit: int = 20) -> list[Agent]:
        stmt = select(AgentORM).where(AgentORM.tenant_id == self._tenant_id)
        if status is not None:
            stmt = stmt.where(AgentORM.status == status)
        stmt = stmt.order_by(AgentORM.created_at.desc(), AgentORM.id.desc()).offset(offset).limit(limit)
        rows = (await self._db.execute(stmt)).scalars().all()
        return [_agent_to_domain(r) for r in rows]

    async def count(self, *, status: str | None = None) -> int:
        stmt = select(func.count()).select_from(AgentORM).where(AgentORM.tenant_id == self._tenant_id)
        if status is not None:
            stmt = stmt.where(AgentORM.status == status)
        return int((await self._db.execute(stmt)).scalar_one())

    async def get_adapter(self, adapter_id: uuid.UUID) -> AgentAdapterInfo | None:
        row = await self._db.get(AgentAdapterORM, adapter_id)
        return _adapter_to_domain(row) if row is not None else None

    async def ensure_platform_adapter(self, agent_tool: str) -> uuid.UUID:
        """取/建平台级适配器行（agent_tool, version='platform'）；uk_agent_adapters 兜底并发。"""
        stmt = select(AgentAdapterORM).where(
            AgentAdapterORM.agent_tool == agent_tool, AgentAdapterORM.version == _PLATFORM_ADAPTER_VERSION
        )
        row = (await self._db.execute(stmt)).scalar_one_or_none()
        if row is not None:
            return row.id
        row = AgentAdapterORM(agent_tool=agent_tool, version=_PLATFORM_ADAPTER_VERSION)
        self._db.add(row)
        await self._db.flush()
        return row.id

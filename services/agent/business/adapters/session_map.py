"""桥会话映射表 adapter_sessions（docs/Agent/05 §4.4 + 20 篇 §2.1/§3.1）。

桥自持 ``platform_session ↔ 外部工具 session id`` 映射（PG 表 adapter_sessions；20 篇 §2.1
「F3/F5 共用，一次建表」——本批 G2 本地建表，与 G1 同名同 DDL，合入冲突由主会话并集解）。

- 适配器经 :class:`AdapterSessionMapper` Protocol 消费（L3 零 PG 依赖，测试注内存桩）；
- 生产实现 :class:`PgAdapterSessionMapper`：短事务读/写（组合根注入 async_sessionmaker）；
  tenant_id 随查/写显式携带（TenantMixin 非空列 + uk (tenant_id, session_id, adapter)）；
- F2 CLI run-per-turn 无跨进程会话，不消费本表（05 篇 §4.4：L1 重放替代）。
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from sqlalchemy import select

from services.agent.data.orm import AdapterSession


class AdapterSessionMapper(Protocol):
    """会话映射端口：platform (tenant_id, session_id) → 对端 foreign_id。"""

    async def get(self, *, tenant_id: uuid.UUID, session_id: uuid.UUID) -> str | None:
        """取映射；无绑定返回 None。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def put(
        self,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        foreign_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """登记/覆盖映射（幂等：同 (tenant_id, session_id, adapter) 覆盖 foreign_id）。"""
        ...  # pragma: no cover — Protocol 方法无实现


class InMemoryAdapterSessionMapper:
    """内存映射（测试桩；进程级字典，零持久化）。"""

    def __init__(self) -> None:
        self._items: dict[tuple[uuid.UUID, uuid.UUID], str] = {}
        self.meta: dict[tuple[uuid.UUID, uuid.UUID], dict[str, Any]] = {}

    async def get(self, *, tenant_id: uuid.UUID, session_id: uuid.UUID) -> str | None:
        return self._items.get((tenant_id, session_id))

    async def put(
        self,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        foreign_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        key = (tenant_id, session_id)
        self._items[key] = foreign_id
        if metadata:
            self.meta[key] = dict(metadata)


class PgAdapterSessionMapper:
    """PG 映射实现（adapter_sessions 表；短事务即用即弃——05 §4.4 桥自持映射裁决）。"""

    def __init__(self, session_factory: Any, *, adapter: str) -> None:
        self._session_factory = session_factory
        self._adapter = adapter

    async def get(self, *, tenant_id: uuid.UUID, session_id: uuid.UUID) -> str | None:
        async with self._session_factory() as db, db.begin():
            stmt = select(AdapterSession.foreign_id).where(
                AdapterSession.tenant_id == tenant_id,
                AdapterSession.session_id == session_id,
                AdapterSession.adapter == self._adapter,
            )
            return (await db.execute(stmt)).scalar_one_or_none()

    async def put(
        self,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        foreign_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        async with self._session_factory() as db, db.begin():
            stmt = select(AdapterSession).where(
                AdapterSession.tenant_id == tenant_id,
                AdapterSession.session_id == session_id,
                AdapterSession.adapter == self._adapter,
            )
            row = (await db.execute(stmt)).scalar_one_or_none()
            if row is None:
                db.add(
                    AdapterSession(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        adapter=self._adapter,
                        foreign_id=foreign_id,
                        meta=metadata or {},
                    )
                )
            else:
                row.foreign_id = foreign_id
                if metadata:
                    row.meta = metadata

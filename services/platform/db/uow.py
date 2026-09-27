"""L6 Unit of Work（06 篇 §1 对外契约；事务边界原则=03 篇 §6.1）。

- 一个用例 = 一个 UoW；仓储实例只从 `for_tenant()` 获取（租户作用域构造期绑定，04 篇 §4 裁决，
  方法级不再传 tenant_id——少一个参数就少一处漏过滤的机会）；
- `__aexit__`：正常提交、异常回滚；SSE 长流程全程不持事务（03 §6.1）；
- 跨存储不做分布式事务：投影只经 `enqueue_projection()` 以 outbox 行**与业务行同事务落库**
  （06 §1/§8；03 §6.2 M4 裁决：Outbox relay 异步投影，崩溃安全=at-least-once + 消费端
  按 event_id 去重；M1~M3 的进程内缓冲占位已退役）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    AsyncSessionTransaction,
    async_sessionmaker,
    create_async_engine,
)

from services.agent.data.repo_impl.session_repo import PgSessionRepository, PgTaskRepository
from services.agent.domain.repo.session_repo import SessionRepository, TaskRepository
from services.writeback.data.orm import OutboxEventORM

logger = logging.getLogger(__name__)


class TenantTransaction:
    """`for_tenant()` 返回的事务作用域：一个 PG 事务 + 绑定租户的仓储集。

    仓储以属性暴露、类型为 L4 Protocol（06 §1）；仅可在 `async with` 体内使用。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> None:
        self._session_factory = session_factory
        self._tenant_id = tenant_id
        self._session: AsyncSession | None = None
        self._tx: AsyncSessionTransaction | None = None
        self.sessions: SessionRepository  # __aenter__ 时绑定
        self.tasks: TaskRepository

    async def __aenter__(self) -> TenantTransaction:
        self._session = self._session_factory()
        self._tx = await self._session.begin()
        self.sessions = PgSessionRepository(self._session, self._tenant_id)
        self.tasks = PgTaskRepository(self._session, self._tenant_id)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._session is None or self._tx is None:
            raise RuntimeError("TenantTransaction 未进入（__aenter__ 未执行）")
        try:
            if exc_type is None:
                await self._tx.commit()  # outbox 行随业务行同事务提交（enqueue_projection 已 add）
            else:
                await self._tx.rollback()
        finally:
            await self._session.close()

    def enqueue_projection(self, event_type: str, aggregate_id: uuid.UUID, payload: dict[str, Any]) -> None:
        """业务行与 outbox 事件同一事务落库的入口（06 §1/§8）。

        M4 起真正写 outbox_events 行（进程内缓冲退役）：本事务提交后由 Outbox relay
        异步发布（at-least-once，消费端按 event_id 去重，03 §6.2/07 §5.2）；
        投影失败不回滚主事实——outbox 行插入失败随本事务回滚属发布前失败（06 §8）。
        ``aggregate_type`` 取事件名首段（"run.cancelled"→"run"；relay 分区保序键）。
        """
        if self._session is None:
            raise RuntimeError("TenantTransaction 未进入（__aenter__ 未执行）")
        self._session.add(
            OutboxEventORM(
                tenant_id=self._tenant_id,
                aggregate_type=event_type.partition(".")[0],
                aggregate_id=aggregate_id,
                event_type=event_type,
                payload=payload,
            )
        )


class AsyncUnitOfWork:
    """UoW：持有 async engine / session 工厂（06 §1 clients/pg 职责收敛于此），`for_tenant()` 出租户绑定事务。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine | None = None) -> None:
        self._session_factory = session_factory
        self._engine = engine

    @classmethod
    def from_dsn(cls, dsn: str) -> AsyncUnitOfWork:
        engine = create_async_engine(dsn, pool_pre_ping=True)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        return cls(factory, engine)

    def for_tenant(self, tenant_id: uuid.UUID) -> TenantTransaction:
        """构造期绑定租户作用域（04 §4 裁决），返回同事务的仓储集。"""
        return TenantTransaction(self._session_factory, tenant_id)

    async def aclose(self) -> None:
        """释放连接池（应用停机调用，02 §2 停机序列）。"""
        if self._engine is not None:
            await self._engine.dispose()

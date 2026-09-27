"""writeback 仓储协议（L4，04 §4：租户作用域构造期绑定，实现归 L6 repo_impl）。

- 台账仓储：租户绑定（create/add 幂等键 UK 冲突转 DuplicateIdempotencyKeyError 携既有行）；
- Outbox 写入面：租户绑定（与业务行同事务 enqueue，06 §8）；
- Outbox 拉取面（relay 专用）：**跨租户**扫描（relay 为平台级常驻任务，07 §5.2），独立协议
  避免污染租户作用域仓储纪律。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from services.writeback.domain.model import LedgerStatus, OutboxEvent, WritebackLedger


@runtime_checkable
class WritebackLedgerRepository(Protocol):
    """回写台账仓储（UK(tenant_id, idempotency_key) 冲突 → DuplicateIdempotencyKeyError）。"""

    async def add(self, entry: WritebackLedger) -> None:
        """insert pending 台账行；幂等键冲突抛 DuplicateIdempotencyKeyError（含既有行）。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def get(self, entry_id: UUID) -> WritebackLedger | None:
        """按 id 取台账行（租户作用域内；未命中返回 None）。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def get_by_idempotency_key(self, idempotency_key: str) -> WritebackLedger | None:
        """按幂等键取台账行（幂等重放/对账核实）。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def save_state(self, entry: WritebackLedger) -> None:
        """全量保存标量状态（status/attempts/receipt/needs_human/last_error；调用方短事务内）。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def list_by_statuses(self, statuses: Sequence[LedgerStatus], *, limit: int = 100) -> list[WritebackLedger]:
        """对账扫描（§4.1 非终态清单；updated_at 升序，优先最老未决行）。"""
        ...  # pragma: no cover — Protocol 方法无实现


@runtime_checkable
class OutboxRepository(Protocol):
    """Outbox 写入面（租户绑定；UoW enqueue_projection 同事务落库用）。"""

    async def enqueue(self, event: OutboxEvent) -> None:
        """insert outbox 行（必须与业务行同一事务；06 §8 铁律：投影失败不回滚主事实）。"""
        ...  # pragma: no cover — Protocol 方法无实现


@runtime_checkable
class OutboxPoller(Protocol):
    """Outbox relay 拉取面（跨租户；07 §5.2 实现参数：单批 100、同聚合按 id 保序）。"""

    async def fetch_pending(self, limit: int) -> list[OutboxEvent]:
        """取待发布批（status IN (pending, failed) 且 published_at IS NULL，按 id 升序）。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def mark_published(self, event_id: UUID, *, at: datetime) -> bool:
        """投递成功回写（WHERE status != published 的守门更新；返回是否实际更新）。"""
        ...  # pragma: no cover — Protocol 方法无实现

    async def mark_failed(self, event_id: UUID, last_error: str, *, dead: bool) -> None:
        """投递失败：retry_count+1（dead=True 时置 dead 死信，不再被扫描）。"""
        ...  # pragma: no cover — Protocol 方法无实现

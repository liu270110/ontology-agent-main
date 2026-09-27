"""llm_calls 审计批量落库（architecture/07 §5.4「llm_calls 批量写」P1 痛点优化的实现）。

- 进程内队列 + **批量落库**：缓冲 max_batch(100) 条或 flush_interval_s(1s) 先到者 flush——
  逐请求直写是 LiteLLM 官方自认瓶颈（≈1k rps 打满 PG）；
- 崩溃容忍 ≤1s 计费明细丢失（07 §5.4 口径）；flush 失败原序回队首重试，队列超限丢最旧并记 ERROR；
- **LLM 调用在事务外原则不受影响**：本缓冲不开启长事务，flush 为一次性短事务；
- 预算/限流判断走 Redis 计数热路径（budget.py），PG 只做冷账本。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.platform.llm.orm import LlmCall

logger = logging.getLogger("services.platform.llm.audit")

_KIND_COMPLETE = "complete"  # database/01 §3.9 kind 枚举（stream/embed 随对应通道启用）


@dataclass(frozen=True)
class LlmCallRecord:
    """单次调用审计记录（与 llm_calls 列一一对应；created_at=调用时刻）。"""

    tenant_id: UUID
    provider: str
    model: str
    status: str  # ok | error
    kind: str = _KIND_COMPLETE
    token_in: int = 0
    token_out: int = 0
    cache_read_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    latency_ms: int | None = None
    session_id: UUID | None = None
    task_id: UUID | None = None
    trace_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class LlmCallAuditBuffer:
    """进程内审计缓冲：record() 入队，flush()/maybe_flush() 批量落库（组合根调度）。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_batch: int = 100,
        flush_interval_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        max_queue: int = 10_000,
    ) -> None:
        self._session_factory = session_factory
        self._max_batch = max_batch
        self._flush_interval_s = flush_interval_s
        self._clock = clock
        self._max_queue = max_queue
        self._queue: deque[LlmCallRecord] = deque()
        self._last_flush = clock()

    # ---------------------------------------------------------------- 入队
    def record(self, rec: LlmCallRecord) -> None:
        """入队（永不抛错：审计失败不阻塞 LLM 调用路径）。"""
        self._queue.append(rec)
        while len(self._queue) > self._max_queue:
            self._queue.popleft()
            logger.error("llm_calls 审计队列超限，丢弃最旧记录（07 §5.4 容忍口径）")

    @property
    def pending(self) -> int:
        """当前缓冲深度（测试与观测用）。"""
        return len(self._queue)

    # ---------------------------------------------------------------- flush 调度
    def needs_flush(self) -> bool:
        """满 N 条或 T 秒（且有存货）即需 flush（07 §5.4：先到者触发）。"""
        if not self._queue:
            return False
        if len(self._queue) >= self._max_batch:
            return True
        return (self._clock() - self._last_flush) >= self._flush_interval_s

    async def maybe_flush(self) -> int | None:
        """到点才 flush；未到点返回 None（调用方无需区分）。"""
        if not self.needs_flush():
            return None
        return await self.flush()

    async def flush(self) -> int:
        """批量落库（一次性短事务）；失败原序回队首返回 0（不抛错、不丢明细）。"""
        batch: list[LlmCallRecord] = []
        while self._queue:
            batch.append(self._queue.popleft())
        if not batch:
            return 0
        try:
            async with self._session_factory() as session:
                session.add_all(self._to_orm(rec) for rec in batch)
                await session.commit()
        except (SQLAlchemyError, OSError):
            logger.exception("llm_calls 批量落库失败，%d 条原序回队重试", len(batch))
            self._queue.extendleft(reversed(batch))
            self._trim_overflow()
            return 0
        self._last_flush = self._clock()
        return len(batch)

    async def run(self, *, poll_interval_s: float = 0.2) -> None:
        """常驻 flush 循环（组合根 asyncio.create_task 持有；取消即退出，余量由停机 flush 兜底）。"""
        try:
            while True:
                await asyncio.sleep(poll_interval_s)
                await self.maybe_flush()
        except asyncio.CancelledError:  # 停机取消：正常退出路径
            return

    # ---------------------------------------------------------------- 内部
    def _trim_overflow(self) -> None:
        while len(self._queue) > self._max_queue:
            self._queue.popleft()
            logger.error("llm_calls 审计队列超限，丢弃最旧记录（07 §5.4 容忍口径）")

    @staticmethod
    def _to_orm(rec: LlmCallRecord) -> LlmCall:
        payload: dict[str, Any] = {
            "tenant_id": rec.tenant_id,
            "provider": rec.provider[:32],
            "model": rec.model[:64],
            "kind": rec.kind,
            "token_in": rec.token_in,
            "token_out": rec.token_out,
            "cache_read_tokens": rec.cache_read_tokens,
            "cost_usd": rec.cost_usd,
            "latency_ms": rec.latency_ms,
            "status": rec.status[:16],
            "session_id": rec.session_id,
            "task_id": rec.task_id,
            "trace_id": rec.trace_id[:64] if rec.trace_id else None,
            "created_at": rec.created_at,
        }
        return LlmCall(**payload)

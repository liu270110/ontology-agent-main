"""llm_calls 审计批量落库测试（07 篇 §5.4：缓冲 1s 或 100 条先到者 flush；fake clock 驱动 T 触发）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from services.platform.llm.audit import LlmCallAuditBuffer, LlmCallRecord

pytestmark = pytest.mark.integration

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from tests.platform.conftest import PlatformSeed


class FakeClock:
    """可手动推进的单调钟（T 秒触发的确定性驱动）。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FlakyFactory:
    """首次调用抛错、其后放行的会话工厂（flush 失败→恢复用例）。"""

    def __init__(self, inner: async_sessionmaker[AsyncSession]) -> None:
        self._inner = inner
        self.fail = True

    def __call__(self) -> AsyncSession:
        if self.fail:
            raise ConnectionError("pg down")
        return self._inner()


def _record(tenant: uuid.UUID, seq: int = 0, at: datetime | None = None) -> LlmCallRecord:
    return LlmCallRecord(
        tenant_id=tenant,
        provider="openai_compatible",
        model="deepseek-chat",
        status="ok",
        token_in=100 + seq,
        token_out=10 + seq,
        cache_read_tokens=3,
        cost_usd=Decimal("0.000100"),
        latency_ms=120,
        trace_id=f"trace-{seq}",
        created_at=at or datetime.now(UTC),
    )


def _buffer(factory: object, *, max_batch: int = 100, interval: float = 1.0) -> tuple[LlmCallAuditBuffer, FakeClock]:
    clock = FakeClock()
    return (
        LlmCallAuditBuffer(
            factory,  # type: ignore[arg-type]  # 测试替身工厂
            max_batch=max_batch,
            flush_interval_s=interval,
            clock=clock,
        ),
        clock,
    )


async def _row_count(seed: PlatformSeed) -> int:
    async with seed.factory() as db:
        from services.platform.llm.orm import LlmCall

        return int((await db.execute(select(func.count()).select_from(LlmCall))).scalar_one())


async def test_满N条触发flush_批量落库(llm_seed):
    # Arrange：max_batch=5，先入 4 条（未到点不触发）
    buffer, _clock = _buffer(llm_seed.factory, max_batch=5)
    for i in range(4):
        buffer.record(_record(llm_seed.tenant_id, i))
    assert buffer.needs_flush() is False
    # Act：第 5 条到点（N 条触发）
    buffer.record(_record(llm_seed.tenant_id, 4))
    flushed = await buffer.maybe_flush()
    # Assert：一次批量落库 + 队列清空
    assert flushed == 5 and buffer.pending == 0
    assert await _row_count(llm_seed) == 5


async def test_T秒到期触发flush_fake_clock(llm_seed):
    # Arrange：1 条在队，时间未到（1s 间隔）
    buffer, clock = _buffer(llm_seed.factory, interval=1.0)
    buffer.record(_record(llm_seed.tenant_id, 0))
    assert buffer.needs_flush() is False
    # Act：时钟推进 2s（07 §5.4：T 秒先到者触发）
    clock.advance(2.0)
    flushed = await buffer.maybe_flush()
    # Assert
    assert flushed == 1 and buffer.pending == 0
    assert await _row_count(llm_seed) == 1


async def test_flush失败_不抛错原序回队_恢复后全量落库(llm_seed):
    # Arrange：首次 flush 必败的工厂
    flaky = FlakyFactory(llm_seed.factory)
    buffer, _clock = _buffer(flaky)
    at = datetime(2026, 9, 27, 0, 0, tzinfo=UTC)
    buffer.record(_record(llm_seed.tenant_id, 0, at))
    buffer.record(_record(llm_seed.tenant_id, 1, at + timedelta(milliseconds=10)))
    # Act：失败 flush（不抛错、原序回队不丢失）
    assert await buffer.flush() == 0
    assert buffer.pending == 2
    # Act：PG 恢复后再 flush
    flaky.fail = False
    assert await buffer.flush() == 2
    # Assert：按 created_at 保序落库
    async with llm_seed.factory() as db:
        from services.platform.llm.orm import LlmCall

        rows = (await db.execute(select(LlmCall.token_in).order_by(LlmCall.created_at))).scalars().all()
    assert rows == [100, 101]


async def test_llm_calls行字段完整_含失败调用(llm_seed):
    # Arrange：一条 error 记录（07 §2.3：每次调用含失败落库）
    buffer, _clock = _buffer(llm_seed.factory)
    buffer.record(
        LlmCallRecord(
            tenant_id=llm_seed.tenant_id,
            provider="openai_compatible",
            model="deepseek-chat",
            status="error",
            token_in=55,
            token_out=6,
            cache_read_tokens=2,
            latency_ms=88,
            trace_id="trace-err",
        )
    )
    # Act
    assert await buffer.flush() == 1
    # Assert：字段与 database/01 §3.9 口径一致
    async with llm_seed.factory() as db:
        from services.platform.llm.orm import LlmCall

        row = (await db.execute(select(LlmCall).order_by(LlmCall.created_at.desc()))).scalars().first()
    assert row is not None
    assert (row.provider, row.model, row.kind, row.status) == (
        "openai_compatible",
        "deepseek-chat",
        "complete",
        "error",
    )
    assert (row.token_in, row.token_out, row.cache_read_tokens) == (55, 6, 2)
    assert row.cost_usd == Decimal("0")
    assert row.trace_id == "trace-err" and row.latency_ms == 88

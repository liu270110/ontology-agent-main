# tests/business/test_idle_gate.py
"""IdleGate 单测：四信号门控/令牌扣减/低峰直通（规格 06 篇 §5.5.2）。"""

from datetime import UTC, datetime

import fakeredis.aioredis

from services.memory.business.idle_gate import FixedSignals, IdleGate


def _gate(signals=None, **kw) -> IdleGate:
    defaults = dict(
        max_qps=5,
        max_queue_depth=3,
        max_llm_concurrency=1,
        max_active_sessions=2,
        tokens_per_window=2,
        off_peak_start_hour=1,
        off_peak_end_hour=7,
    )
    defaults.update(kw)
    return IdleGate(fakeredis.aioredis.FakeRedis(), signals or FixedSignals(), **defaults)


PEAK = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)  # 12 点：高峰
OFFPEAK = datetime(2026, 9, 26, 3, 0, tzinfo=UTC)  # 3 点：低峰


class OverloadedSignals(FixedSignals):
    async def global_qps(self) -> int:
        return 10  # > max_qps=5


async def test_off_peak_bypass():
    gate = _gate()
    assert await gate.try_acquire(OFFPEAK) is True
    assert await gate.try_acquire(OFFPEAK) is True  # 无令牌概念，恒放行


async def test_all_within_threshold_grants_tokens():
    gate = _gate()
    granted = await gate.evaluate(PEAK)
    assert granted == 2  # tokens_per_window


async def test_overloaded_signal_blocks():
    gate = _gate(signals=OverloadedSignals())
    assert await gate.evaluate(PEAK) == 0
    assert await gate.try_acquire(PEAK) is False  # 无令牌


async def test_try_acquire_consumes_tokens():
    gate = _gate()
    await gate.evaluate(PEAK)
    assert await gate.try_acquire(PEAK) is True
    assert await gate.try_acquire(PEAK) is True
    assert await gate.try_acquire(PEAK) is False  # 令牌耗尽

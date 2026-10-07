"""LLM 预算闸门测试（5005 RETRY_BUDGET_EXHAUSTED；Redis 计数热路径 + fail-open 降级）。"""

from __future__ import annotations

import pytest
from fakeredis import aioredis as fakeredis_aio

from services.platform.llm.budget import BudgetExhaustedError, LlmBudgetGate

TENANT = "00000000-0000-0000-0000-0000000000a1"
TENANT_B = "00000000-0000-0000-0000-0000000000b2"


def fake_redis() -> fakeredis_aio.FakeRedis:
    """与 conftest 同款替身（tests 包不可跨文件导入，本文件自含）。"""
    return fakeredis_aio.FakeRedis(decode_responses=True)


def _gate(redis, limit: int = 1000) -> LlmBudgetGate:  # noqa: ANN001
    return LlmBudgetGate(redis, limit_tokens=limit, window_s=3600)


async def test_预算内放行_按租户累计计数():
    redis = fake_redis()
    gate = _gate(redis, limit=1000)
    # Act
    await gate.acquire(TENANT, 300)
    await gate.acquire(TENANT, 300)
    # Assert：同租户累计、跨租户独立（key 带 tenant 前缀）
    assert await gate.current(TENANT) == 600
    assert await gate.current(TENANT_B) == 0


async def test_超预算抛5005_且超限计数保留防探测():
    redis = fake_redis()
    gate = _gate(redis, limit=500)
    await gate.acquire(TENANT, 400)
    # Act / Assert：超限拒绝（02 §7 已登记码 5005，禁新编）
    with pytest.raises(BudgetExhaustedError) as ei:
        await gate.acquire(TENANT, 200)
    assert ei.value.code == 5005
    assert "RETRY_BUDGET_EXHAUSTED" in str(ei.value)
    # Assert：超限计数保留（防 1-token 反复探测绕过）
    assert await gate.current(TENANT) == 600


async def test_Redis不可达_fail_open放行不阻塞LLM():
    class _BrokenRedis:
        async def incrby(self, key: str, amount: int) -> int:
            raise ConnectionError("redis down")

        async def expire(self, key: str, seconds: int) -> bool:
            raise ConnectionError("redis down")

        async def get(self, key: str) -> None:
            return None

    gate = _gate(_BrokenRedis(), limit=1)
    # Act / Assert：fail-open（02 §3 ⑤ 可用性优先同纪律），不抛错
    await gate.acquire(TENANT, 10_000)


async def test_无租户上下文_预算跳过():
    redis = fake_redis()
    gate = _gate(redis, limit=1)
    # Act / Assert：后台任务无租户（tenant_id_ctx=None）→ 不计量不拒绝
    await gate.acquire(None, 10_000)
    assert await gate.current(TENANT) == 0

"""租户 LLM token 预算闸门（architecture/07 §2.3 预算 + §5.4：Redis 计数热路径，PG 只做冷账本）。

- 窗口计数：``budget:llm:{tenant_id}`` INCRBY 预估 tokens，TTL=window_s（滑动窗口近似）；
- 超限抛 **5005 RETRY_BUDGET_EXHAUSTED**（02 §7 已登记平台码，禁新编）；
- 超限请求的计数保留（防反复 1-token 探测绕过）；
- Redis 不可达 fail-open + WARNING（02 §3 ⑤ 可用性优先同纪律——预算闸不应放大为全站不可用）；
- 计数为**预留式估算**（调用前按 prompt 体量估算），精确计量以 llm_calls 冷账本为准。
"""

from __future__ import annotations

import logging

from redis import asyncio as aioredis

from services.platform.ports.model_port import ModelPortError

logger = logging.getLogger("services.platform.llm.budget")

_DEFAULT_LIMIT_TOKENS = 500_000  # 租户单窗口预算（组合根可覆盖；config 收口随 M3 成本治理批次）
_DEFAULT_WINDOW_S = 3600


class BudgetExhaustedError(ModelPortError):
    """租户 LLM 预算耗尽（5005 RETRY_BUDGET_EXHAUSTED，02 §7 已登记段）。"""

    def __init__(self, message: str = "租户 LLM 预算已耗尽") -> None:
        super().__init__(5005, f"5005 RETRY_BUDGET_EXHAUSTED: {message}")


class LlmBudgetGate:
    """按租户的 token 预算闸门（acquire 在 LLM 调用前执行，事务外原则不受影响）。"""

    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        limit_tokens: int = _DEFAULT_LIMIT_TOKENS,
        window_s: int = _DEFAULT_WINDOW_S,
        key_prefix: str = "budget:llm",
    ) -> None:
        self._redis = redis
        self._limit_tokens = limit_tokens
        self._window_s = window_s
        self._key_prefix = key_prefix

    async def acquire(self, tenant_id: str | None, est_tokens: int) -> None:
        """预留 est_tokens；超预算抛 BudgetExhaustedError。无租户上下文（后台任务）跳过。"""
        if not tenant_id or est_tokens <= 0:
            return
        key = f"{self._key_prefix}:{tenant_id}"
        try:
            count = int(await self._redis.incrby(key, est_tokens))
            await self._redis.expire(key, self._window_s)
        except (aioredis.RedisError, OSError):
            logger.warning("预算计数 Redis 不可达，fail-open 放行（tenant=%s）", tenant_id)
            return
        if count > self._limit_tokens:
            raise BudgetExhaustedError(
                f"窗口（{self._window_s}s）预算 {self._limit_tokens} tokens 已耗尽，计数 {count}"
            )

    async def current(self, tenant_id: str) -> int:
        """当前窗口计数（观测/测试用）。"""
        value = await self._redis.get(f"{self._key_prefix}:{tenant_id}")
        return int(value) if value else 0

"""空闲闸门（06 篇 §5.5.2；确定性规则非 AI）：四信号全达标发令牌，低峰直通，deadline 兜底在任务层。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from redis.asyncio import Redis

_TOKENS_KEY = "idle:tokens"
_WINDOW_SECONDS = 30


class GateSignals(Protocol):
    async def global_qps(self) -> int: ...
    async def online_queue_depth(self) -> int: ...
    async def llm_concurrency(self) -> int: ...
    async def active_sessions(self) -> int: ...


class FixedSignals:
    """v1 默认信号源：恒 0（单机无观测数据时视为空闲）。"""

    async def global_qps(self) -> int:
        return 0

    async def online_queue_depth(self) -> int:
        return 0

    async def llm_concurrency(self) -> int:
        return 0

    async def active_sessions(self) -> int:
        return 0


class RedisSignals:
    """读既有 Redis 计数（02 篇键空间）；缺键视为 0。键名随 M1 中间件落地对齐。"""

    def __init__(self, redis: Redis) -> None:
        self._r = redis

    async def _count(self, key: str) -> int:
        raw = await self._r.get(key)
        return int(raw) if raw else 0

    async def global_qps(self) -> int:
        return await self._count("rl:global:qps")

    async def online_queue_depth(self) -> int:
        # 键=arq 队列名（opaque zset），深度读数用 ZCARD 待 M1 接线，v1 GET 缺键=0
        return await self._count("arq:online")

    async def llm_concurrency(self) -> int:
        return await self._count("llm:concurrency")

    async def active_sessions(self) -> int:
        return await self._count("sse:active_sessions")


class IdleGate:
    def __init__(
        self,
        redis: Redis,
        signals: GateSignals,
        *,
        max_qps: int,
        max_queue_depth: int,
        max_llm_concurrency: int,
        max_active_sessions: int,
        tokens_per_window: int = 4,
        off_peak_start_hour: int = 1,
        off_peak_end_hour: int = 7,
    ) -> None:
        self._r = redis
        self._signals = signals
        self._limits = (max_qps, max_queue_depth, max_llm_concurrency, max_active_sessions)
        self._tokens = tokens_per_window
        self._start = off_peak_start_hour
        self._end = off_peak_end_hour

    def is_off_peak(self, now: datetime) -> bool:
        return self._start <= now.hour < self._end

    async def evaluate(self, now: datetime) -> int:
        """四信号全达标（< 阈值）→ 发放令牌；返回本次发放数。低峰时段恒发。"""
        if self.is_off_peak(now):
            granted = self._tokens
        else:
            measured = (
                await self._signals.global_qps(),
                await self._signals.online_queue_depth(),
                await self._signals.llm_concurrency(),
                await self._signals.active_sessions(),
            )
            granted = self._tokens if all(m < lim for m, lim in zip(measured, self._limits, strict=True)) else 0
        if granted:
            await self._r.incrby(_TOKENS_KEY, granted)
            await self._r.expire(_TOKENS_KEY, _WINDOW_SECONDS)
        return granted

    async def try_acquire(self, now: datetime) -> bool:
        """空闲 worker 取任务前调用：低峰直通；否则扣一个令牌（无令牌 False）。"""
        if self.is_off_peak(now):
            return True
        raw = await self._r.get(_TOKENS_KEY)
        if not raw or int(raw) <= 0:
            return False
        remaining = await self._r.decr(_TOKENS_KEY)
        return remaining >= 0

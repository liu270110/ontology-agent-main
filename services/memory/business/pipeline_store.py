"""沉淀管线支撑设施（docs/memory/多层记忆设计.md §2.1 定稿口径）。

- checkpoint：Redis key ``mem:consolidate:ckpt:{tenant_id}:{session_id}``（SET/GET/DEL +
  TTL；每步完成即写、成功即清）。PG checkpoint 表（kb_pipeline_step 模式）DDL 回填
  database 篇为在册待办（memory §10），此前以 Redis 过渡——断点续跑语义一致，进程重启
  不丢（TTL 兜底回收）；
- 死信：Redis list **``memory:dead``**（memory §2.1：沉淀管线专用死信键，不落通用
  ``q:tasks:idle:dead``——按域独立便于按 session 重放与告警隔离）。RPUSH 保序 + LTRIM
  护栏防无限增长；载荷含 tenant_id/session_id/step/attempt/error/trace_id/payload（断点
  现场），重放=逐条 LPOP 幂等重跑（指纹判重兜底，§5.4），失败 RPUSH 回队尾不阻塞后继；
- 依赖倒置：业务层（consolidation.py）只依赖 CheckpointStore/DeadLetterSink 协议，
  Redis 实现仅在本模块由 api 装配（业务层零 Redis 依赖）。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable
from uuid import UUID

from redis import asyncio as aioredis

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

logger = logging.getLogger("services.memory.business.pipeline_store")

DEAD_LETTER_KEY: Final[str] = "memory:dead"  # memory §2.1 定稿键名（depth>0 进 ops/02 日检）
_DEAD_LETTER_MAX: Final[int] = 1000  # LTRIM 护栏：只保留最近 N 条（防无限增长；溢出丢最旧）

_CHECKPOINT_KEY: Final[str] = "mem:consolidate:ckpt:{tenant_id}:{session_id}"
_CHECKPOINT_TTL_S: Final[int] = 7 * 24 * 3600  # 断点保留一周（会话归档周期同量级；过期即放弃续跑）


@runtime_checkable
class CheckpointStore(Protocol):
    """沉淀 checkpoint 步进存储（协议面向业务层；每步完成即 save，成功 clear）。"""

    async def load(self, tenant_id: UUID, session_id: UUID) -> dict[str, Any] | None:
        """读断点现场（JSON dict）；无断点返回 None（从 extracting 全新起跑）。"""
        ...

    async def save(self, tenant_id: UUID, session_id: UUID, state: Mapping[str, Any]) -> None:
        """步进写入（整体覆盖；TTL 续期）。"""
        ...

    async def clear(self, tenant_id: UUID, session_id: UUID) -> None:
        """管线成功完成后清除（失败保留——重跑按断点续）。"""
        ...


@runtime_checkable
class DeadLetterSink(Protocol):
    """死信投递面（协议面向业务层；失败语义见模块 docstring）。"""

    async def push(self, payload: Mapping[str, Any]) -> None:
        """保序追加一条死信（载荷约定见模块 docstring）。"""
        ...


class RedisCheckpointStore:
    """checkpoint 的 Redis 实现（SET json + EXPIRE；键规范见模块 docstring）。"""

    def __init__(self, redis: aioredis.Redis, *, ttl_seconds: int = _CHECKPOINT_TTL_S) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def _key(tenant_id: UUID, session_id: UUID) -> str:
        return _CHECKPOINT_KEY.format(tenant_id=tenant_id, session_id=session_id)

    async def load(self, tenant_id: UUID, session_id: UUID) -> dict[str, Any] | None:
        raw = await self._redis.get(self._key(tenant_id, session_id))
        return None if raw is None else dict(json.loads(raw))

    async def save(self, tenant_id: UUID, session_id: UUID, state: Mapping[str, Any]) -> None:
        await self._redis.set(
            self._key(tenant_id, session_id),
            json.dumps(dict(state), ensure_ascii=False),
            ex=self._ttl_seconds,
        )

    async def clear(self, tenant_id: UUID, session_id: UUID) -> None:
        await self._redis.delete(self._key(tenant_id, session_id))


class RedisDeadLetterSink:
    """死信的 Redis list 实现（RPUSH 保序 + LTRIM 护栏；键=memory:dead）。"""

    def __init__(self, redis: aioredis.Redis, *, key: str = DEAD_LETTER_KEY, max_len: int = _DEAD_LETTER_MAX) -> None:
        self._redis = redis
        self._key = key
        self._max_len = max_len

    async def push(self, payload: Mapping[str, Any]) -> None:
        await self._redis.rpush(self._key, json.dumps(dict(payload), ensure_ascii=False))
        await self._redis.ltrim(self._key, -self._max_len, -1)  # 护栏：溢出丢最旧（日志已留痕于业务层）


async def read_dead_letters(
    redis: aioredis.Redis,
    *,
    key: str = DEAD_LETTER_KEY,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """按入队序读死信（LRANGE 0..limit-1；观测/人工核查面，不动队列）。"""
    raws = await redis.lrange(key, 0, limit - 1)
    return [dict(json.loads(raw)) for raw in raws]


async def replay_dead_letters(
    redis: aioredis.Redis,
    runner: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    key: str = DEAD_LETTER_KEY,
    limit: int = 100,
) -> tuple[int, int]:
    """逐条幂等重放：按入队序 LPOP → runner(payload)；成功丢弃、失败 RPUSH 回队尾。

    迭代次数=调用时队列深度（上限 limit）——每条死信本次恰好尝试一次，失败者按原相对序
    沉入队尾（不阻塞后继，也不在本调用内循环重试）；重放期间并发新入队的死信落在队尾
    不受影响。幂等性由重放走同管线保证（指纹判重 + checkpoint 续跑，§5.4/§2.1）。
    返回 (成功数, 失败数)。
    """
    done = failed = 0
    depth = min(int(await redis.llen(key)), limit)
    for _ in range(depth):
        raw = await redis.lpop(key)
        if raw is None:
            break
        try:
            await runner(dict(json.loads(raw)))
            done += 1
        except Exception as exc:  # noqa: BLE001 —— 单条失败不中断整批（记日志 + 回队尾，禁静默）
            await redis.rpush(key, raw)
            failed += 1
            logger.warning("死信重放失败（已回队尾 %s）: %s", key, exc)
    return done, failed

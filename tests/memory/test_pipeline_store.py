# tests/memory/test_pipeline_store.py
"""沉淀管线支撑设施单测（P2-3；fakeredis 替身零外部依赖）。

覆盖：checkpoint 存取/清除（Redis 键规范）、死信 RPUSH 保序 + LTRIM 护栏、
read_dead_letters 保序读、replay_dead_letters 逐条幂等重放（成功丢弃/失败回队尾）。
"""

from __future__ import annotations

import uuid
from typing import Any

from fakeredis import aioredis as fakeredis_aio

from services.memory.business.pipeline_store import (
    DEAD_LETTER_KEY,
    RedisCheckpointStore,
    RedisDeadLetterSink,
    read_dead_letters,
    replay_dead_letters,
)

TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
SESSION = uuid.UUID("00000000-0000-0000-0000-0000000000d2")


def _redis() -> fakeredis_aio.FakeRedis:
    return fakeredis_aio.FakeRedis(decode_responses=True)


async def test_checkpoint存取与清除_键带租户会话前缀():
    store = RedisCheckpointStore(_redis())
    # Act：无断点 → None
    assert await store.load(TENANT, SESSION) is None
    # Act：步进写入 → 读回一致
    await store.save(TENANT, SESSION, {"step": "extracting", "written": 0})
    assert await store.load(TENANT, SESSION) == {"step": "extracting", "written": 0}
    # Act：清除 → 复位
    await store.clear(TENANT, SESSION)
    assert await store.load(TENANT, SESSION) is None
    # Assert：键规范（mem:consolidate:ckpt:{tenant}:{session}）
    assert RedisCheckpointStore._key(TENANT, SESSION) == f"mem:consolidate:ckpt:{TENANT}:{SESSION}"


async def test_死信RPUSH保序_LTRIM护栏与保序读():
    redis = _redis()
    sink = RedisDeadLetterSink(redis, max_len=3)
    for i in range(5):  # 5 条入队，护栏只保留最近 3 条
        await sink.push({"session_id": str(i), "step": "writing", "error": f"e{i}"})
    letters = await read_dead_letters(redis)
    # Assert：保序=按发生时间（RPUSH 序），溢出丢最旧（护栏）
    assert [item["session_id"] for item in letters] == ["2", "3", "4"]
    assert await redis.llen(DEAD_LETTER_KEY) == 3


async def test_死信重放_成功逐条丢弃_失败回队尾不阻塞后继():
    redis = _redis()
    sink = RedisDeadLetterSink(redis)
    for i in range(3):
        await sink.push({"n": i})

    async def always_ok(_payload: dict[str, Any]) -> None:
        return None

    # Act：全部成功 → 队列清空
    assert await replay_dead_letters(redis, always_ok) == (3, 0)
    assert await read_dead_letters(redis) == []

    # Act：全部失败 → 回队尾（保序近似：失败沉底，不阻塞后继）
    async def always_fail(_payload: dict[str, Any]) -> None:
        raise RuntimeError("重放仍失败")

    await sink.push({"n": "a"})
    await sink.push({"n": "b"})
    assert await replay_dead_letters(redis, always_fail) == (0, 2)
    letters = await read_dead_letters(redis)
    assert [item["n"] for item in letters] == ["a", "b"]  # 失败不丢（回队尾，仍保相对序）

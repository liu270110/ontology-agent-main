# tests/data/test_l1_redis.py
"""L1 会话记忆块单测（fakeredis；规格 06 篇 §1.1 Letta 式记忆块）。"""

import fakeredis.aioredis
import pytest

from services.memory.data.cache.l1_redis import L1SessionStore


@pytest.fixture
async def store():
    r = fakeredis.aioredis.FakeRedis()
    yield L1SessionStore(r, ttl_seconds=3600)
    await r.aclose()


async def test_set_and_get_blocks(store):
    await store.set_block("s1", "persona", "你是电力分析助手")
    await store.set_block("s1", "user_profile", "用户偏好中文回答")
    blocks = await store.get_blocks("s1")
    assert blocks == {"persona": "你是电力分析助手", "user_profile": "用户偏好中文回答"}


async def test_merge_block(store):
    await store.merge_block("s1", "user_profile", "已知：负责人张三")
    await store.merge_block("s1", "user_profile", "已知：系统 A 依赖 B")
    assert await store.get_block("s1", "user_profile") == "已知：负责人张三\n已知：系统 A 依赖 B"


async def test_ttl_refresh_on_write(store):
    await store.set_block("s1", "persona", "x")
    ttl1 = await store.ttl("s1")
    assert 0 < ttl1 <= 3600


async def test_export_all(store):
    await store.set_block("s1", "persona", "x")
    exported = await store.export_all("s1")
    assert exported == [{"block": "persona", "content": "x"}]


async def test_get_missing_block_returns_none(store):
    assert await store.get_block("s1", "nope") is None


async def test_get_blocks_empty_session(store):
    assert await store.get_blocks("s1") == {}


async def test_corrupt_json_tolerated(store):
    await store.set_block("s1", "persona", "x")
    await store._r.hset("l1:session:s1", "user_profile", b"not-json")  # 手工注入坏值（模拟外部写入/损坏）
    assert await store.get_block("s1", "user_profile") is None
    blocks = await store.get_blocks("s1")
    assert blocks == {"persona": "x"}  # 坏块跳过，好块不受影响

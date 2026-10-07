# tests/gateway/test_sse_dual_replica.py
"""SSE 双副本实测（02 §8 多副本出口条件 P1-1；Redis Stream 后端，本地 Redis 不可达即跳过）。

模拟两进程：两个 RedisSseHub 实例各持独立连接对（async+sync），共享同一 Redis——
A 副本 publish → B 副本订阅者经 XREAD/XRANGE 收到，验证跨副本 fanout、Last-Event-ID
重放不重不漏、缺口 4301、重复消费幂等、seq 跨副本单调。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from redis import asyncio as aioredis

from services.gateway.sse import SseHub, build_sse_hub
from services.gateway.sse.redis_hub import RedisSseHub
from services.platform.config import Settings
from services.platform.errors import ErrorCode, GatewayError

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

pytestmark = pytest.mark.integration

_TTL_TEST_S = 120


def _hub(redis_url: str, *, max_len: int = 1000) -> RedisSseHub:
    """独立连接对的一副本（模拟独立进程；redis-py from_url 惰性连接）。"""
    import redis as sync_redis

    return RedisSseHub(
        aioredis.from_url(redis_url, decode_responses=True),
        sync_redis_client=sync_redis.from_url(redis_url, decode_responses=True, socket_timeout=2.0),
        max_len=max_len,
        ttl_seconds=_TTL_TEST_S,
        block_ms=100,  # 短分片轮询：close 及时生效 + 用例低时延
    )


@pytest.fixture
async def dual_hubs() -> AsyncIterator[tuple[RedisSseHub, RedisSseHub, uuid.UUID]]:
    """两副本 hub（独立连接对）+ 独立会话号；Redis 不可达即跳过整套件。"""
    settings = Settings()
    probe = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await probe.ping()
    except (aioredis.RedisError, OSError):
        await probe.aclose()
        pytest.skip("本地 Redis 不可达，跳过 SSE 双副本集成用例")
    await probe.aclose()

    hub_a, hub_b = _hub(settings.redis_url), _hub(settings.redis_url)
    session_id = uuid.uuid4()
    yield hub_a, hub_b, session_id
    await hub_a.purge(session_id)
    await hub_a.aclose()
    await hub_b.aclose()


async def _recv(subscription: Any, timeout: float = 2.0) -> Any:
    return await asyncio.wait_for(subscription.next(), timeout=timeout)


async def _drain(subscription: Any, count: int) -> list[int]:
    """收满 count 条事件并返回 seq 序列（超时即失败——「不漏」断言的另一半）。"""
    seqs: list[int] = []
    for _ in range(count):
        event = await _recv(subscription)
        assert event is not None
        seqs.append(event.seq)
    return seqs


async def test_双副本发布_B_副本订阅实时_fanout_不重不漏(dual_hubs):
    hub_a, hub_b, session_id = dual_hubs
    stream = hub_b.open_stream(session_id, last_event_id=None, heartbeat_s=0.0)  # B 副本订阅（实时）
    names = ["RUN_STARTED", "TEXT_MESSAGE_CONTENT", "RUN_FINISHED"]
    for name in names:  # A 副本发布
        await hub_a.publish(session_id, name, {"n": name})
    frames = [await asyncio.wait_for(anext(stream), timeout=2.0) for _ in names]
    assert [f.decode().split("event: ")[1].split("\n")[0] for f in frames] == names
    ids = [int(f.split(b"\n", 1)[0].split(b": ")[1]) for f in frames]
    assert ids == [1, 2, 3]  # seq 单调不重不漏（跨副本）


async def test_发布_seq_跨副本单调共享(dual_hubs):
    hub_a, hub_b, session_id = dual_hubs
    seq_a, _ = await hub_a.publish(session_id, "RUN_STARTED", {})
    seq_b, _ = await hub_b.publish(session_id, "RUN_FINISHED", {})  # 另一副本发布
    assert (seq_a, seq_b) == (1, 2)  # 共享 INCR：会话内全局单调（Lua 原子，追加序==seq 序）


async def test_跨副本_Last_Event_ID_重放不重不漏_并衔接实时(dual_hubs):
    hub_a, hub_b, session_id = dual_hubs
    for i in range(4):  # A 副本发布 seq=1..4
        await hub_a.publish(session_id, "TEXT_MESSAGE_CONTENT", {"delta": str(i)})
    subscription = hub_b.subscribe(session_id, last_event_id=1)  # B 副本重连（Last-Event-ID=1）
    assert await _drain(subscription, 3) == [2, 3, 4]  # 回放 seq>1（不重）
    await hub_a.publish(session_id, "RUN_FINISHED", {})  # A 副本发布 seq=5
    assert await _drain(subscription, 1) == [5]  # 无缝衔接实时（不漏）
    subscription.close()


async def test_重复消费幂等_同_last_event_id_重订阅回放一致(dual_hubs):
    hub_a, hub_b, session_id = dual_hubs
    for i in range(3):
        await hub_a.publish(session_id, "TEXT_MESSAGE_CONTENT", {"delta": str(i)})
    sub1 = hub_b.subscribe(session_id, last_event_id=0)
    first = await _drain(sub1, 3)
    sub1.close()
    sub2 = hub_b.subscribe(session_id, last_event_id=0)  # 同位点重订阅
    second = await _drain(sub2, 3)
    sub2.close()
    assert first == second == [1, 2, 3]  # 回放只读不烧（幂等，无重复/缺失）


async def test_跨副本缺口返回_4301_窗口边界可订阅(dual_hubs):
    """小窗口副本（MAXLEN=3）发布 5 条后重连：缺口即 4301，窗口边界可正常订阅。"""
    _, _, session_id = dual_hubs
    trimmed = _hub(Settings().redis_url, max_len=3)
    try:
        for i in range(5):
            await trimmed.publish(session_id, "TEXT_MESSAGE_CONTENT", {"delta": str(i)})
        with pytest.raises(GatewayError) as exc_info:
            trimmed.subscribe(session_id, last_event_id=1)  # last=1 < oldest-1=2 → 缺口
        assert exc_info.value.code == int(ErrorCode.SSE_REPLAY_EXPIRED)
        assert exc_info.value.status_code == 410
        assert trimmed.subscribe(session_id, last_event_id=2) is not None  # 窗口下边界（oldest-1）
        assert trimmed.subscribe(session_id, last_event_id=5) is not None  # 最新位
    finally:
        await trimmed.aclose()


async def test_未知会话带_id_订阅拒绝_不带则开新流(dual_hubs):
    hub_a, hub_b, _ = dual_hubs
    ghost = uuid.uuid4()
    with pytest.raises(GatewayError) as exc_info:
        hub_b.subscribe(ghost, last_event_id=3)
    assert exc_info.value.code == int(ErrorCode.SSE_REPLAY_EXPIRED)
    fresh = hub_b.subscribe(uuid.uuid4(), last_event_id=None)  # 不带 id：纯实时新订阅
    await hub_a.publish(ghost, "RUN_STARTED", {})  # 其他会话事件不投递
    with pytest.raises(TimeoutError):  # 无事件可收（阻塞等待即证未串流）
        await asyncio.wait_for(fresh.next(), timeout=0.2)
    fresh.close()


async def test_工厂_Redis可达选_Stream后端_不可达回落进程内(dual_hubs):
    settings = Settings()
    hub = await build_sse_hub(redis_url=settings.redis_url, probe_timeout_s=1.0)
    assert isinstance(hub, RedisSseHub)
    await hub.aclose()
    fallback_none = await build_sse_hub(redis_url=None)
    assert type(fallback_none) is SseHub  # 未配置 → 零参回落
    fallback_down = await build_sse_hub(redis_url="redis://localhost:59099/0", probe_timeout_s=0.3)
    assert type(fallback_down) is SseHub  # 不可达 → 回落（WARNING 留痕）

"""L1 会话工作记忆存储单测（fakeredis 替身，零外部依赖；memory §7 key 规范 + 降级契约）。"""

from __future__ import annotations

import uuid

from services.memory.data.l1 import RedisL1Store
from services.memory.domain.model.l1 import MemoryBlock, WindowMessage

TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
SESSION_1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
SESSION_2 = uuid.UUID("00000000-0000-0000-0000-000000000002")

# Redis 不可达替身：全部操作抛 ConnectionError（OSError 子类，命中降级捕获面）


class _BrokenRedis:
    def pipeline(self):  # noqa: ANN201
        raise ConnectionError("redis down")

    async def hgetall(self, key: str) -> dict[str, str]:
        raise ConnectionError("redis down")

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        raise ConnectionError("redis down")

    async def get(self, key: str) -> str | None:
        raise ConnectionError("redis down")

    async def delete(self, *keys: str) -> int:
        raise ConnectionError("redis down")


def _store(redis, ttl: int = 3600) -> RedisL1Store:  # noqa: ANN001
    return RedisL1Store(redis, ttl_seconds=ttl, window_size=3)


async def test_L1_blocks_window_state_roundtrip_且写入续期TTL():
    from fakeredis import aioredis as fakeredis_aio

    store = _store(fakeredis_aio.FakeRedis(decode_responses=True))
    # Act：三键写入
    await store.write_blocks(TENANT_A, SESSION_1, [MemoryBlock(key="profile", title="画像", content="偏好结论先行")])
    await store.append_window(
        TENANT_A,
        SESSION_1,
        [WindowMessage(role="user", content="分析停电原因"), WindowMessage(role="assistant", content="好的")],
    )
    await store.write_state(TENANT_A, SESSION_1, {"draft": "step-2"})
    # Assert：读回一致
    snap = await store.read(TENANT_A, SESSION_1)
    assert snap.blocks["profile"].content == "偏好结论先行"
    assert [m.role for m in snap.window] == ["assistant", "user"]  # 新→旧（LPUSH 序）
    assert snap.state == {"draft": "step-2"}
    assert snap.degraded is False


async def test_L1_滑动窗口_LTRIM只保留最近N条():
    from fakeredis import aioredis as fakeredis_aio

    store = _store(fakeredis_aio.FakeRedis(decode_responses=True), ttl=3600)
    # Act：window_size=3 下追加 5 条
    await store.append_window(TENANT_A, SESSION_1, [WindowMessage(role="user", content=f"m{i}") for i in range(5)])
    # Assert：仅保留最近 3 条且新→旧
    snap = await store.read(TENANT_A, SESSION_1)
    assert [m.content for m in snap.window] == ["m4", "m3", "m2"]


async def test_L1_租户隔离_同会话号跨租户互不可见():
    from fakeredis import aioredis as fakeredis_aio

    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    store = _store(redis)
    # Arrange：租户 A 写入
    await store.write_blocks(TENANT_A, SESSION_1, [MemoryBlock(key="k", content="A 的秘密")])
    # Act：租户 B 同会话号读取
    snap_b = await store.read(TENANT_B, SESSION_1)
    # Assert：空快照（key 带 tenant 前缀，08 §1）
    assert snap_b.blocks == {}
    # Assert：同租户不同会话亦隔离
    snap_other = await store.read(TENANT_A, SESSION_2)
    assert snap_other.blocks == {}


async def test_L1_Redis不可达_降级返回空快照且写入不抛(caplog):  # noqa: ANN001
    store = _store(_BrokenRedis())
    # Act / Assert：读降级（degraded=True，空快照）、写静默丢弃，全程不抛
    snap = await store.read(TENANT_A, SESSION_1)
    assert snap.degraded is True and snap.blocks == {} and snap.window == []
    assert await store.write_blocks(TENANT_A, SESSION_1, [MemoryBlock(key="k", content="x")]) == 0
    await store.write_state(TENANT_A, SESSION_1, {"a": 1})
    await store.delete_all(TENANT_A, SESSION_1)
    assert store.degraded is True  # 禁用标记（恢复需重建实例，见报告遗留）


async def test_L1_归档清理_三键全删():
    from fakeredis import aioredis as fakeredis_aio

    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    store = _store(redis)
    await store.write_blocks(TENANT_A, SESSION_1, [MemoryBlock(key="k", content="x")])
    await store.append_window(TENANT_A, SESSION_1, [WindowMessage(role="user", content="hi")])
    # Act
    await store.delete_all(TENANT_A, SESSION_1)
    # Assert
    snap = await store.read(TENANT_A, SESSION_1)
    assert snap.blocks == {} and snap.window == [] and snap.state is None


def test_L1_key规范_三键均带tenant前缀():
    """memory §7：mem:l1:{tenant_id}:{session_id}:{blocks|window|state}（08 §1 租户隔离）。"""
    assert RedisL1Store._blocks_key(TENANT_A, SESSION_1) == f"mem:l1:{TENANT_A}:{SESSION_1}:blocks"
    assert RedisL1Store._window_key(TENANT_A, SESSION_1) == f"mem:l1:{TENANT_A}:{SESSION_1}:window"
    assert RedisL1Store._state_key(TENANT_A, SESSION_1) == f"mem:l1:{TENANT_A}:{SESSION_1}:state"


# ── 降级→恢复（P2-4：半开探测自动恢复，无需重建实例）──────────────────────


class _FlakyRedis:
    """可切换故障替身：down=True 时全部操作抛 ConnectionError；ping 调用计数可断言。"""

    def __init__(self, inner) -> None:  # noqa: ANN001
        self._inner = inner
        self.down = False
        self.ping_calls = 0

    async def ping(self):  # noqa: ANN201
        self.ping_calls += 1
        if self.down:
            raise ConnectionError("redis down")
        return await self._inner.ping()

    def pipeline(self):  # noqa: ANN201
        if self.down:
            raise ConnectionError("redis down")
        return self._inner.pipeline()

    def __getattr__(self, name):  # 其余操作代理（读路径 hgetall/lrange/get/delete）
        attr = getattr(self._inner, name)

        async def _call(*args, **kwargs):  # noqa: ANN002,ANN003,ANN201
            if self.down:
                raise ConnectionError("redis down")
            return await attr(*args, **kwargs)

        return _call


async def test_L1_降级后半开探测恢复_读写正常且原数据不丢():
    from fakeredis import aioredis as fakeredis_aio

    flaky = _FlakyRedis(fakeredis_aio.FakeRedis(decode_responses=True))
    store = RedisL1Store(flaky, ttl_seconds=3600, window_size=3, probe_interval_s=0.0, probe_timeout_s=1.0)
    # Arrange：健康期写入
    await store.write_blocks(TENANT_A, SESSION_1, [MemoryBlock(key="k", content="降级前写入")])
    # Act：故障 → 降级
    flaky.down = True
    snap = await store.read(TENANT_A, SESSION_1)
    assert snap.degraded is True and store.degraded is True
    # Act：恢复 → 下一次读半开探测通过，自动恢复（无需重建实例）
    flaky.down = False
    snap2 = await store.read(TENANT_A, SESSION_1)
    # Assert：degraded=false + 降级期间数据未失 + 写路径恢复正常
    assert store.degraded is False and snap2.degraded is False
    assert snap2.blocks["k"].content == "降级前写入"
    assert await store.write_blocks(TENANT_A, SESSION_1, [MemoryBlock(key="k2", content="恢复后写入")]) == 1
    assert await store.append_window(TENANT_A, SESSION_1, [WindowMessage(role="user", content="恢复后消息")]) == 1


async def test_L1_降级探测冷却窗内不反复探测_窗满后才探测():
    from fakeredis import aioredis as fakeredis_aio

    flaky = _FlakyRedis(fakeredis_aio.FakeRedis(decode_responses=True))
    store = RedisL1Store(flaky, ttl_seconds=3600, probe_interval_s=3600.0, probe_timeout_s=1.0)
    # Act：降级后冷却窗（3600s）内连续读——不触发探测（防故障期探测风暴）
    flaky.down = True
    await store.read(TENANT_A, SESSION_1)
    await store.read(TENANT_A, SESSION_1)
    # Assert：窗内 ping 零调用且保持降级（首次降级来自操作失败，不额外探测）
    assert flaky.ping_calls == 0 and store.degraded is True

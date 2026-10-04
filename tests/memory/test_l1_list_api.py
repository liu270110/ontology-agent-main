"""GET /memory/l1 列表态测试（B8-WB 断头补齐 2026-10-04；前端 api.ts listL1 消费形状）。

零外部依赖（fakeredis 替身，test_l1_store.py 同款纪律）；端点函数直调=tests/memory 惯例。
覆盖：两会话→两条目聚合（条目字段=前端 L1Session 逐字段对位）、空存储→空数组、
Redis 降级→空数组、三键任一存在即活跃（仅 window 会话不漏）、租户隔离、limit 截断。
"""

from __future__ import annotations

import uuid
from uuid import uuid4

from fakeredis import aioredis as fakeredis_aio

from services.memory.api.memory import list_l1_sessions
from services.memory.data.l1 import RedisL1Store
from services.memory.domain.model.l1 import MemoryBlock, WindowMessage
from services.platform.deps import Principal


def _store(redis) -> RedisL1Store:  # noqa: ANN001
    return RedisL1Store(redis, ttl_seconds=3600)


def _principal(tenant_id: uuid.UUID) -> Principal:
    return Principal(
        {
            "sub": str(uuid4()),
            "tenant_id": str(tenant_id),
            "roles": ["member"],
            "scopes": ["memory:read"],
            "typ": "access",
            "jti": uuid4().hex,
        }
    )


class _BrokenRedis:
    """Redis 不可达替身：scan 起步即抛 ConnectionError（OSError 子类，命中降级捕获面）。"""

    async def scan_iter(self, match: str, count: int = 100):  # noqa: ANN201, ARG002
        raise ConnectionError("redis down")
        yield  # pragma: no cover —— 使其成为异步生成器


async def test_GET_memory_l1_两会话聚合_条目字段与前端L1Session逐字段对位():
    # Arrange：租户内两个活跃会话（甲=blocks+window+state 三键；乙=仅 blocks）
    tenant = uuid4()
    sid_a, sid_b = uuid4(), uuid4()
    store = _store(fakeredis_aio.FakeRedis(decode_responses=True))
    await store.write_blocks(
        tenant,
        sid_a,
        [
            MemoryBlock(key="profile", content="偏好结论先行"),  # 无 title（title 代理取首个非空）
            MemoryBlock(key="task", title="任务", content="停电归因分析"),
        ],
    )
    await store.append_window(tenant, sid_a, [WindowMessage(role="user", content="查一下城东停电原因")])
    await store.write_state(tenant, sid_a, {"step": "grounding"})
    await store.write_blocks(tenant, sid_b, [MemoryBlock(key="draft", title="草稿", content="step-2")])
    # Act
    out = await list_l1_sessions(principal=_principal(tenant), l1=store)
    # Assert：两条目聚合（信封 {data, meta:{page,page_size,total}}，M4.6-D3 B1 批统一；
    # L1 SCAN 聚合无全量 count → total=len(data) v1 口径）
    assert len(out.data) == 2 and out.meta.total == 2
    assert out.meta.page == 1 and out.meta.page_size == 50
    assert {s.session_id for s in out.data} == {sid_a, sid_b}
    # Assert：条目键集=前端 L1Session 逐字段（api.ts 契约面）
    by_id = {s.session_id: s for s in out.data}
    item = by_id[sid_a]
    assert set(item.model_dump()) == {"session_id", "title", "ttl_total_s", "ttl_remaining_s", "blocks"}
    assert set(item.blocks[0].model_dump()) == {"key", "value", "masked"}
    # Assert：title=首个非空块 title 的块级代理（profile 无 title → 取 task 的「任务」）
    assert item.title == "任务"
    assert item.ttl_total_s == 3600 and 0 < item.ttl_remaining_s <= 3600
    # Assert：blocks 按 key 稳定序展开 {key,value} 行；masked 缺省 False（掩码引擎未接入）
    assert [(b.key, b.value, b.masked) for b in item.blocks] == [
        ("profile", "偏好结论先行", False),
        ("task", "停电归因分析", False),
    ]
    assert by_id[sid_b].title == "草稿"
    assert [(b.key, b.value) for b in by_id[sid_b].blocks] == [("draft", "step-2")]


async def test_GET_memory_l1_空存储_空数组():
    empty_store = _store(fakeredis_aio.FakeRedis(decode_responses=True))
    out = await list_l1_sessions(principal=_principal(uuid4()), l1=empty_store)
    assert out.data == [] and out.meta.total == 0  # 契约形状：无活跃会话 → data=[]（非 404/非 null）


async def test_GET_memory_l1_Redis降级_空数组不阻塞():
    store = _store(_BrokenRedis())
    out = await list_l1_sessions(principal=_principal(uuid4()), l1=store)
    assert out.data == [] and store.degraded is True  # 降级契约：空列表 + 观测位（容量卡空态）


async def test_GET_memory_l1_仅window会话不漏_租户隔离():
    tenant = uuid4()
    sid_win = uuid4()
    store = _store(fakeredis_aio.FakeRedis(decode_responses=True))
    await store.append_window(tenant, sid_win, [WindowMessage(role="user", content="只有窗口的消息")])
    await store.write_blocks(uuid4(), uuid4(), [MemoryBlock(key="x", content="他租户噪声")])
    # Act / Assert：三键任一存在即活跃（仅 window 会话入列）；他租户键不入列
    out = await list_l1_sessions(principal=_principal(tenant), l1=store)
    assert [s.session_id for s in out.data] == [sid_win]
    assert out.data[0].blocks == [] and out.data[0].title == ""  # 无 blocks：空块行 + 空 title
    assert out.data[0].ttl_remaining_s > 0  # TTL 取三键最大值（window 键续期生效）


async def test_GET_memory_l1_limit截断_TTL剩余降序():
    tenant = uuid4()
    store = _store(fakeredis_aio.FakeRedis(decode_responses=True))
    sids = [uuid4() for _ in range(3)]
    for i, sid in enumerate(sids):
        await store.write_blocks(tenant, sid, [MemoryBlock(key=f"k{i}", content=f"v{i}")])
    out = await list_l1_sessions(principal=_principal(tenant), l1=store, limit=2)
    # Assert：limit 截断；TTL 同值并列按 session_id 稳定序（可重放）
    assert len(out.data) == 2 and out.meta.total == 2  # total=len(data)（SCAN 聚合无 count，v1 口径）
    ttls = [s.ttl_remaining_s for s in out.data]
    assert ttls == sorted(ttls, reverse=True)
    ids = [str(s.session_id) for s in out.data]
    assert ids == sorted(ids)

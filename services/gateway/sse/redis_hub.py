"""SSE Redis Stream 后端（02 §5 写入与推送分离的多副本形态；M3 出口条件 P1-1）。

设计取舍（报告《评审-2026-09-27-M3批次验收》P1-1 修复包）：

- **事件落 Redis Stream**：key ``sse:stream:{session_id}``，XADD ``MAXLEN``（护栏，
  同 02 §5 建议值 1000）+ EXPIRE（TTL 10min，滚动续期）——跨副本共享回放窗口；
- **seq 分配与追加原子化**：Lua 脚本内 INCR(seq) + XADD 同脚本执行，Redis 单线程串行
  保证「Stream 追加序 == seq 序」跨副本成立（否则两副本 INCR/XADD 交错会乱序）；
  seq 语义与进程内 SseHub 完全一致（会话内单调 int，Last-Event-ID 契约不变）；
- **消费=每订阅者独立 XREAD**（非消费组）：SSE fanout 是广播语义——每个副本都要把每条
  事件投给本地订阅者，消费组「一条消息只投一个消费者」是错误语义且引入 XACK 账本；
  XREAD 游标无服务端状态，断连即弃、重连即以 Last-Event-ID 重读（天然重放）；
- **Last-Event-ID 映射**：客户端 id 即会话内 seq（int，与端点 api/01 契约一致）；
  回放=XRANGE 快照内 ``seq > last_event_id`` 的条目；缺口（快照最旧 seq-1 > last）或
  id 超前 → 4301 SSE_REPLAY_EXPIRED（HTTP 410，在 subscribe 同步抛出——与进程内
  hub 同一契约，端点无须改动即可在建流前转统一错误体；为此快照读取用同步连接，
  XRANGE 有界（≤MAXLEN 条）亚毫秒级，订阅频度下可接受）；
- **零参构造回落**：``build_sse_hub`` 工厂——Redis 不可达/未配置 → 进程内 SseHub
  （行为不变，存量测试不受影响）；可达 → RedisSseHub 跨副本 fanout。

组合根接线（app.py 本批禁改，留一行切换待合入）::

    app.state.sse_hub = await build_sse_hub(redis_url=s.redis_url)
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any, Final

import redis as sync_redis
from redis import asyncio as aioredis

from services.gateway.sse.events import SseEvent, encode_frame
from services.gateway.sse.hub import SseHub, stream_frames
from services.platform.errors import ErrorCode, GatewayError

logger = logging.getLogger("services.gateway.sse.redis_hub")

_STREAM_KEY: Final[str] = "sse:stream:{session_id}"
_SEQ_KEY: Final[str] = "sse:seq:{session_id}"

# INCR+XADD+EXPIRE 原子化（追加序==seq 序，见模块 docstring）；KEYS/ARGV 见 publish()
_PUBLISH_LUA: Final[str] = (
    "local seq = redis.call('INCR', KEYS[2]) "
    "redis.call('XADD', KEYS[1], 'MAXLEN', ARGV[1], '*', "
    "'seq', seq, 'name', ARGV[2], 'data', ARGV[3]) "
    "redis.call('EXPIRE', KEYS[1], ARGV[4]) "
    "redis.call('EXPIRE', KEYS[2], ARGV[4]) "
    "return tostring(seq)"
)

_SENTINEL = object()  # 订阅关闭信号（与 hub._SENTINEL 同构，包内各自独立）


class RedisSseHub:
    """跨副本 SSE 枢纽：publish（Lua 原子发布）/ subscribe（快照回放+XREAD 实时）/ open_stream。

    双客户端：异步客户端承载 publish 与订阅实时读（事件循环内）；同步客户端仅用于
    subscribe 时的 XRANGE 快照（4301 须在 open_stream 返回前同步抛出，进程内 hub 同契约，
    端点零改动——见模块 docstring「Last-Event-ID 映射」）。快照有界（≤max_len 条）。
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        sync_redis_client: sync_redis.Redis,
        max_len: int = 1000,
        ttl_seconds: int = 600,
        block_ms: int = 500,
    ) -> None:
        self._redis = redis
        self._sync_redis = sync_redis_client
        self._max_len = max_len
        self._ttl_seconds = ttl_seconds
        self._block_ms = block_ms

    # ── 生产侧 ───────────────────────────────────────────────────────────
    async def publish(self, session_id: uuid.UUID, name: str, data: Mapping[str, Any]) -> tuple[int, bytes]:
        """原子发布：INCR seq + XADD（MAXLEN 护栏 + TTL 滚动续期）；返回 (seq, 帧)。

        返回帧供生产连接直接下发（02 §5 写入与推送分离——本连接不消费自己的队列）。
        """
        payload = json.dumps(dict(data), ensure_ascii=False, separators=(",", ":"))
        raw_seq = await self._redis.eval(
            _PUBLISH_LUA,
            2,
            _STREAM_KEY.format(session_id=session_id),
            _SEQ_KEY.format(session_id=session_id),
            self._max_len,
            name,
            payload,
            self._ttl_seconds,
        )
        seq = int(raw_seq)
        return seq, encode_frame(SseEvent(seq=seq, name=name, data=data))

    # ── 消费侧 ───────────────────────────────────────────────────────────
    def subscribe(self, session_id: uuid.UUID, *, last_event_id: int | None = None) -> RedisSseSubscription:
        """订阅：同步 XRANGE 快照 → 回放窗口判定（缺口/超前=4301，同步抛出）→ XREAD 实时。

        须在运行中的事件循环内调用（订阅者后台读任务随构造创建）。
        """
        stream_key = _STREAM_KEY.format(session_id=session_id)
        entries = self._snapshot(stream_key)
        replay: list[SseEvent] = []
        if last_event_id is not None:
            if not entries:
                raise GatewayError(
                    ErrorCode.SSE_REPLAY_EXPIRED,
                    "SSE 回放窗口不可用（会话无缓冲事件），请拉全量历史后重新订阅",
                    status_code=410,
                )
            oldest = int(entries[0][1]["seq"])
            latest = int(entries[-1][1]["seq"])
            if last_event_id > latest or last_event_id < oldest - 1:
                raise GatewayError(
                    ErrorCode.SSE_REPLAY_EXPIRED,
                    f"SSE 回放窗口已过期（缓冲最早 seq={oldest}，最新 seq={latest}，"
                    f"请求 last_event_id={last_event_id}）",
                    status_code=410,
                )
            replay = [
                SseEvent(seq=int(fields["seq"]), name=str(fields["name"]), data=json.loads(fields["data"]))
                for _entry_id, fields in entries
                if int(fields["seq"]) > last_event_id
            ]
        # 实时游标=快照末条 id：XRANGE 与首读之间新追加的条目 id 更大，XREAD 不漏；
        # 快照为空（新会话/已过期）从 0 起读——空快照保证不存在「旧条目」，等价纯实时。
        cursor = entries[-1][0] if entries else "0-0"
        return RedisSseSubscription(self._redis, stream_key, replay, cursor, block_ms=self._block_ms)

    def open_stream(
        self,
        session_id: uuid.UUID,
        *,
        last_event_id: int | None = None,
        heartbeat_s: float = 15.0,
    ) -> AsyncIterator[bytes]:
        """订阅并返回帧流（4301 在调用时同步抛出；心跳语义与进程内 hub 一致）。"""
        subscription = self.subscribe(session_id, last_event_id=last_event_id)
        return stream_frames(subscription, heartbeat_s=heartbeat_s)

    # ── 运维/停机 ────────────────────────────────────────────────────────
    async def purge(self, session_id: uuid.UUID) -> None:
        """清空单会话的 Stream 与 seq 键（测试/运维；TTL 亦可自然过期）。"""
        await self._redis.delete(
            _STREAM_KEY.format(session_id=session_id),
            _SEQ_KEY.format(session_id=session_id),
        )

    async def aclose(self) -> None:
        """停机回收：关闭双客户端连接池（组合根 lifespan 停机段调用）。"""
        await self._redis.aclose()
        self._sync_redis.close()

    def _snapshot(self, stream_key: str) -> list[tuple[str, dict[str, str]]]:
        """同步 XRANGE 全量快照（≤max_len 条；4301 判定与回放的数据源，见类 docstring）。"""
        try:
            return list(self._sync_redis.xrange(stream_key, min="-", max="+"))
        except (sync_redis.RedisError, OSError) as exc:
            # 订阅期 Redis 抖动：按「回放窗口不可用」处理（带 id 重连客户端收到 4301）
            raise GatewayError(
                ErrorCode.SSE_REPLAY_EXPIRED,
                f"SSE 回放快照读取失败（Redis 不可达）: {exc}",
                status_code=410,
            ) from exc


class RedisSseSubscription:
    """单订阅者游标：快照回放先入队，后台任务 XREAD 实时补入；close 后 next() 返回 None。

    幂等：seq ≤ 已投递最大 seq 的条目不重发（重复消费/回放-实时衔接窗口双保险）。
    Redis 故障 → 流终止（next() 返回 None），客户端按 Last-Event-ID 语义重连。
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        stream_key: str,
        replay: list[SseEvent],
        cursor: str,
        *,
        block_ms: int,
    ) -> None:
        self._redis = redis
        self._stream_key = stream_key
        self._cursor = cursor
        self._block_ms = block_ms
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        for event in replay:
            self._queue.put_nowait(event)
        self._last_seq = replay[-1].seq if replay else None
        self._closed = False
        self._task = asyncio.create_task(self._reader())

    async def next(self) -> SseEvent | None:
        """取下一条事件；流关闭返回 None（幂等）。"""
        if self._closed:
            return None
        item = await self._queue.get()
        if item is _SENTINEL:
            self._closed = True
            return None
        return item  # type: ignore[no-any-return]

    def close(self) -> None:
        """退订（幂等）：终止后台读任务并唤醒等待者。"""
        if self._closed:
            return
        self._closed = True
        self._task.cancel()
        self._queue.put_nowait(_SENTINEL)

    async def _reader(self) -> None:
        """XREAD 阻塞轮询（block_ms 分片，保证 close 及时生效）→ 本地队列 fanout。"""
        try:
            while not self._closed:
                response = await self._redis.xread({self._stream_key: self._cursor}, block=self._block_ms, count=64)
                for _key, entries in response or []:
                    for entry_id, fields in entries:
                        self._cursor = entry_id
                        seq = int(fields.get("seq", 0))
                        if self._last_seq is not None and seq <= self._last_seq:
                            continue  # 幂等：重复消费不重发
                        self._last_seq = seq
                        self._queue.put_nowait(
                            SseEvent(seq=seq, name=str(fields["name"]), data=json.loads(fields["data"]))
                        )
        except asyncio.CancelledError:
            raise
        except (aioredis.RedisError, OSError) as exc:
            if not self._closed:
                logger.warning("SSE Redis Stream 订阅中断（%s），流终止——客户端须带 Last-Event-ID 重连", exc)
                self._queue.put_nowait(_SENTINEL)


async def build_sse_hub(
    *,
    redis_url: str | None,
    max_len: int = 1000,
    ttl_seconds: int = 600,
    probe_timeout_s: float = 1.0,
) -> SseHub | RedisSseHub:
    """组合根工厂：Redis 可达 → RedisSseHub（跨副本）；未配置/不可达 → 进程内 SseHub（回落）。

    探测超时 probe_timeout_s（默认 1s）：启动期一次 ping，不阻塞 lifespan 超过该上限。
    回落时记 WARNING 留痕（观测面：单副本形态运行）。
    """
    if not redis_url:
        logger.warning("未配置 redis_url，SSE 枢纽回落进程内实现（单副本形态）")
        return SseHub(buffer_size=max_len)
    try:
        client = aioredis.from_url(redis_url, decode_responses=True, socket_connect_timeout=probe_timeout_s)
        await asyncio.wait_for(client.ping(), probe_timeout_s)
    except (aioredis.RedisError, OSError, TimeoutError) as exc:
        logger.warning("Redis 不可达（%s），SSE 枢纽回落进程内实现（单副本形态）", exc)
        return SseHub(buffer_size=max_len)
    sync_client = sync_redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=probe_timeout_s,
        socket_timeout=max(2.0, probe_timeout_s),  # 同步快照读护栏：XRANGE 有界仍须超时必设
    )
    logger.info("SSE 枢纽启用 Redis Stream 后端（跨副本 fanout，max_len=%d ttl=%ds）", max_len, ttl_seconds)
    return RedisSseHub(client, sync_redis_client=sync_client, max_len=max_len, ttl_seconds=ttl_seconds)

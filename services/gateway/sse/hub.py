"""SSE 事件缓冲与多订阅 fanout（02 §5 写入与推送分离的进程内形态）。

- 会话级事件缓冲：seq 单调递增、deque 上限=buffer_size（02 §5 Redis Stream MAXLEN 1000
  同参；TTL 10min 与跨副本共享见同包 redis_hub 的 Redis Stream 后端——本类保持零参构造
  可用，作为「无 Redis 配置/不可达」的回落实现）；
- 多订阅 fanout：每订阅者独立 asyncio.Queue，publish 广播，慢消费者背压（256KB 水位）
  随 M4 落地（02 §5 建议值，本批不实现）；
- Last-Event-ID 重放：订阅时从缓冲回放 seq > last_event_id 的历史帧再切实时；
  超出回放窗口（缺口）抛 4301 SSE_REPLAY_EXPIRED（api/01 §4.3，HTTP 410）。

帧流心跳循环（stream_frames）为两后端共用：订阅者只须满足 EventSubscription 协议
（next()/close() 鸭子类型，redis_hub.RedisSseSubscription 结构化满足）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol

from services.gateway.sse.events import SseEvent, encode_frame
from services.platform.errors import ErrorCode, GatewayError

_SENTINEL = object()  # 订阅关闭信号（队列内哨兵，与事件对象互斥）


class EventSubscription(Protocol):
    """订阅者最小协议（进程内 SseSubscription 与 Redis 订阅共同的鸭子类型面）。"""

    async def next(self) -> SseEvent | None:
        """取下一条事件；流关闭返回 None。"""
        ...

    def close(self) -> None:
        """退订（幂等）。"""
        ...


class _Channel:
    """单会话事件通道：seq 分配 + 环形缓冲 + 订阅者集合（单事件循环内使用，无锁）。"""

    __slots__ = ("buffer", "seq", "subscribers")

    def __init__(self, buffer_size: int) -> None:
        self.seq = 0
        self.buffer: deque[SseEvent] = deque(maxlen=buffer_size)
        self.subscribers: list[asyncio.Queue[Any]] = []

    def allocate(self, name: str, data: Mapping[str, Any]) -> SseEvent:
        self.seq += 1
        event = SseEvent(seq=self.seq, name=name, data=data)
        self.buffer.append(event)
        return event


class SseSubscription:
    """单订阅者游标：回放队列耗尽后无缝切实时；close 后 next() 返回 None。"""

    def __init__(self, channel: _Channel, replay: list[SseEvent]) -> None:
        self._channel = channel
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        for event in replay:
            self._queue.put_nowait(event)
        channel.subscribers.append(self._queue)
        self._closed = False

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
        """退订（幂等）：移出广播集合并唤醒等待者。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._channel.subscribers.remove(self._queue)
        except ValueError:
            pass
        self._queue.put_nowait(_SENTINEL)


class SseHub:
    """进程内 SSE 枢纽：publish（含帧编码）/ subscribe（Last-Event-ID 重放）/ open_stream。"""

    def __init__(self, *, buffer_size: int = 1000) -> None:
        self._buffer_size = buffer_size
        self._channels: dict[uuid.UUID, _Channel] = {}

    # ── 生产侧（POST messages 快路径：发布 + 编码一步返回）────────────────
    def publish(self, session_id: uuid.UUID, name: str, data: Mapping[str, Any]) -> tuple[int, bytes]:
        """发布一条事件：分配会话内单调 seq、入缓冲、fanout 全部订阅者；返回 (seq, 帧)。

        返回帧供生产连接直接下发（02 §5：写入与推送分离——本连接不消费自己的队列）。
        """
        channel = self._channels.setdefault(session_id, _Channel(self._buffer_size))
        event = channel.allocate(name, data)
        for queue in list(channel.subscribers):
            queue.put_nowait(event)
        return event.seq, encode_frame(event)

    # ── 消费侧（GET events 重连 / 第二连接 fanout）────────────────────────
    def subscribe(self, session_id: uuid.UUID, *, last_event_id: int | None = None) -> SseSubscription:
        """订阅：last_event_id 给定时先回放缓冲（缺口=超回放窗口 → 4301），再切实时。"""
        channel = self._channels.setdefault(session_id, _Channel(self._buffer_size))
        return SseSubscription(channel, self._replay(channel, last_event_id))

    def _replay(self, channel: _Channel, last_event_id: int | None) -> list[SseEvent]:
        """回放窗口计算：缺口即 4301（客户端须拉全量历史后重新订阅，02 §5）。"""
        if last_event_id is None:
            return []
        if last_event_id > channel.seq:
            # 本进程从未发布过该会话事件（新通道 seq=0）或 id 超前：回放窗口不可用
            raise GatewayError(
                ErrorCode.SSE_REPLAY_EXPIRED,
                "SSE 回放窗口不可用（会话无缓冲事件或 id 超前），请拉全量历史后重新订阅",
                status_code=410,
            )
        oldest = channel.buffer[0].seq if channel.buffer else channel.seq + 1
        if last_event_id < oldest - 1:
            raise GatewayError(
                ErrorCode.SSE_REPLAY_EXPIRED,
                f"SSE 回放窗口已过期（缓冲最早 seq={oldest}，请求 last_event_id={last_event_id}）",
                status_code=410,
            )
        return [event for event in channel.buffer if event.seq > last_event_id]

    def open_stream(
        self,
        session_id: uuid.UUID,
        *,
        last_event_id: int | None = None,
        heartbeat_s: float = 15.0,
    ) -> AsyncIterator[bytes]:
        """订阅并返回帧流（4301 在调用时同步抛出，供端点转统一错误体而非断流中报错）。

        心跳：空闲超过 heartbeat_s 注释帧 ``: ping``（02 §5；heartbeat_s<=0 关闭心跳）。
        """
        subscription = self.subscribe(session_id, last_event_id=last_event_id)
        return stream_frames(subscription, heartbeat_s=heartbeat_s)


async def stream_frames(subscription: EventSubscription, *, heartbeat_s: float) -> AsyncIterator[bytes]:
    """订阅 → 帧流（两后端共用）：空闲超 heartbeat_s 产出 ``: ping`` 注释帧（<=0 关闭心跳）。"""
    try:
        while True:
            if heartbeat_s > 0:
                try:
                    event = await asyncio.wait_for(subscription.next(), timeout=heartbeat_s)
                except TimeoutError:
                    yield b": ping\n\n"
                    continue
            else:
                event = await subscription.next()
            if event is None:
                return
            yield encode_frame(event)
    finally:
        subscription.close()

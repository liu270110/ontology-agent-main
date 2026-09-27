"""SSE 事件流出口（L2 网关，02 §5 协议机制权威）：主干波 11 事件编码 + 会话缓冲/重放。

模块组成：
- events：帧编码（id/event/data + 空行）、心跳注释帧、主干波校验集；
- hub：进程内会话事件缓冲（seq 单调）、多订阅 fanout、Last-Event-ID 重放（缺口=4301）——
  零参构造保持可用，作为无 Redis 配置/不可达时的回落实现；
- redis_hub：Redis Stream 后端（02 §8 多副本出口条件 P1-1）——事件 XADD 落 Stream
  （MAXLEN+TTL 护栏）、订阅端 XREAD 直读（广播语义，不用消费组）、Last-Event-ID=会话内
  seq 映射、缺口 4301；``build_sse_hub`` 工厂按 Redis 可达性二选一。

跨模块口径：本包不 import agent.domain/data（契约③）；事件名单一事实源为
services.agent.business.chat_events（零依赖叶子，gateway→叶子 lint 合法）。
"""

from services.gateway.sse.events import (
    HEARTBEAT_FRAME,
    MAINSTREAM_EVENT_NAMES,
    SseEvent,
    encode_frame,
)
from services.gateway.sse.hub import EventSubscription, SseHub, SseSubscription, stream_frames
from services.gateway.sse.redis_hub import RedisSseHub, RedisSseSubscription, build_sse_hub

__all__ = [
    "HEARTBEAT_FRAME",
    "MAINSTREAM_EVENT_NAMES",
    "EventSubscription",
    "RedisSseHub",
    "RedisSseSubscription",
    "SseEvent",
    "SseHub",
    "SseSubscription",
    "build_sse_hub",
    "encode_frame",
    "stream_frames",
]

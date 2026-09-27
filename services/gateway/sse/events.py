"""SSE 帧编码（02 §5 帧格式权威）：主干波 11 事件 → text/event-stream 帧。

帧格式（02 §5，每帧以空行结尾；id=会话内单调递增 seq，跨任务由 M4 Redis Stream
回放窗口定位）：

    id: 1739
    event: TEXT_MESSAGE_CONTENT
    data: {"message_id":"m_01J9","delta":"本体的"}

- 事件名单一事实源 = services.agent.business.chat_events.ChatEventName（生产侧），
  本模块只做编码与主干波校验（M3 不外发扩展波事件，前端对未知 event 名忽略）；
- 心跳为注释帧 ``: ping\\n\\n``，防代理层空闲断连，不计入事件序列（02 §5；
  间隔 config.sse_heartbeat_seconds，建议值 15s，压测后冻结）；
- 响应头固定 Content-Type/Cache-Control/X-Accel-Buffering 由端点层（sessions.py）落。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from services.agent.business.chat_events import ChatEventName

# 主干波 11 事件校验集（与生产侧同源；tests/gateway 断言两侧一致防漂移）
MAINSTREAM_EVENT_NAMES: frozenset[str] = frozenset(name.value for name in ChatEventName)

HEARTBEAT_FRAME = b": ping\n\n"  # 02 §5 心跳注释帧（不计入事件序列）


@dataclass(frozen=True, slots=True)
class SseEvent:
    """单条 SSE 事件（frozen）：seq 会话内单调递增，Last-Event-ID 重放的定位键。"""

    seq: int
    name: str
    data: Mapping[str, Any]


def encode_frame(event: SseEvent) -> bytes:
    """事件 → SSE 帧（UTF-8；data 单行紧凑 JSON，中文明文不转义，与 02 §5 示例同构）。"""
    payload = json.dumps(dict(event.data), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.name}\ndata: {payload}\n\n".encode()

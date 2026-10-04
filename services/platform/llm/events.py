"""LLM 韧性事件通道（M4.5-C，docs/Agent/12 §3：llm.failover / llm.retry_* 落 task_events）。

- 机制：ContextVar 事件汇（进程内逐 Run 绑定）——模型端口层（本包 resilience.py）在
  降级/重试调度点调用 :func:`emit_llm_event`；编排器（agent.business，经组合根注入的
  emitter 工厂）在 Run 开始时 set、结束时 reset，事件沿「现有事件通道」落 task_events
  （先落库后推送，04 §2；持久化由 emitter 实现方承担，本模块只做绑定与分发）；
- 无绑定即丢弃（DEBUG 留痕）：kb 抽取/记忆沉淀等后台调用面无 task 上下文，事件通道
  不适用（审计面已由 llm_calls 冷账本承担，07 §2.3）——模型调用本身不受影响；
- 事件汇抛错只告警不传播：事件留痕失败不阻断模型调用（审计不阻塞主流程，02 §3 ⑥）；
- 分层纪律：本模块零上层依赖（platform 底座，importlinter 契约一），编排器侧注入边界
  同 services/agent/api/sessions.py build_kernel_ledger_sink_factory 先例。
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("services.platform.llm.events")

# 事件汇签名：(event_type, data) → 落库/推送；data 一律 JSON 可序列化标量/容器。
LlmEventEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]

_emitter: contextvars.ContextVar[LlmEventEmitter | None] = contextvars.ContextVar("llm_event_emitter", default=None)


def set_llm_event_emitter(emitter: LlmEventEmitter) -> contextvars.Token[LlmEventEmitter | None]:
    """绑定当前上下文的事件汇（编排器 Run 开始处调用；返回 token 供 reset 配对）。"""
    return _emitter.set(emitter)


def reset_llm_event_emitter(token: contextvars.Token[LlmEventEmitter | None]) -> None:
    """解除绑定（Run 结束/生成器关闭处调用，与 set 配对）。"""
    _emitter.reset(token)


async def emit_llm_event(event_type: str, data: dict[str, Any]) -> None:
    """分发一条 llm.* 事件：有绑定则 await 落库（先落库后推送由实现方保证序），无绑定丢弃。

    事件名口径 = task_events.event_type（{聚合名}.{snake_case}，standards/01 §2.2）：
    llm.failover / llm.retry_scheduled / llm.retry_succeeded（M4.5-C 登记三项）。
    """
    emitter = _emitter.get()
    if emitter is None:
        logger.debug("llm 事件无绑定汇（无 task 上下文），丢弃: type=%s", event_type)
        return
    try:
        await emitter(event_type, dict(data))
    except Exception as exc:  # noqa: BLE001 ——留痕失败不阻断模型调用（02 §3 ⑥）
        logger.warning("llm 事件汇执行失败（type=%s）: %s", event_type, exc)

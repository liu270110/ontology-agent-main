"""chat 能力绑定的 Run 级环境注入口（W2-4，2026-10-07 批；docs/api/对账 W2-4 + docs/Agent/06）。

问题：extra_tool_bindings 是编排器单例上的**静态**绑定对象（每轮注册进新分发器，轮间复用），
而工具 ``invoke(call, ctx)`` 只收 TenantContext——父 TaskRef（run_id 归因）与 ChatEvent
投影口（on_event）都是**每 Run 一变**的编排器内部值，静态绑定拿不到。

机制：ContextVar 逐 Run 绑定（``platform.llm.events`` 的 set_llm_event_emitter 同款先例，
编排器在 Run 开始处绑定、finally 解除；asyncio.create_task 复制上下文，内核工具调用与
编排器同任务链，invoke 侧可读）。本模块只搬运、不裁决——消费方：

- ``current_parent_task``：subagent spawn 的 task_resolver（父 Run 归因/父作用域查找键）；
- ``current_event_emitter``：ask_user 的 TOOL_CALL_* 投影口（问询下发三连，前端工具卡）。

未绑定（后台面/kb 抽取等非 chat Run）返回 None——消费方按各自 fail-closed 语义收口
（spawn 结构化拒绝、投影静默跳过），本模块不抛错。
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from typing import Any

from services.agent.business.chat_events import ChatEvent
from services.agent.domain.model.kernel_context import TaskRef

# 事件投影口签名（chat_orchestrator.on_event 同形；ask_user events.AskUserEventEmitter 同款）
ChatEventEmitter = Callable[[ChatEvent], None]

_current_task: contextvars.ContextVar[TaskRef | None] = contextvars.ContextVar("chat_run_parent_task", default=None)
_current_emitter: contextvars.ContextVar[ChatEventEmitter | None] = contextvars.ContextVar(
    "chat_run_event_emitter", default=None
)


def bind_run_scope(task: TaskRef, emit: ChatEventEmitter) -> contextvars.Token:
    """Run 开始处绑定（编排器 _execute_turn 建立父 TaskRef 后调用；返回 token 供 finally 解除）。"""
    t1 = _current_task.set(task)
    t2 = _current_emitter.set(emit)
    return (t1, t2)  # type: ignore[return-value]  # 复合 token（双 ContextVar 原子解除）


def reset_run_scope(token: Any) -> None:
    """Run 终态解除（finally 面；异常路径同收——ContextVar 泄漏会串 Run 归因）。"""
    t1, t2 = token
    _current_task.reset(t1)
    _current_emitter.reset(t2)


def current_parent_task() -> TaskRef | None:
    """当前 Run 的父 TaskRef（未绑定=None；subagent task_resolver 消费）。"""
    return _current_task.get()


def current_event_emitter() -> ChatEventEmitter | None:
    """当前 Run 的 ChatEvent 投影口（未绑定=None；ask_user emit 消费）。"""
    return _current_emitter.get()


def emit_via_run_scope(event: ChatEvent) -> None:
    """经 Run 级投影口发射（ask_user 绑定的静态 emit 注入值；未绑定=静默丢弃）。"""
    emit = _current_emitter.get()
    if emit is not None:
        emit(event)


def resolve_parent_task(ctx: Any) -> TaskRef:
    """subagent task_resolver 缺省实现：Run 级环境取父 TaskRef；未绑定=结构化拒绝。

    fail-closed 语义（SubagentGuardError 3001 形状）：无父归因的派生禁发起——父 run_id
    是分账/级联取消/审计的唯一键，编不出来（宁拒不冒）。ctx 仅落审计上下文（签名对齐
    TaskScopeResolver，本实现未用）。
    """
    del ctx
    from services.agent.business.capabilities.subagent.guards import SubagentGuardError
    from services.platform.errors import ErrorCode

    task = _current_task.get()
    if task is None:
        raise SubagentGuardError(
            ErrorCode.PARAM_INVALID,
            "当前无 Run 上下文（父 TaskRef 未绑定）：subagent.spawn 仅可在对话 Run 内调用",
        )
    return task

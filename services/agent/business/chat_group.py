"""群聊轮流发言集成（27 篇 X15）：路由解算 → 逐成员顺序发言。

Run 粒度裁决（2026-09-28，规避 uk_tasks_one_active_run）：**一轮用户消息 = 1 Task/1 Run**，
成员发言为 Run 内顺序执行（每成员一次编排器调用），不派生子 Task/Run；成员人格经
``ChatCommand.member_system_prompt`` 注入，发言归属经 ``agent_id`` 进 ChatOutcome/消息行。

本模块为纯编排叶：不 import ORM/UoW——持久化侧（assistant 计数、事件落库）经调用方
注入的回调完成（03 §6.1 流式全程不持事务）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from services.agent.business.chat_events import ChatCommand, ChatEvent, ChatEventName
from services.agent.business.chat_routing import (
    MemberRef,
    RoutingMode,
    resolve_all,
    resolve_mention,
    resolve_orchestrator,
    resolve_round_robin,
)
from services.agent.domain.model.session import GroupMember

_COUNT_FALLBACK = 0


async def stream_group_turn(
    orchestrator: Any,
    *,
    command: ChatCommand,
    members: tuple[GroupMember, ...],
    routing: str,
    count_assistant: Callable[[], Awaitable[int]],
    resolve_model: Any | None = None,
) -> AsyncIterator[ChatEvent]:
    """群聊一轮：路由解算 → ROUTING_DECISION 审计事件 → 逐成员顺序执行编排器。

    ``resolve_model``：orchestrator 模式的选人模型（ModelPort）；None 或 LLM 失败时
    按解算器纪律诚实回退（selected_by=deterministic）。
    """
    refs = tuple(
        MemberRef(
            agent_id=m.agent_id,
            display_name=m.display_name,
            system_prompt=m.system_prompt or "",
            model=m.model,
            routing_role=m.routing_role.value,
        )
        for m in members
    )
    mode = RoutingMode(routing)
    if mode is RoutingMode.MENTION:
        decision = resolve_mention(command.message, refs)
    elif mode is RoutingMode.ROUND_ROBIN:
        try:
            assistant_count = await count_assistant()
        except Exception:  # noqa: BLE001 ——计数失败按 0 起步（游标从头轮）
            assistant_count = _COUNT_FALLBACK
        decision = resolve_round_robin(command.message, refs, assistant_count=assistant_count)
    elif mode is RoutingMode.ALL:
        decision = resolve_all(command.message, refs)
    else:
        decision = await resolve_orchestrator(command.message, refs, resolve_model, command.trace_id)

    yield ChatEvent(
        name=ChatEventName.ROUTING_DECISION,
        data={
            "mode": mode.value,
            "selected": [str(m.agent_id) for m in decision.members],
            "names": [m.display_name for m in decision.members],
            "selected_by": decision.selected_by,
            "reason": decision.reason,
        },
        run_id=command.run_id,
    )
    for member in decision.members:
        member_command = command.model_copy(
            update={
                "agent_id": member.agent_id,
                "adapter": member.model if member.model in ("builtin", "claude") else "builtin",
                "member_system_prompt": member.system_prompt or None,
            }
        )
        async for event in orchestrator.stream_chat(member_command):
            yield event

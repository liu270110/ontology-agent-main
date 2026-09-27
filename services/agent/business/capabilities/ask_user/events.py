"""ask_user 的 ChatStream 事件投影（docs/Agent/06 路线 #6；承载取舍=复用既有事件族）。

**不新增事件名**（02 §5 主干波 11 事件一个不多一个不少；新事件名须走 02 §5 分波评审，
本批不做）：问询经既有 TOOL_CALL_* 事件族下发，载荷形状逐字段对齐 02 §5 事件表——

- TOOL_CALL_START ``{tool_call_id, tool_name}``：问询发起；
- TOOL_CALL_ARGS ``{tool_call_id, delta}``：delta=问询载荷 JSON 字符串
  （question_id/question/options/context）——**前端可见性=问题文本+选项** 的承载点，
  与 ChatAnswerTool 以 delta 携带 JSON 的先例同构（adapters/base.py）；
- TOOL_CALL_END ``{tool_call_id}``：载荷收集完毕、开始等待答复；
- TOOL_CALL_RESULT ``{tool_call_id, ok, summary, cost_ms}``：收口（答复/超时），
  summary 为答复预览或超时默认拒绝文案（B5 同纪律对用户可见的口径）。

question_id 与 tool_call_id 同键：前端凭 TOOL_CALL_ARGS.delta.question_id 关联答复
入口，审计凭同一 id 对账（问询-答复关联 id 贯穿）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from services.agent.business.chat_events import ChatEvent, ChatEventName

# 事件发射口签名（chat_orchestrator.on_event 同形；None=无投影，纯内核面测试用）
AskUserEventEmitter = Callable[[ChatEvent], None]

ASK_USER_TOOL_NAME = "ask_user"  # TOOL_CALL_START.tool_name（前端工具识别键）

_RESULT_SUMMARY_MAX_CHARS = 120  # TOOL_CALL_RESULT.summary 预览上限（ChatAnswerTool 同款）


def emit_question_opened(
    emit: AskUserEventEmitter | None,
    *,
    tool_call_id: str,
    run_id: Any,
    payload: dict[str, Any],
) -> None:
    """问询下发三连（START→ARGS→END）：ARGS.delta=问询载荷 JSON（问题文本+选项可见）。"""
    if emit is None:
        return
    emit(
        ChatEvent(
            name=ChatEventName.TOOL_CALL_START,
            data={"tool_call_id": tool_call_id, "tool_name": ASK_USER_TOOL_NAME},
            run_id=run_id,
        )
    )
    emit(
        ChatEvent(
            name=ChatEventName.TOOL_CALL_ARGS,
            data={"tool_call_id": tool_call_id, "delta": json.dumps(payload, ensure_ascii=False)},
            run_id=run_id,
        )
    )
    emit(ChatEvent(name=ChatEventName.TOOL_CALL_END, data={"tool_call_id": tool_call_id}, run_id=run_id))


def emit_question_settled(
    emit: AskUserEventEmitter | None,
    *,
    tool_call_id: str,
    run_id: Any,
    ok: bool,
    summary: str,
    cost_ms: int,
) -> None:
    """问询收口（TOOL_CALL_RESULT）：答复/超时统一形状，summary 截 120 字。"""
    if emit is None:
        return
    emit(
        ChatEvent(
            name=ChatEventName.TOOL_CALL_RESULT,
            data={
                "tool_call_id": tool_call_id,
                "ok": ok,
                "summary": summary[:_RESULT_SUMMARY_MAX_CHARS],
                "cost_ms": cost_ms,
            },
            run_id=run_id,
        )
    )

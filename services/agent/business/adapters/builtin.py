"""builtin 适配器：OpenAI 兼容直驱的最小生成通道（ModelPort 消费，M3 联调基线）。

- 走 L7 ModelPort（services/platform/ports/model_port.py）：审计/预算在组合根由
  AuditedModelPort 内建包裹（硬约束：勿绕过）；本适配器只组提示词与流式投影；
- 流式形态：ModelPort.complete_structured 为一次性结构化产物（答案 JSON），delta 按
  固定步长切片透传（与 claude 真 SSE 流式同构消费，SSE 投影无差别）；
- 用量：平台用量上下文（llm/usage）回填后读取，FakeModelPort 不回填 → 记 0（显式口径）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from services.agent.business.adapters.base import CHAT_ACTION_IRI, ChatAdapter, ChatTurn, GenerationEvent
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.platform.llm.usage import get_last_usage
from services.platform.ports.model_port import ModelPort

# 答案抽取 Schema（推理分级宪法第 2 条：LLM 输出必过确定性校验才可用）
_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}
_DELTA_CHARS = 24  # 透传切片步长（示例值；SSE 投影粒度，非生成粒度）


def _build_system_prompt(turn: ChatTurn) -> str:
    """系统提示：角色约定 + 已标界上下文（B3 标界头在组装器落，此处原样携带）。"""
    return (
        "你是 ontology-agent 平台对话助手：仅依据给定的记忆与知识证据回答，"
        "证据不足时明确说明；引用事实时保持与证据原文一致。\n"
        f"{turn.context_text}"
    )


def _build_user_prompt(turn: ChatTurn) -> str:
    """用户提示：近窗历史（新→旧反转成时序）+ 本条消息。"""
    lines = ["## 对话历史"]
    for role, content in reversed(turn.history):
        lines.append(f"{role}: {content}")
    lines.append("## 本条消息")
    lines.append(turn.message)
    return "\n".join(lines)


def _chunk_text(text: str, size: int = _DELTA_CHARS) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


class BuiltinAdapter(ChatAdapter):
    """builtin：内置 harness（ModelPort 直驱）；无模型配置时调用报 5002（端口契约）。"""

    meta = ExtensionMeta(
        name="chat.builtin",
        version="1.0.0",
        semantic_annotation={"action_iri": CHAT_ACTION_IRI},
    )
    adapter_name = "builtin"

    def __init__(self, model: ModelPort) -> None:
        self._model = model

    async def stream_chat(
        self, turn: ChatTurn, ctx: TenantContext, *, timeout_ms: int = 30_000
    ) -> AsyncIterator[GenerationEvent]:
        data: dict[str, Any] = await asyncio.wait_for(
            self._model.complete_structured(
                system=_build_system_prompt(turn),
                user=_build_user_prompt(turn),
                json_schema=_ANSWER_SCHEMA,
                timeout_s=max(timeout_ms / 1000, 1.0),
                trace_id=ctx.trace_id,
                num_ctx=turn.num_ctx,
            ),
            timeout=max(timeout_ms / 1000, 1.0),
        )
        answer = data.get("answer")
        if not isinstance(answer, str):  # Schema 已保证；防御性收窄（值不经采样）
            answer = ""
        for piece in _chunk_text(answer):
            yield GenerationEvent(kind="text_delta", delta=piece)
        recorded = get_last_usage()
        usage = (
            {
                "token_in": recorded.token_in,
                "token_out": recorded.token_out,
                "cache_read_tokens": recorded.cache_read_tokens,
            }
            if recorded is not None
            else {}
        )
        yield GenerationEvent(kind="finish", usage=usage, finish_reason="stop")

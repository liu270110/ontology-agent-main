"""builtin 适配器：OpenAI 兼容直驱的最小生成通道（ModelPort 消费，M3 联调基线）。

- 走 L7 ModelPort（services/platform/ports/model_port.py）：审计/预算在组合根由
  AuditedModelPort 内建包裹（硬约束：勿绕过）；本适配器只组提示词与流式投影；
- 流式形态（H-6 真流式批，2026-09-29）：端口具备 ``stream_complete`` 即走**真流式**
  （HTTP SSE 逐段产出 text_delta，模型产出顺序透传，无攒齐再切）；端口仅有
  complete_structured（Protocol 扩展前的旧实现/测试桩，及生产 AuditedModelPort 收口
  流式面之前的过渡期）→ 回退「一次性结构化答案 + 固定步长切片」伪流式（规划回退路径
  保留）。两路事件语义同构：TEXT_MESSAGE_CONTENT 的 delta 形态不变，SSE 投影无差别，
  前端零感知；reasoning 透传批（2026-10-07）：端口具备 ``stream_complete_events``
  结构化面 → 优先消费（reasoning_content → reasoning_delta 与 text_delta 并存投影），
  缺席回退纯文本面（reasoning 丢弃）；
- 用量：平台用量上下文（llm/usage）回填后读取（真流式=末块 usage），FakeModelPort
  不回填 → 记 0（显式口径）；
- **H-1 钉死引用接入（2026-09-29，一处最小接入）**：``turn.system_prompt`` 若为
  ``prompt:{id}@{version|head}`` 钉死引用 → 经注入的 ``prompt_resolver``（PromptResolver
  端口，组合根 build_db_prompt_resolver 装配）消解为确定版本内容（回执 id@version+checksum
  落 prompt.ref_resolved 事件）；未装配 resolver 时 fail-closed（5002，宁拒不错载）。
  非 ``prompt:`` 前缀的行为不变（平台缺省角色约定——成员 persona 字面量消费是独立缺口，
  随 chat 编排批另行登记）。
- **H-2 遮蔽式工具 schema（2026-09-29）**：``render_tool_schema_section`` /
  ``mask_tools`` / ``build_tools_segment``——全量定义常驻 + 当轮启用清单遮蔽
  （KV-cache 前缀稳定，07 §3 规律 2）；消费接线随逐轮 tool-calling 批。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

from services.agent.business.adapters.base import CHAT_ACTION_IRI, ChatAdapter, ChatTurn, GenerationEvent
from services.agent.business.prompts.resolver import PromptResolver, is_prompt_ref, persona_text
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.platform.llm.usage import get_last_usage
from services.platform.ports.model_port import ModelPort, ModelUnavailableError

# 答案抽取 Schema（推理分级宪法第 2 条：LLM 输出必过确定性校验才可用）——伪流式回退路径消费
_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}
_DELTA_CHARS = 24  # 回退路径透传切片步长（示例值；SSE 投影粒度，非生成粒度）

# ── H-2 遮蔽式工具 schema（2026-09-29 批，研究 07 §3 规律 2 Manus 教训）────────────────
# KV-cache 前缀不可被打穿：工具 schema 定义**逐轮增删会让前缀字节漂移、缓存全灭**——
# 定义本体全量常驻 system 前缀（稳定性=tier 1），当轮启用子集只经遮蔽清单表达。
# builtin 现无逐轮工具集变化（chat 单步形态），机制以下列纯函数落成可复用件，
# 消费接线随逐轮 tool-calling（ReAct）批（02 §11.3 开放问题）。

_TOOL_SCHEMA_SECTION_HEADER = "【工具 Schema·全量定义（常驻；当轮启用以清单为准）】"
_TOOL_MASK_HEADER = "本轮可用工具："


def render_tool_schema_section(definitions: Mapping[str, Any]) -> str:
    """全量工具 schema 常驻段（tier=1 稳定知识）：按名确定性排序 + 固定序列化。

    纯函数：同一 ``definitions`` 任意轮次产出**字节一致**（sorted + sort_keys JSON，
    禁时间戳/禁 dict 迭代序）——这是 KV-cache 前缀稳定的前置契约。
    """
    lines = [_TOOL_SCHEMA_SECTION_HEADER]
    for name in sorted(definitions):
        lines.append(f"- {name}: {json.dumps(definitions[name], ensure_ascii=False, sort_keys=True)}")
    return "\n".join(lines)


def mask_tools(available: set[str]) -> str:
    """当轮启用遮蔽清单（纯函数）：定义本体不动，只换这一行（易变尾在常驻段之后）。

    确定性：``sorted`` 消化 set 迭代序（同集合同输出）；空集显式声明（遮蔽全量=全不可用）。
    """
    names = ", ".join(sorted(available)) if available else "（无）"
    return f"{_TOOL_MASK_HEADER}{names}"


def build_tools_segment(definitions: Mapping[str, Any], available: set[str]) -> str:
    """常驻段拼装：全量定义（稳定前缀）在前 + 遮蔽清单（易变尾）在后。

    轮间差异被压到段尾一行——前缀字节稳定（07 §6.3 区块 3「候选集用遮蔽限定，
    不增删 schema 定义」的落地形态）。
    """
    return f"{render_tool_schema_section(definitions)}\n{mask_tools(available)}"


def _build_system_prompt(turn: ChatTurn) -> str:
    """系统提示：角色约定 + 技能目录段（竖线② L1，进程级稳定前缀）+ 已标界上下文（B3 标界
    头在组装器落，此处原样携带；目录段置于 context_text 之前保 KV-cache 前缀稳定）。

    K28-c（docs/Agent/13 §34）：turn.tools_segment 非空=会话具名工具集的 schema 遮蔽段
    （H-2 build_tools_segment 产出，编排器装配）——插在目录段之后、context_text 之前
    （段内定义本体进程级稳定+遮蔽行会话级恒定，先于逐轮易变上下文保前缀稳定）；
    空串=不遮蔽（未设工具集会话，现行行为零变化）。H-2 机制首个消费接线。"""
    parts = [
        "你是 ontology-agent 平台对话助手：仅依据给定的记忆与知识证据回答，"
        "证据不足时明确说明；引用事实时保持与证据原文一致。"
    ]
    if turn.skills_catalog:
        parts.append(turn.skills_catalog)
    if turn.tools_segment:
        parts.append(turn.tools_segment)
    parts.append(turn.context_text)
    return "\n".join(parts)


def _build_user_prompt(turn: ChatTurn) -> str:
    """用户提示：近窗历史（新→旧反转成时序）+ 本条消息。"""
    lines = ["## 对话历史"]
    for role, content in reversed(turn.history):
        lines.append(f"{role}: {content}")
    lines.append("## 本条消息")
    lines.append(turn.message)
    return "\n".join(lines)


def _build_messages(turn: ChatTurn, persona: str | None = None) -> list[dict[str, Any]]:
    """chat completions messages 形态（H-6）：system（角色约定+已标界上下文）+ user。

    persona 非 None=钉死引用已消解的人格文本（H-1，覆盖缺省角色约定）。
    """
    return [
        {"role": "system", "content": persona if persona is not None else _build_system_prompt(turn)},
        {"role": "user", "content": _build_user_prompt(turn)},
    ]


def _chunk_text(text: str, size: int = _DELTA_CHARS) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def _finish_event() -> GenerationEvent:
    """finish 终态：用量取平台上下文（真流式=末块 usage 回填；桩不回填记 0，显式口径）。"""
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
    return GenerationEvent(kind="finish", usage=usage, finish_reason="stop")


class BuiltinAdapter(ChatAdapter):
    """builtin：内置 harness（ModelPort 直驱）；无模型配置时调用报 5002（端口契约）。"""

    meta = ExtensionMeta(
        name="chat.builtin",
        version="1.0.0",
        semantic_annotation={"action_iri": CHAT_ACTION_IRI},
    )
    adapter_name = "builtin"

    def __init__(self, model: ModelPort, *, prompt_resolver: PromptResolver | None = None) -> None:
        self._model = model
        self._prompt_resolver = prompt_resolver

    async def _persona_prompt(self, turn: ChatTurn) -> str:
        """人格面板：``prompt:`` 钉死引用 → 运行时消解（H-1）；其余=平台缺省角色约定。"""
        if is_prompt_ref(turn.system_prompt):
            if self._prompt_resolver is None:
                # fail-closed：引用语法在但解析器未装配（组合根接线遗留），宁拒不错载
                raise ModelUnavailableError(f"prompt: 钉死引用未接线解析器（5002）: {turn.system_prompt}")
            resolved = await self._prompt_resolver(turn.system_prompt)
            return persona_text(resolved)
        return _build_system_prompt(turn)

    async def stream_chat(
        self, turn: ChatTurn, ctx: TenantContext, *, timeout_ms: int = 30_000
    ) -> AsyncIterator[GenerationEvent]:
        """生成入口：端口有结构化流式面走真流式（含 reasoning 两路），仅纯文本流式面走
        文本真流式，否则回退结构化切片（三路事件语义对消费侧同构）。"""
        persona = await self._persona_prompt(turn)
        stream_complete = getattr(self._model, "stream_complete", None)
        if stream_complete is None:
            # 回退（勿解包 AuditedModelPort._inner 走真流式——审计/预算硬约束不可绕过）
            async for event in self._stream_structured_fallback(turn, ctx, timeout_ms=timeout_ms, persona=persona):
                yield event
            return
        # 硬上限由内核单工具超时（asyncio.wait_for）钳制；传输层超时=timeout_s（端口内
        # httpx 必设）——与 claude 直连通道同口径（docs/Agent §5：流式按传输层判超时）。
        timeout_s = max(timeout_ms / 1000, 1.0)
        # 结构化流式面（reasoning 透传批，2026-10-07）：端口具备 stream_complete_events
        # （鸭子类型探测，与 stream_complete 同款可选面纪律）→ content/reasoning 两路并存
        # 投影（单 piece 双路时 reasoning 先于 content）；缺席 → 纯文本面（reasoning 丢弃）。
        stream_events = getattr(self._model, "stream_complete_events", None)
        if stream_events is not None:
            async for piece in stream_events(
                _build_messages(turn, persona),
                temperature=None,  # 对话档：服务端缺省（complete_structured 的事实型 0.1 属抽取面，不沿用到闲聊面）
                num_ctx=turn.num_ctx,
                timeout_s=timeout_s,
                trace_id=ctx.trace_id,
            ):
                if piece.reasoning:
                    yield GenerationEvent(kind="reasoning_delta", delta=piece.reasoning)
                if piece.content:
                    yield GenerationEvent(kind="text_delta", delta=piece.content)
            yield _finish_event()
            return
        async for piece in stream_complete(
            _build_messages(turn, persona),
            temperature=None,  # 对话档：服务端缺省（complete_structured 的事实型 0.1 属抽取面，不沿用到闲聊面）
            num_ctx=turn.num_ctx,
            timeout_s=timeout_s,
            trace_id=ctx.trace_id,
        ):
            if piece:
                yield GenerationEvent(kind="text_delta", delta=piece)
        yield _finish_event()

    async def _stream_structured_fallback(
        self, turn: ChatTurn, ctx: TenantContext, *, timeout_ms: int, persona: str
    ) -> AsyncIterator[GenerationEvent]:
        """伪流式回退（H-6 前形态原样保留）：complete_structured 一次性答案 JSON → 固定步长切片。"""
        data: dict[str, Any] = await asyncio.wait_for(
            self._model.complete_structured(
                system=persona,
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
        yield _finish_event()

"""chat 适配器契约面（计划 3.2；Agent 服务设计 §3.1/§3.2 + Agent/02 §4.2 AgentSlot）。

三层职责（与文档锚点一一对应）：

1. **AgentSlot 注册面**（02 §4.2，`agent.slots`）：ChatAdapter 经
   ``ExtensionDispatcher.register_agent_slot`` 进内核分发器同一注册表（meta 语义标注 +
   spawn_sub 契约签名，版本握手走注册面缺省值）；v1 仅冻结注册面——chat 路径不经
   spawn_sub，返回占位句柄仅满足契约签名（消费随 M4 子代理批次接通）。
2. **每轮扩展装配**（02 §4 ②①④ 通道，策略下放、形态归内核）：一次对话 = 模板规划
   （planning.strategies，零 token 模板档）+ 组装上下文供给（context.providers）+
   单步生成工具（tools.bindings，B1 基线在计划步上照常生效）——三件经
   ``bind_turn`` 注册进**每轮新建**的分发器（轮间零共享状态）。
3. **流式生成**（Agent 服务设计 §3.1 collect_events 的流式形态）：子类实现
   ``stream_chat``，产出 :class:`GenerationEvent`；TEXT_*/TOOL_CALL_* 的 SSE 投影由
   :class:`ChatAnswerTool` 统一翻译（适配器只管生成，不管 wire 语义）。

纪律：适配器不得自称 externally_verified（B3，内核标界覆写）；LLM 调用经 ModelPort
（builtin）或 Anthropic 直连通道（claude，锚点 §3.3 保留通道），失败一律结构化返回
（02 §4.1 ④ 契约：禁裸异常逃逸循环）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from services.agent.business.chat_events import ChatEvent, ChatEventName
from services.agent.domain.model.kernel_actions import ApprovalTicket, ExecutionMode, ToolCall, ToolResult
from services.agent.domain.model.kernel_context import (
    ContextBlock,
    ExtensionMeta,
    StepRef,
    TaskRef,
    TenantContext,
)
from services.agent.domain.model.kernel_planning import PlanCandidate, PlanMode, PlanStep
from services.platform.errors import ErrorCode

# chat 行动类与授权面：计划步 required_scopes ⊆ 租户 scopes（B1 R3，授权唯一依据）；
# 主体 scope 由端点从 JWT 注入 ChatCommand.scopes，适配器不自查（MCP annotations 不作授权依据）。
CHAT_ACTION_IRI = "http://ontology.example/action/chat_answer"
CHAT_SCOPE = "session:chat"
CHAT_PLANNING_RULE_IRI = "http://ontology.example/rule/chat_template_planning"

EventEmitter = Callable[[ChatEvent], None]


class ChatTurn(BaseModel):
    """单轮生成请求（值对象 frozen）：编排器组装上下文后投给适配器。"""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    session_id: UUID
    run_id: UUID
    message: str  # 本条用户消息
    history: tuple[tuple[str, str], ...] = ()  # 近窗 (role, content)，新→旧
    context_text: str = ""  # 已标界的记忆+证据上下文（B3 标界在组装器落）
    num_ctx: int | None = None  # 上下文窗口注入（ModelPort 可选参，端点不支持时忽略）

    @property
    def message_id(self) -> str:
        """TEXT_MESSAGE_* 的 message_id（run 维度稳定，重放一致）。"""
        return f"m_{self.run_id}"


class TurnBox:
    """单轮可变收货箱：适配器/工具回填生成产物，编排器在内核终态后读取。

    内核 ToolPort 契约只回 ToolResult（不可信标界后无正文保证），回答全文经独立
    通道回传编排器做 L1 回写与结果审计（值来自工具自身产出，非模型自述状态）。
    """

    __slots__ = ("answer", "cost_ms", "error_code", "error_message", "finish_reason", "tool_call_id", "usage")

    def __init__(self) -> None:
        self.answer = ""
        self.usage: dict[str, Any] = {}
        self.finish_reason: str | None = None
        self.error_code: int | None = None
        self.error_message: str | None = None
        self.tool_call_id: str | None = None
        self.cost_ms = 0

    @property
    def ok(self) -> bool:
        return self.error_code is None


class GenerationEvent(BaseModel):
    """适配器生成事件（frozen）：text_delta 流式增量 / finish 终态（含用量）。"""

    model_config = ConfigDict(frozen=True)

    kind: str  # text_delta | finish
    delta: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    finish_reason: str | None = None


class ChatTemplatePlanner:
    """chat 模板规划策略（planning.strategies，03 §1.1 规划三档之模板优先，零生成成本）。

    单步计划：chat 行动类（READ，无审批面）+ 空/必填参数域 + 主体 scope 断言；
    过内核三层校验（结构/绑定/分级）后生效。
    """

    meta = ExtensionMeta(
        name="chat.template_planner",
        version="1.0.0",
        semantic_annotation={"rule_iri": CHAT_PLANNING_RULE_IRI},
    )

    async def plan(
        self,
        task: TaskRef,
        ctx: TenantContext,
        *,
        mode: PlanMode = PlanMode.TEMPLATE,
        timeout_ms: int = 10_000,
    ) -> PlanCandidate:
        step = PlanStep(
            seq=1,
            action_iri=CHAT_ACTION_IRI,
            execution_mode=ExecutionMode.READ,
            parameters={},
            parameter_schema={},
            required_scopes=(CHAT_SCOPE,),
            description="chat 单步生成（模板档：检索证据+记忆上下文 → 模型流式回答）",
        )
        return PlanCandidate(strategy_name=self.meta.name, mode=PlanMode.TEMPLATE, steps=(step,))


class ChatContextProvider:
    """组装上下文供给器（context.providers）：投递已标界的记忆+证据文本（B3 标界在组装器）。

    内核契约覆写：产出信任级一律被内核强制 agent_attested（grounding.py），供给器
    自称 externally_verified 无效；budget_tokens 为绝对上限（内核裁剪）。
    """

    meta = ExtensionMeta(
        name="chat.context_provider",
        version="1.0.0",
        semantic_annotation={"concept_iri": "http://ontology.example/concept/对话上下文"},
    )

    def __init__(self, turn: ChatTurn) -> None:
        self._turn = turn

    async def provide(
        self,
        task: TaskRef,
        step: StepRef | None,
        ctx: TenantContext,
        *,
        budget_tokens: int,
        timeout_ms: int = 3_000,
    ) -> ContextBlock:
        content = self._turn.context_text or "（无附加记忆/证据上下文）"
        return ContextBlock(source=self.meta.name, content=content, tokens=max(1, len(content) // 3))


class ChatAnswerTool:
    """chat 生成工具（tools.bindings，行动类=CHAT_ACTION_IRI）：流式翻译 + 结构化失败。

    SSE 投影（02 §5 载荷形状）：TOOL_CALL_START/ARGS/END → TEXT_MESSAGE_* →
    TOOL_CALL_RESULT（{tool_call_id, ok, summary, cost_ms}）；ModelPortError 族
    （5xxx 已登记码）结构化返回，不裸异常（02 §4.1 ④ 契约）。
    """

    meta = ExtensionMeta(
        name="chat.answer_tool",
        version="1.0.0",
        semantic_annotation={"action_iri": CHAT_ACTION_IRI},
    )

    def __init__(self, adapter: ChatAdapter, turn: ChatTurn, box: TurnBox, on_event: EventEmitter) -> None:
        self._adapter = adapter
        self._turn = turn
        self._box = box
        self._emit = on_event

    async def invoke(
        self,
        call: ToolCall,
        ctx: TenantContext,
        *,
        approval: ApprovalTicket | None = None,
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        tool_call_id = str(call.call_id)
        message_id = self._turn.message_id
        started = time.monotonic()
        self._box.tool_call_id = tool_call_id
        self._emit(
            ChatEvent(
                name=ChatEventName.TOOL_CALL_START,
                data={"tool_call_id": tool_call_id, "tool_name": CHAT_ACTION_IRI.rsplit("/", 1)[-1]},
                run_id=self._turn.run_id,
            )
        )
        self._emit(
            ChatEvent(
                name=ChatEventName.TOOL_CALL_ARGS,
                data={
                    "tool_call_id": tool_call_id,
                    "delta": json.dumps({"message": self._turn.message}, ensure_ascii=False),
                },
                run_id=self._turn.run_id,
            )
        )
        self._emit(
            ChatEvent(name=ChatEventName.TOOL_CALL_END, data={"tool_call_id": tool_call_id}, run_id=self._turn.run_id)
        )
        self._emit(
            ChatEvent(name=ChatEventName.TEXT_MESSAGE_START, data={"message_id": message_id}, run_id=self._turn.run_id)
        )

        parts: list[str] = []
        usage: dict[str, Any] = {}
        finish_reason = "stop"
        try:
            async for generated in self._adapter.stream_chat(self._turn, ctx, timeout_ms=timeout_ms):
                if generated.kind == "text_delta":
                    parts.append(generated.delta)
                    self._emit(
                        ChatEvent(
                            name=ChatEventName.TEXT_MESSAGE_CONTENT,
                            data={"message_id": message_id, "delta": generated.delta},
                            run_id=self._turn.run_id,
                        )
                    )
                elif generated.kind == "finish":
                    usage = dict(generated.usage)
                    finish_reason = generated.finish_reason or "stop"
        except asyncio.CancelledError:
            raise  # 取消传播（内核清单以取消错误闭合未闭合调用，02 §2.4）
        except BaseException as exc:  # 生成失败一律结构化（02 §4.1 ④：禁裸异常逃逸循环）
            # 码源优先级：超时=5001（含 wait_for 硬钳）→ 端口/网关异常自带 code（5xxx 已登记）
            # → 其余裸异常兜底 5999（转义留痕）
            if isinstance(exc, TimeoutError):
                code = int(ErrorCode.LLM_TIMEOUT)
            else:
                code = int(getattr(exc, "code", int(ErrorCode.INTERNAL_ERROR)))
            message = str(exc)
            self._box.error_code = code
            self._box.error_message = message
            cost_ms = int((time.monotonic() - started) * 1000)
            self._box.cost_ms = cost_ms
            self._emit(
                ChatEvent(
                    name=ChatEventName.TEXT_MESSAGE_END,
                    data={"message_id": message_id, "finish_reason": "error"},
                    run_id=self._turn.run_id,
                )
            )
            self._emit(
                ChatEvent(
                    name=ChatEventName.TOOL_CALL_RESULT,
                    data={"tool_call_id": tool_call_id, "ok": False, "summary": message[:200], "cost_ms": cost_ms},
                    run_id=self._turn.run_id,
                )
            )
            return ToolResult(ok=False, error_code=code, error_message=message, usage=usage)

        self._box.answer = "".join(parts)
        self._box.usage = usage
        self._box.finish_reason = finish_reason
        cost_ms = int((time.monotonic() - started) * 1000)
        self._box.cost_ms = cost_ms
        self._emit(
            ChatEvent(
                name=ChatEventName.TEXT_MESSAGE_END,
                data={"message_id": message_id, "finish_reason": finish_reason},
                run_id=self._turn.run_id,
            )
        )
        summary = self._box.answer[:120] or "（空回答）"
        self._emit(
            ChatEvent(
                name=ChatEventName.TOOL_CALL_RESULT,
                data={"tool_call_id": tool_call_id, "ok": True, "summary": summary, "cost_ms": cost_ms},
                run_id=self._turn.run_id,
            )
        )
        total_tokens = sum(v for v in usage.values() if isinstance(v, int))
        return ToolResult(ok=True, output={"answer": self._box.answer}, usage={"total_tokens": total_tokens})


class ChatAdapter:
    """chat 适配器基类：子类实现 stream_chat（生成通道），扩展装配面共用。"""

    meta: ExtensionMeta
    adapter_name: str = ""

    async def stream_chat(
        self, turn: ChatTurn, ctx: TenantContext, *, timeout_ms: int = 30_000
    ) -> AsyncIterator[GenerationEvent]:
        """执行一次生成：产出 text_delta 流 + finish 终态；失败抛 ModelPortError 族。"""
        raise NotImplementedError("适配器必须实现 stream_chat")  # pragma: no cover
        yield GenerationEvent(kind="finish")  # pragma: no cover（async generator 形态要求）

    # ── AgentSlot 契约面（02 §4.2；v1 仅冻结注册面）──────────────────────
    async def spawn_sub(
        self,
        task: TaskRef,
        ctx: TenantContext,
        *,
        context_budget: int,
        artifact_schema: dict[str, Any],
        timeout_ms: int = 600_000,
    ) -> str:
        """v1 占位：chat 适配器不派生子代理，返回占位句柄仅满足 AgentSlot 契约签名。"""
        return f"{self.adapter_name}-sub-{uuid.uuid4()}"

    # ── 每轮扩展装配（策略下放、形态归内核；轮间零共享状态）────────────────
    def turn_planner(self, turn: ChatTurn) -> ChatTemplatePlanner:
        return ChatTemplatePlanner()

    def turn_context_provider(self, turn: ChatTurn) -> ChatContextProvider:
        return ChatContextProvider(turn)

    def turn_tool(self, turn: ChatTurn, box: TurnBox, on_event: EventEmitter) -> ChatAnswerTool:
        return ChatAnswerTool(self, turn, box, on_event)

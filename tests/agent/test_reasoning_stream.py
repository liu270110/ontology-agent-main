# tests/agent/test_reasoning_stream.py
"""reasoning 透传批（2026-10-07）· vLLM/DeepSeek reasoning_content → THINKING_* SSE 事件。

断言目标：
- 端口结构化流式面（stream_complete_events）→ builtin 投影 reasoning_delta/text_delta
  两路并存（reasoning 先于 content，互不干扰）；纯文本端口回退口径零变化；
- 网关 SSE 解析：``delta.reasoning_content`` 两路透传；stream_complete 纯文本面 reasoning
  丢弃（既有契约不变）；audited 装饰同口径（透传/装箱退化，审计行不缺）；
- 工具层 THINKING_* 投影：首条 reasoning_delta → START（reasoning_effort? 透传），逐条
  CONTENT，切换 text/终态（finish/error）前 END——每消息至多一对（幂等守卫）；
- 落库口径（EventSink 双写豁免，SUBRUN_UPDATED 先例）：THINKING_CONTENT 纯实时不落
  task_events；THINKING_START/END 落账本（SSE 双写钩子 + task_worker 落库路径同口径）。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import contextmanager
from typing import Any

import httpx

from services.agent.api.sessions import build_exec_event_dual_write
from services.agent.business.adapters import BuiltinAdapter, ChatAnswerTool, ChatTurn, GenerationEvent, TurnBox
from services.agent.business.chat_events import ChatCommand, ChatEvent, ChatEventName, wire_data
from services.agent.business.exec_events import (
    EXEC_PERSISTED_EVENTS,
    EXEC_REALTIME_ONLY_EVENTS,
    THINKING_PERSISTED_EVENTS,
    THINKING_REALTIME_ONLY_EVENTS,
)
from services.agent.business.task_worker import TaskRunWorker
from services.agent.domain.model.kernel_actions import ToolCall
from services.agent.domain.model.kernel_context import TenantContext
from services.gateway.sse.events import MAINSTREAM_EVENT_NAMES
from services.platform.errors import tenant_id_ctx
from services.platform.llm.audit import LlmCallAuditBuffer
from services.platform.llm.audited import AuditedModelPort
from services.platform.llm.gateway import OpenAICompatibleModelPort
from services.platform.llm.resilience import FailoverModelPort
from services.platform.llm.usage import get_last_usage, set_last_usage
from services.platform.ports.model_port import ModelStreamPiece

TENANT = uuid.uuid4()
_TRACE = "trace-reasoning-stream"


def make_ctx() -> TenantContext:
    return TenantContext(tenant_id=TENANT, scopes=("session:chat",), trace_id=_TRACE)


def make_turn(*, reasoning_effort: str | None = None) -> ChatTurn:
    return ChatTurn(
        tenant_id=TENANT,
        session_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        message="线路A为何停电？",
        context_text="【证据】线路A于14:02跳闸。",
        reasoning_effort=reasoning_effort,
    )


# ── 端口桩 ───────────────────────────────────────────────────────────────


class PiecesFakeModel:
    """结构化流式端口桩：stream_complete_events 逐段产出 ModelStreamPiece。"""

    provider = "pieces_fake"

    def __init__(self, pieces: list[ModelStreamPiece]) -> None:
        self.pieces = pieces
        self.structured_calls = 0

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.structured_calls += 1
        return {"answer": "不应走回退路径"}

    async def stream_complete(self, messages: list[dict[str, Any]], **kw: Any) -> AsyncIterator[str]:
        raise AssertionError("结构化面在场时不得退化消费纯文本面")

    async def stream_complete_events(
        self, messages: list[dict[str, Any]], **kw: Any
    ) -> AsyncIterator[ModelStreamPiece]:
        for piece in self.pieces:
            yield piece


class TextOnlyFakeModel:
    """纯文本流式端口桩（无 stream_complete_events）：回退面口径不变。"""

    provider = "text_only_fake"

    def __init__(self, pieces: list[str]) -> None:
        self.pieces = pieces

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("真流式路径不触达结构化面")

    async def stream_complete(self, messages: list[dict[str, Any]], **kw: Any) -> AsyncIterator[str]:
        for piece in self.pieces:
            yield piece


async def _collect(adapter: BuiltinAdapter, turn: ChatTurn, ctx: TenantContext) -> list[GenerationEvent]:
    return [event async for event in adapter.stream_chat(turn, ctx, timeout_ms=30_000)]


# ── builtin：reasoning_delta / text_delta 两路并存投影 ────────────────────


async def test_builtin_结构化流式面_reasoning与text两路并存投影() -> None:
    """reasoning_delta 与 text_delta 并存互不干扰；单 piece 双路时 reasoning 先于 content。"""
    model = PiecesFakeModel(
        [
            ModelStreamPiece(reasoning="先想"),
            ModelStreamPiece(reasoning="再想"),
            ModelStreamPiece(content="答案"),
            ModelStreamPiece(content="。", reasoning="补一句推理"),  # 单 chunk 双路
        ]
    )
    events = await _collect(BuiltinAdapter(model), make_turn(), make_ctx())

    kinds = [(e.kind, e.delta) for e in events]
    assert kinds == [
        ("reasoning_delta", "先想"),
        ("reasoning_delta", "再想"),
        ("text_delta", "答案"),
        ("reasoning_delta", "补一句推理"),  # 双路 piece：reasoning 先出
        ("text_delta", "。"),
        ("finish", ""),
    ]
    assert model.structured_calls == 0  # 真流式路径不触达结构化面
    assert "".join(e.delta for e in events if e.kind == "text_delta") == "答案。"


async def test_builtin_纯文本端口_无reasoning_delta_回退口径零变化() -> None:
    """端口缺 stream_complete_events（鸭子类型探测）→ 纯文本面：只产 text_delta。"""
    events = await _collect(BuiltinAdapter(TextOnlyFakeModel(["雷击", "跳闸。"])), make_turn(), make_ctx())

    assert [e.kind for e in events] == ["text_delta", "text_delta", "finish"]
    assert all(e.delta for e in events if e.kind == "text_delta")


# ── 网关：SSE delta.reasoning_content 解析 ────────────────────────────────

_REASONING_SSE = "".join(
    line + "\n\n"
    for line in [
        'data: {"choices":[{"delta":{"role":"assistant"}}]}',  # role 首块：无增量
        'data: {"choices":[{"delta":{"reasoning_content":"雷击会"}}]}',
        'data: {"choices":[{"delta":{"reasoning_content":"导致跳闸"}}]}',
        'data: {"choices":[{"delta":{"content":"是的，"}}]}',
        'data: {"choices":[{"delta":{"content":"雷击导致。","reasoning_content":"收尾"}}]}',  # 双路
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":6}}',
        "data: [DONE]",
    ]
).encode("utf-8")


def _gateway_port(sse: bytes, captured: list[dict]) -> OpenAICompatibleModelPort:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, content=sse)

    return OpenAICompatibleModelPort(
        base_url="http://llm", api_key="k", model="m", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def test_网关SSE解析_reasoning_content两路透传() -> None:
    """stream_complete_events：reasoning/content 两路并存产出；usage 末块照常回填。"""
    captured: list[dict] = []
    port = _gateway_port(_REASONING_SSE, captured)
    set_last_usage(None)
    try:
        pieces = [p async for p in port.stream_complete_events([{"role": "user", "content": "问"}], trace_id=_TRACE)]
    finally:
        await port.aclose()

    assert [(p.reasoning, p.content) for p in pieces] == [
        ("雷击会", ""),
        ("导致跳闸", ""),
        ("", "是的，"),
        ("收尾", "雷击导致。"),
    ]
    assert captured[0]["stream"] is True  # 流式请求体成立
    usage = get_last_usage()
    assert usage is not None and usage.token_in == 10 and usage.token_out == 6


async def test_网关纯文本面_reasoning丢弃_既有契约不变() -> None:
    """stream_complete 只产 content 字符串（reasoning 丢弃）——既有调用方零感知。"""
    captured: list[dict] = []
    port = _gateway_port(_REASONING_SSE, captured)
    try:
        pieces = [p async for p in port.stream_complete([{"role": "user", "content": "问"}], trace_id=_TRACE)]
    finally:
        await port.aclose()

    assert pieces == ["是的，", "雷击导致。"]


# ── audited / resilience 装饰：结构化面透传与退化装箱 ──────────────────────

_TENANT_STR = "11111111-1111-1111-1111-111111111111"


@contextmanager
def _tenant():
    token = tenant_id_ctx.set(_TENANT_STR)
    try:
        yield
    finally:
        tenant_id_ctx.reset(token)


def _buffer() -> LlmCallAuditBuffer:
    return LlmCallAuditBuffer(None)  # type: ignore[arg-type]  # 本用例不触发 flush


async def test_audited_结构化面透传两路_流终落审计行() -> None:
    stub = PiecesFakeModel([ModelStreamPiece(reasoning="想"), ModelStreamPiece(content="答")])
    audit = _buffer()
    port = AuditedModelPort(stub, audit)
    with _tenant():
        pieces = [p async for p in port.stream_complete_events([{"role": "user", "content": "问"}], trace_id="t-r-1")]

    assert [(p.reasoning, p.content) for p in pieces] == [("想", ""), ("", "答")]
    rows = list(audit._queue)
    assert len(rows) == 1 and rows[0].status == "ok" and rows[0].trace_id == "t-r-1"


class _StrOnlyStub:
    """无结构化面的旧桩：audited/resilience 退化装箱路径的触达口。"""

    provider = "str_stub"
    _model = "stub-chat"

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover — 不触达
        raise AssertionError

    async def stream_complete(self, messages: list[dict[str, Any]], **kw: Any) -> AsyncIterator[str]:
        yield "纯文本一段"


async def test_audited_inner无结构化面_退化装箱仅content() -> None:
    port = AuditedModelPort(_StrOnlyStub(), _buffer())
    pieces = [p async for p in port.stream_complete_events([{"role": "user", "content": "问"}], trace_id="t-r-2")]
    assert [(p.reasoning, p.content) for p in pieces] == [("", "纯文本一段")]


async def test_resilience_结构化面透传与退化装箱_两形态() -> None:
    """FailoverModelPort（组合根最外层）：inner 具备结构化面→透传；旧桩→装箱仅 content。"""
    wrapped = FailoverModelPort(
        PiecesFakeModel([ModelStreamPiece(reasoning="想", content="答")]), provider="p", model="m"
    )
    pieces = [p async for p in wrapped.stream_complete_events([{"role": "user", "content": "问"}], trace_id="t-r-3")]
    assert [(p.reasoning, p.content) for p in pieces] == [("想", "答")]

    fallback = FailoverModelPort(_StrOnlyStub(), provider="p", model="m")
    pieces = [p async for p in fallback.stream_complete_events([{"role": "user", "content": "问"}], trace_id="t-r-4")]
    assert [(p.reasoning, p.content) for p in pieces] == [("", "纯文本一段")]


# ── 工具层：THINKING_* 投影（幂等守卫）────────────────────────────────────


class ScriptedAdapter:
    """按脚本吐 GenerationEvent 的适配器桩（ChatAnswerTool 消费面）。"""

    def __init__(self, script: list[GenerationEvent], *, boom_after: int | None = None) -> None:
        self._script = script
        self._boom_after = boom_after

    async def stream_chat(
        self, turn: ChatTurn, ctx: TenantContext, *, timeout_ms: int = 30_000
    ) -> AsyncIterator[GenerationEvent]:
        for i, event in enumerate(self._script):
            if self._boom_after is not None and i == self._boom_after:
                raise TimeoutError("流中断")
            yield event


def _finish() -> GenerationEvent:
    return GenerationEvent(kind="finish", usage={"token_in": 1}, finish_reason="stop")


async def _invoke(
    script: list[GenerationEvent], turn: ChatTurn, *, boom_after: int | None = None
) -> tuple[list[ChatEvent], TurnBox, Any, ChatTurn]:
    events: list[ChatEvent] = []
    box = TurnBox()
    tool = ChatAnswerTool(ScriptedAdapter(script, boom_after=boom_after), turn, box, events.append)
    call = ToolCall(action_iri="http://ontology.example/action/chat_answer", param_hash="h")
    result = await tool.invoke(call, make_ctx(), timeout_ms=30_000)
    return events, box, result, turn


async def test_工具层思考流投影_START_CONTENT_END成对且序正确() -> None:
    """reasoning→text→finish：START/CONTENT*/END 恰一对，END 先于首条 TEXT_MESSAGE_CONTENT。"""
    script = [
        GenerationEvent(kind="reasoning_delta", delta="先想"),
        GenerationEvent(kind="reasoning_delta", delta="再想"),
        GenerationEvent(kind="text_delta", delta="答案"),
        GenerationEvent(kind="text_delta", delta="。"),
        _finish(),
    ]
    events, box, result, turn = await _invoke(script, make_turn())
    names = [e.name for e in events]

    assert names == [
        ChatEventName.TOOL_CALL_START,
        ChatEventName.TOOL_CALL_ARGS,
        ChatEventName.TOOL_CALL_END,
        ChatEventName.TEXT_MESSAGE_START,
        ChatEventName.THINKING_START,
        ChatEventName.THINKING_CONTENT,
        ChatEventName.THINKING_CONTENT,
        ChatEventName.THINKING_END,
        ChatEventName.TEXT_MESSAGE_CONTENT,
        ChatEventName.TEXT_MESSAGE_CONTENT,
        ChatEventName.TEXT_MESSAGE_END,
        ChatEventName.TOOL_CALL_RESULT,
    ]
    start = events[4]
    assert start.data == {"message_id": turn.message_id}  # 无 effort → 载荷省略
    assert [e.data["delta"] for e in events if e.name is ChatEventName.THINKING_CONTENT] == ["先想", "再想"]
    assert events[7].data == {"message_id": turn.message_id}  # THINKING_END
    assert box.answer == "答案。"  # 思考文本不进回答
    assert result.ok is True
    # trace_id 经 ChatEvent 携带、wire_data 补缺进 wire 载荷（40 篇 §4.2）
    thinking_names = THINKING_PERSISTED_EVENTS | THINKING_REALTIME_ONLY_EVENTS
    assert all(e.trace_id == _TRACE for e in events if e.name in thinking_names)
    thinking = [e for e in events if e.name is ChatEventName.THINKING_START]
    assert wire_data(thinking[0])["trace_id"] == _TRACE


async def test_工具层_START载荷透传reasoning_effort() -> None:
    """turn.reasoning_effort 在场 → START {message_id, reasoning_effort}（请求参数透传面）。"""
    events, _box, _result, turn = await _invoke(
        [GenerationEvent(kind="reasoning_delta", delta="想"), _finish()], make_turn(reasoning_effort="high")
    )
    start = next(e for e in events if e.name is ChatEventName.THINKING_START)
    assert start.data == {"message_id": turn.message_id, "reasoning_effort": "high"}


async def test_工具层_END后迟到reasoning丢弃_不重开() -> None:
    """text 已开（END 已发）后的 reasoning_delta 丢弃：每消息至多一对 START/END。"""
    script = [
        GenerationEvent(kind="reasoning_delta", delta="想"),
        GenerationEvent(kind="text_delta", delta="答案"),
        GenerationEvent(kind="reasoning_delta", delta="迟到推理"),
        GenerationEvent(kind="text_delta", delta="补"),
        _finish(),
    ]
    events, box, _result, _turn = await _invoke(script, make_turn())
    thinking_names = [e.name for e in events if e.name.value.startswith("THINKING")]
    assert thinking_names == [
        ChatEventName.THINKING_START,
        ChatEventName.THINKING_CONTENT,
        ChatEventName.THINKING_END,
    ]  # 迟到增量不再开新对
    assert box.answer == "答案补"


async def test_工具层_失败侧收口思考流_END在error终态前() -> None:
    """流中途失败：已开的思考流以 END 收口（防悬挂 START），再走 TEXT_MESSAGE_END(error)。"""
    events, box, result, _turn = await _invoke(
        [GenerationEvent(kind="reasoning_delta", delta="想"), GenerationEvent(kind="text_delta", delta="答案")],
        make_turn(),
        boom_after=1,
    )
    names = [e.name for e in events]
    assert names[-2:] == [ChatEventName.TEXT_MESSAGE_END, ChatEventName.TOOL_CALL_RESULT]
    assert names.count(ChatEventName.THINKING_END) == 1
    assert names.index(ChatEventName.THINKING_END) < names.index(ChatEventName.TEXT_MESSAGE_END)
    error_end = events[-2]
    assert error_end.data["finish_reason"] == "error"
    assert box.ok is False and result.ok is False


async def test_工具层_无reasoning_主干事件零变化() -> None:
    """纯文本流：不出现任何 THINKING_*（主干波形态与 reasoning 透传批之前一致）。"""
    events, _box, _result, _turn = await _invoke(
        [GenerationEvent(kind="text_delta", delta="答案"), _finish()], make_turn()
    )
    assert not [e for e in events if e.name.value.startswith("THINKING")]


# ── 落库口径：THINKING_CONTENT 豁免，START/END 落账本 ──────────────────────


def test_落库集合_THINKING_CONTENT纯实时_START_END落账本() -> None:
    """分类集合（SUBRUN_UPDATED 先例同构）+ 网关校验集全枚举直通自动放行。"""
    assert THINKING_REALTIME_ONLY_EVENTS == frozenset({ChatEventName.THINKING_CONTENT})
    assert THINKING_PERSISTED_EVENTS == frozenset({ChatEventName.THINKING_START, ChatEventName.THINKING_END})
    assert THINKING_REALTIME_ONLY_EVENTS | THINKING_PERSISTED_EVENTS <= set(ChatEventName)
    assert THINKING_REALTIME_ONLY_EVENTS.isdisjoint(EXEC_PERSISTED_EVENTS)
    assert EXEC_REALTIME_ONLY_EVENTS == frozenset({ChatEventName.SUBRUN_UPDATED})  # 先例口径未被扰动
    for name in (ChatEventName.THINKING_START, ChatEventName.THINKING_CONTENT, ChatEventName.THINKING_END):
        assert name.value in MAINSTREAM_EVENT_NAMES  # 网关主干校验集=全枚举直通


class _RecordingTx:
    """TaskEvent 记录器：event_type/data/replay_root 全记（断言 wire 同源）。"""

    def __init__(self) -> None:
        self.appended: list[tuple[str, dict[str, Any], bool]] = []

    def append(self, _task_id: Any, event: Any, *, replay_root: bool = False) -> None:
        self.appended.append((event.event_type, dict(event.data), replay_root))


class _FakeUow:
    """单租户事务桩（test_worker_sse_push 同款形态）：tasks.append_event 委托共享记录器。"""

    def __init__(self, tx: _RecordingTx) -> None:
        self._tx = tx

    def for_tenant(self, _tenant_id: Any) -> Any:
        return self

    async def __aenter__(self) -> Any:
        tx = self._tx

        class _Tasks:
            @staticmethod
            async def append_event(task_id: Any, event: Any, *, replay_root: bool = False) -> None:
                tx.append(task_id, event, replay_root=replay_root)

        return type("_TxView", (), {"tasks": _Tasks()})()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


async def test_SSE双写钩子_THINKING_START_END落库_CONTENT豁免() -> None:
    """build_exec_event_dual_write：START/END 进 task_events（回放根），CONTENT no-op。"""
    tx = _RecordingTx()
    dual_write = build_exec_event_dual_write(_FakeUow(tx), TENANT)  # type: ignore[arg-type]
    task_id = uuid.uuid4()
    for name, data in [
        (ChatEventName.THINKING_START, {"message_id": "m1"}),
        (ChatEventName.THINKING_CONTENT, {"message_id": "m1", "delta": "想"}),
        (ChatEventName.THINKING_END, {"message_id": "m1"}),
    ]:
        await dual_write(task_id, ChatEvent(name=name, data=data, run_id=uuid.uuid4(), trace_id=_TRACE))

    assert [a[0] for a in tx.appended] == ["THINKING_START", "THINKING_END"]  # CONTENT 豁免
    assert all(a[2] is True for a in tx.appended)  # 回放根（钩子口径）
    assert tx.appended[0][1] == {"message_id": "m1", "trace_id": _TRACE}  # wire_data 补 trace_id


class _FakeOrchestrator:
    def __init__(self, script: list[ChatEvent]) -> None:
        self._script = script

    async def stream_chat(self, command: ChatCommand) -> AsyncIterator[ChatEvent]:
        for evt in self._script:
            yield ChatEvent(name=evt.name, data=dict(evt.data), run_id=command.run_id, trace_id=command.trace_id)


def _make_worker(
    script: list[ChatEvent], published: list[tuple[Any, str, dict[str, Any]]], tx: _RecordingTx
) -> TaskRunWorker:
    async def publisher(session_id: Any, name: str, data: dict[str, Any]) -> None:
        published.append((session_id, name, data))

    return TaskRunWorker(
        uow=_FakeUow(tx),  # type: ignore[arg-type]
        poller=None,
        orchestrator_provider=lambda: _FakeOrchestrator(script),
        rng=lambda: 0.5,
        event_publisher=publisher,
    )


async def test_worker落库路径_THINKING_CONTENT仅推送不落库_START_END落账本() -> None:
    """_drain_orchestrator：三条 THINKING 全推送；落库仅 START/END（CONTENT 豁免）。"""
    published: list[tuple[Any, str, dict[str, Any]]] = []
    tx = _RecordingTx()
    script = [
        ChatEvent(name=ChatEventName.THINKING_START, data={"message_id": "m1"}),
        ChatEvent(name=ChatEventName.THINKING_CONTENT, data={"message_id": "m1", "delta": "想"}),
        ChatEvent(name=ChatEventName.THINKING_END, data={"message_id": "m1"}),
        ChatEvent(name=ChatEventName.RUN_FINISHED, data={"run_id": "r", "usage": {"total_tokens": 7}}),
    ]
    worker = _make_worker(script, published, tx)
    cmd = ChatCommand(
        tenant_id=TENANT,
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        message="hi",
        trace_id=_TRACE,
    )
    final = await worker._drain_orchestrator(cmd)  # noqa: SLF001

    assert final is None
    assert [p[1] for p in published] == ["THINKING_START", "THINKING_CONTENT", "THINKING_END", "RUN_FINISHED"]
    assert [a[0] for a in tx.appended] == ["THINKING_START", "THINKING_END"]  # CONTENT/终态不落库
    assert tx.appended[0][1]["trace_id"] == _TRACE  # wire_data 同源

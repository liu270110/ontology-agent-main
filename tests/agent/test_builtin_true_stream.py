# tests/agent/test_builtin_true_stream.py
"""H-6 真流式批 · builtin 适配器 stream_chat 生成路径测试。

断言目标：
- 端口具备 stream_complete → 真流式：text_delta 逐段产出（顺序=端口产出顺序，不再
  24 字符切片、空段不产出），complete_structured 不被触达；
- finish 终态携带用量（真流式=网关末块 usage 回填）与 finish_reason；
- 端口仅有 complete_structured（Protocol 扩展前旧桩/AuditedModelPort 过渡期）→ 回退
  伪流式切片（既有口径零变化）；
- 真流式中途端口失败 → 错误码原样上抛（工具层结构化收敛）；
- 网关真流式 + 适配器投影端到端（MockTransport SSE → GenerationEvent 序列）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from services.agent.business.adapters import BuiltinAdapter, ChatTurn, GenerationEvent
from services.agent.domain.model.kernel_context import TenantContext
from services.platform.errors import ErrorCode
from services.platform.llm.gateway import OpenAICompatibleModelPort
from services.platform.llm.usage import set_last_usage
from services.platform.ports.model_port import ModelUnavailableError

TENANT = uuid.uuid4()


def make_ctx() -> TenantContext:
    return TenantContext(tenant_id=TENANT, scopes=("session:chat",), trace_id="trace-true-stream")


def make_turn(*, num_ctx: int | None = None, context_text: str = "【证据】线路A于14:02跳闸。") -> ChatTurn:
    return ChatTurn(
        tenant_id=TENANT,
        session_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        message="线路A为何停电？",
        context_text=context_text,
        num_ctx=num_ctx,
    )


class StreamingFakeModel:
    """真流式端口桩：stream_complete 逐段产出；complete_structured 仅回退路径可触达。"""

    provider = "streaming_fake"

    def __init__(self, pieces: list[str]) -> None:
        self.pieces = pieces
        self.stream_kwargs: dict[str, Any] | None = None
        self.structured_calls = 0

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.structured_calls += 1
        return {"answer": "不应走回退路径"}

    async def stream_complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
        timeout_s: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        trace_id: str | None = None,
    ) -> Any:
        self.stream_kwargs = {
            "messages": messages,
            "temperature": temperature,
            "num_ctx": num_ctx,
            "timeout_s": timeout_s,
            "trace_id": trace_id,
        }
        for piece in self.pieces:
            yield piece


class StructuredOnlyModel:
    """旧口径端口桩（Protocol 扩展前）：仅 complete_structured——回退路径触达口。"""

    def __init__(self, answer: str = "雷击导致线路 A 跳闸。") -> None:
        self.answer = answer
        self.calls = 0

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {"answer": self.answer}


async def _collect(adapter: BuiltinAdapter, turn: ChatTurn, ctx: TenantContext, **kw: Any) -> list[GenerationEvent]:
    return [event async for event in adapter.stream_chat(turn, ctx, **kw)]


# ── 真流式路径 ──────────────────────────────────────────────────────────


async def test_builtin_真流式逐段产出_顺序与端口一致且不再切片():
    """H-6 核心：delta 序列=端口产出序列（含空段过滤），不触发 complete_structured。"""
    model = StreamingFakeModel(["雷击", "", "导致线路 A ", "跳闸。"])  # 长度异于 24 步长=证伪再切片
    events = await _collect(BuiltinAdapter(model), make_turn(), make_ctx(), timeout_ms=30_000)

    text_events = [e for e in events if e.kind == "text_delta"]
    assert [e.delta for e in text_events] == ["雷击", "导致线路 A ", "跳闸。"]  # 空段不产出
    assert "".join(e.delta for e in text_events) == "雷击导致线路 A 跳闸。"
    assert model.structured_calls == 0  # 真流式路径不触达结构化面
    finish = events[-1]
    assert finish.kind == "finish" and finish.finish_reason == "stop" and finish.usage == {}


async def test_builtin_真流式透传消息组装_trace_id_超时与num_ctx():
    """messages=system+user 两块；trace_id/timeout_s/num_ctx 逐项贯穿端口（C2 可追溯）。"""
    model = StreamingFakeModel(["答"])
    turn = make_turn(num_ctx=4096)
    events = await _collect(BuiltinAdapter(model), turn, make_ctx(), timeout_ms=5_000)

    assert events[-1].kind == "finish"
    kwargs = model.stream_kwargs
    assert kwargs is not None
    assert [m["role"] for m in kwargs["messages"]] == ["system", "user"]
    assert "线路A为何停电？" in kwargs["messages"][1]["content"]
    assert "线路A于14:02跳闸。" in kwargs["messages"][0]["content"]  # 已标界上下文随 system
    assert kwargs["trace_id"] == "trace-true-stream"
    assert kwargs["timeout_s"] == 5.0  # timeout_ms → 秒
    assert kwargs["num_ctx"] == 4096
    assert kwargs["temperature"] is None  # 对话档：服务端缺省


async def test_builtin_真流式中途端口失败_错误码原样上抛():
    """端口中途抛 5002 → 适配器不吞不换码（工具层结构化收敛，02 §4.1 ④）。"""

    class _FailMidStream(StreamingFakeModel):
        async def stream_complete(self, messages: list[dict[str, Any]], **kw: Any) -> Any:
            yield "雷击"
            raise ModelUnavailableError("端点中途断流")

    adapter = BuiltinAdapter(_FailMidStream(["雷击", "后续"]))
    with pytest.raises(ModelUnavailableError) as exc_info:
        _ = await _collect(adapter, make_turn(), make_ctx())
    assert exc_info.value.code == int(ErrorCode.LLM_UNAVAILABLE)


async def test_builtin_网关真流式端到端_SSE投影为GenerationEvent序列():
    """OpenAICompatibleModelPort（MockTransport SSE）+ builtin：逐段投影，末块 usage 入 finish。"""
    captured: list[dict] = []
    sse = "".join(
        line + "\n\n"
        for line in [
            'data: {"choices":[{"delta":{"role":"assistant"}}]}',
            'data: {"choices":[{"delta":{"content":"雷击导致"}}]}',
            'data: {"choices":[{"delta":{"content":"线路 A 跳闸。"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":10,"completion_tokens":6}}',
            "data: [DONE]",
        ]
    ).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, content=sse)

    model = OpenAICompatibleModelPort(
        base_url="http://llm", api_key="k", model="m", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    set_last_usage(None)  # 清上下文（防用例间串扰）
    try:
        events = await _collect(BuiltinAdapter(model), make_turn(), make_ctx(), timeout_ms=30_000)
    finally:
        await model.aclose()

    text_events = [e for e in events if e.kind == "text_delta"]
    assert [e.delta for e in text_events] == ["雷击导致", "线路 A 跳闸。"]
    assert captured[0]["stream"] is True  # 网关确以流式请求
    finish = events[-1]
    assert finish.kind == "finish" and finish.finish_reason == "stop"
    assert finish.usage == {"token_in": 10, "token_out": 6, "cache_read_tokens": 0}  # 末块 usage → finish


# ── 伪流式回退路径（既有口径零变化）─────────────────────────────────────


async def test_builtin_端口无流式面_回退结构化切片_既有口径不变():
    """仅 complete_structured 的端口（旧桩/过渡期）→ 24 字符切片透传，拼接=全文。"""
    answer = "雷击导致线路 A 于 14:02 跳闸，原因认定为雷击。"  # 28 字符 → 24+4 两片（步长可观测）
    model = StructuredOnlyModel(answer)
    events = await _collect(BuiltinAdapter(model), make_turn(), make_ctx(), timeout_ms=30_000)

    text_events = [e for e in events if e.kind == "text_delta"]
    assert model.calls == 1  # 单次结构化补全
    assert [len(e.delta) for e in text_events] == [24, 4]  # 固定步长切片（回退形态）
    assert "".join(e.delta for e in text_events) == answer
    assert events[-1].kind == "finish" and events[-1].finish_reason == "stop"

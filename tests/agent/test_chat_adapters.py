# tests/agent/test_chat_adapters.py
"""chat 适配器单测（计划 3.2）：builtin（ModelPort 直驱）+ claude（Anthropic 直连通道）。

断言目标：
- builtin：流式增量拼接=答案全文、finish 携带用量、ModelPort 结构化产物消费；
- claude：无 API key 注册成功、调用报已登记码 5002（任务口径）；
- claude：SSE 行解析（text_delta / usage / stop_reason）、prompt caching system 块；
- 适配器注册面：AgentSlot 契约（meta 语义标注 + spawn_sub 签名）可进内核分发器。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from services.agent.business.adapters import BuiltinAdapter, ClaudeAdapter
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.platform.errors import ErrorCode
from services.platform.ports.model_port import ModelUnavailableError

TENANT = uuid.uuid4()


def make_ctx() -> TenantContext:
    return TenantContext(tenant_id=TENANT, scopes=("session:chat",), trace_id="trace-adapter-test")


def make_turn_kwargs() -> dict[str, Any]:
    from services.agent.business.adapters.base import ChatTurn

    turn = ChatTurn(tenant_id=TENANT, session_id=uuid.uuid4(), run_id=uuid.uuid4(), message="线路A为何停电？")
    return {"turn": turn, "ctx": make_ctx()}


# ── builtin（ModelPort 直驱）──────────────────────────────────────────────


class StubModelPort:
    """最小 ModelPort 桩：记录调用并返回确定性答案产物。"""

    def __init__(self, answer: str = "雷击导致线路 A 跳闸。", error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls = 0
        self.kwargs: dict[str, Any] = {}

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return {"answer": self.answer}


async def test_builtin_流式增量拼接等于答案全文且_finish_收尾() -> None:
    """builtin 生成：text_delta 切片透传拼接=全文；finish 携带 finish_reason。"""
    adapter = BuiltinAdapter(StubModelPort("雷击导致线路 A 跳闸。"))
    events = [event async for event in adapter.stream_chat(**make_turn_kwargs(), timeout_ms=30_000)]

    assert events[-1].kind == "finish"
    assert events[-1].finish_reason == "stop"
    assert "".join(e.delta for e in events if e.kind == "text_delta") == "雷击导致线路 A 跳闸。"


async def test_builtin_提示词携带消息与_trace_id_透传() -> None:
    """user 提示含本条消息；trace_id 贯穿到 ModelPort（C2 可追溯）。"""
    model = StubModelPort()
    adapter = BuiltinAdapter(model)
    kwargs = make_turn_kwargs()
    _ = [event async for event in adapter.stream_chat(**kwargs, timeout_ms=30_000)]

    assert model.kwargs["trace_id"] == "trace-adapter-test"
    assert "线路A为何停电？" in model.kwargs["user"]


async def test_builtin_模型不可用时抛_5002_端口异常() -> None:
    """ModelPort 不可达（已登记 5002）→ 适配器原样上抛（工具层结构化收敛）。"""
    adapter = BuiltinAdapter(StubModelPort(error=ModelUnavailableError("端点不可达")))
    with pytest.raises(ModelUnavailableError) as exc_info:
        _ = [event async for event in adapter.stream_chat(**make_turn_kwargs(), timeout_ms=30_000)]
    assert exc_info.value.code == int(ErrorCode.LLM_UNAVAILABLE)


async def test_builtin_与_claude_可按_AgentSlot_契约注册进分发器() -> None:
    """注册面（02 §4.2）：meta 语义标注齐备（action_iri），register_agent_slot 拒绝即不合规。"""
    dispatcher = ExtensionDispatcher()
    dispatcher.register_agent_slot(BuiltinAdapter(StubModelPort()))
    dispatcher.register_agent_slot(ClaudeAdapter(api_key="test-key"))
    assert dispatcher.agent_slot("chat.builtin") is not None
    assert dispatcher.agent_slot("chat.claude") is not None


async def test_适配器_meta_语义标注带_行动类() -> None:
    """§7.4 无语义标注不上架：两个适配器的 semantic_annotation 均绑定 chat 行动类 IRI。"""
    for adapter in (BuiltinAdapter(StubModelPort()), ClaudeAdapter()):
        meta: ExtensionMeta = adapter.meta
        assert "." in meta.name and meta.version.count(".") == 2
        assert "action_iri" in meta.semantic_annotation


# ── claude（Anthropic 直连通道）──────────────────────────────────────────


async def test_claude_无_API_key_注册成功但调用报_5002() -> None:
    """任务口径：无 key 构造成功（组合根恒装配），调用抛 5002 LLM_UNAVAILABLE。"""
    adapter = ClaudeAdapter()  # 无 key
    with pytest.raises(ModelUnavailableError) as exc_info:
        _ = [event async for event in adapter.stream_chat(**make_turn_kwargs(), timeout_ms=30_000)]
    assert exc_info.value.code == int(ErrorCode.LLM_UNAVAILABLE)


def _anthropic_sse_payload() -> list[bytes]:
    """Anthropic Messages SSE 流（content_block_delta / message_start / message_delta）。"""
    lines = [
        'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":120,'
        '"cache_read_input_tokens":64}}}',
        'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta",'
        '"text":"雷击导致"}}',
        'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta",'
        '"text":"线路 A 跳闸。"}}',
        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":16}}',
        'event: message_stop\ndata: {"type":"message_stop"}',
    ]
    return [(line + "\n\n").encode("utf-8") for line in lines]


async def _sse_stream() -> Any:
    """异步字节流（AsyncClient+MockTransport 流式响应要求 AsyncByteStream）。"""
    for chunk in _anthropic_sse_payload():
        yield chunk


async def test_claude_SSE_流解析_增量与用量_停止原因归一() -> None:
    """httpx MockTransport 流：text_delta 透传拼接、token 双向计量、end_turn→stop 归一。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        captured["system_cache_control"] = captured["body"]["system"][0].get("cache_control")
        return httpx.Response(200, content=_sse_stream())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ClaudeAdapter(api_key="test-key", client=client)
    events = [event async for event in adapter.stream_chat(**make_turn_kwargs(), timeout_ms=30_000)]
    await adapter.aclose()

    finish = events[-1]
    assert finish.kind == "finish" and finish.finish_reason == "stop"
    assert finish.usage == {"token_in": 120, "cache_read_tokens": 64, "token_out": 16}
    assert "".join(e.delta for e in events if e.kind == "text_delta") == "雷击导致线路 A 跳闸。"
    assert captured["system_cache_control"] == {"type": "ephemeral"}  # prompt caching（锚点 §3.3）
    assert captured["body"]["stream"] is True


async def test_claude_非_200_响应结构化报_5002() -> None:
    """上游非 200 → ModelUnavailableError（5002，已登记码），不裸异常逃逸。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"overloaded")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ClaudeAdapter(api_key="test-key", client=client)
    with pytest.raises(ModelUnavailableError) as exc_info:
        _ = [event async for event in adapter.stream_chat(**make_turn_kwargs(), timeout_ms=30_000)]
    await adapter.aclose()
    assert exc_info.value.code == int(ErrorCode.LLM_UNAVAILABLE)
    assert "503" in str(exc_info.value)

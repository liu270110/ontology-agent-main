# tests/platform/test_model_port_openai_face.py
"""H-6 模型协议主干批 · OpenAI 对话生成面测试：complete（裸对话）+ stream_complete（真流式）。

MockTransport 双形态：非流式 chat completions 与 SSE 流式（role 首块 + 3 个 delta chunk +
末块 usage + [DONE]）；断言全文返回、逐段产出顺序、tools/tool_choice 原样透传、
参数注入纪律（None 不注入）、错误分类（5001/5002）与 FakeModelPort 对话面确定性口径。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from services.platform.llm.gateway import (
    FakeModelPort,
    ModelGatewayTimeoutError,
    ModelGatewayUnavailableError,
    OpenAICompatibleModelPort,
)
from services.platform.llm.usage import LlmUsage, get_last_usage, set_last_usage

_MESSAGES = [
    {"role": "system", "content": "你是平台助手"},
    {"role": "user", "content": "线路A为何停电？"},
]
_TOOLS = [{"type": "function", "function": {"name": "query_outage", "parameters": {"type": "object"}}}]
_FULL_TEXT = "雷击导致线路 A 跳闸。"


def _capture_client(captured: list[dict], *, payload: dict | None = None) -> httpx.AsyncClient:
    """MockTransport 客户端：捕获请求体，返回固定非流式响应（不触网）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json=payload or {"choices": [{"message": {"content": _FULL_TEXT}}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _stream_client(captured: list[dict], *, status: int = 200) -> httpx.AsyncClient:
    """MockTransport 流式客户端：SSE 序列 = role 首块 + 3 delta + 末块 usage + [DONE]。"""
    events: list[str | dict[str, Any]] = [
        {"choices": [{"delta": {"role": "assistant"}}]},  # 首块无 content（安全跳过）
        {"choices": [{"delta": {"content": "雷击导致"}}]},
        {"choices": [{"delta": {"content": "线路 A "}}]},
        {"choices": [{"delta": {"content": "跳闸。"}}]},
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 6},
        },
        "[DONE]",
        {"choices": [{"delta": {"content": "DONE后不应产出"}}]},  # [DONE] 后的有效增量：break 失败即漏出
    ]
    # [DONE] 之后追加一截脏数据：终止后不再消费（断言提前 break 不误读）
    body = "".join(
        f"data: {e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)}\n\n" for e in events
    ).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(status, content=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── complete（裸对话，非 JSON 模式）──────────────────────────────────────


async def test_complete_裸对话返回全文_不带JSON模式与流式标记():
    captured: list[dict] = []
    port = OpenAICompatibleModelPort(base_url="http://llm", api_key="k", model="m", client=_capture_client(captured))
    # Act
    text = await port.complete(_MESSAGES, trace_id="t-complete")
    # Assert：全文直返（无 response_format / 无 stream），messages 原样入体
    assert text == _FULL_TEXT
    assert captured[0]["model"] == "m" and captured[0]["messages"] == _MESSAGES
    assert "response_format" not in captured[0] and "stream" not in captured[0]
    await port.aclose()


async def test_complete_采样参数与num_ctx_仅显式时注入():
    captured: list[dict] = []
    port = OpenAICompatibleModelPort(base_url="http://llm", api_key="k", model="m", client=_capture_client(captured))
    # Act：显式注入
    await port.complete(_MESSAGES, temperature=0.7, max_tokens=256, num_ctx=4096)
    # Assert：全部入体
    assert captured[0]["temperature"] == 0.7 and captured[0]["max_tokens"] == 256
    assert captured[0]["num_ctx"] == 4096
    # Act：缺省（None=服务端默认）
    await port.complete(_MESSAGES)
    # Assert：不注入
    for key in ("temperature", "max_tokens", "num_ctx"):
        assert key not in captured[1]
    await port.aclose()


async def test_complete_tools与tool_choice原样透传():
    captured: list[dict] = []
    port = OpenAICompatibleModelPort(base_url="http://llm", api_key="k", model="m", client=_capture_client(captured))
    # Act（ReAct 双模式前置件，docs/Agent/02 §11.3：本期只透传不消费）
    await port.complete(_MESSAGES, tools=_TOOLS, tool_choice="auto")
    # Assert
    assert captured[0]["tools"] == _TOOLS and captured[0]["tool_choice"] == "auto"
    # Act：不传 tools → 不注入
    await port.complete(_MESSAGES)
    assert "tools" not in captured[1] and "tool_choice" not in captured[1]
    await port.aclose()


# ── stream_complete（真流式 SSE）─────────────────────────────────────────


async def test_stream_complete_逐段产出顺序正确_DONE终止_流式请求体成立():
    captured: list[dict] = []
    port = OpenAICompatibleModelPort(base_url="http://llm", api_key="k", model="m", client=_stream_client(captured))
    # Act
    pieces = [
        piece async for piece in port.stream_complete(_MESSAGES, tools=_TOOLS, tool_choice="auto", trace_id="t-s")
    ]
    # Assert：产出顺序=模型产出顺序（空段/无 content 块不产出），[DONE] 终止
    assert pieces == ["雷击导致", "线路 A ", "跳闸。"]
    assert "".join(pieces) == _FULL_TEXT
    # 流式请求体：stream=True + tools 原样透传（Ollama 兼容层忽略不识别字段）
    assert captured[0]["stream"] is True
    assert captured[0]["tools"] == _TOOLS and captured[0]["tool_choice"] == "auto"
    assert captured[0]["messages"] == _MESSAGES
    await port.aclose()


async def test_stream_complete_末块usage回填用量上下文():
    captured: list[dict] = []
    port = OpenAICompatibleModelPort(base_url="http://llm", api_key="k", model="m", client=_stream_client(captured))
    set_last_usage(None)  # 清上下文（防用例间串扰）
    # Act：消费完整流（末块带 usage——上游主动携带口径；本期不发 stream_options）
    _ = [piece async for piece in port.stream_complete(_MESSAGES)]
    # Assert
    assert get_last_usage() == LlmUsage(token_in=10, token_out=6, cache_read_tokens=0)
    await port.aclose()


async def test_非200结构化报5002_非流式与流式同口径():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"overloaded")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    port = OpenAICompatibleModelPort(base_url="http://llm", api_key="k", model="m", client=client)
    # Act / Assert：complete
    with pytest.raises(ModelGatewayUnavailableError) as ei:
        await port.complete(_MESSAGES)
    assert ei.value.code == 5002 and "503" in str(ei.value)
    # Act / Assert：stream_complete（流式非 200 同样 5002，不裸异常）
    with pytest.raises(ModelGatewayUnavailableError) as ei2:
        _ = [piece async for piece in port.stream_complete(_MESSAGES)]
    assert ei2.value.code == 5002 and "503" in str(ei2.value)
    await port.aclose()


async def test_超时与不可达分类_5001与5002():
    def raise_client(exc_factory) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            raise exc_factory(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    # 读超时 → 5001（LLM_TIMEOUT）
    port = OpenAICompatibleModelPort(
        base_url="http://llm",
        api_key="k",
        model="m",
        client=raise_client(lambda r: httpx.ReadTimeout("read", request=r)),
    )
    with pytest.raises(ModelGatewayTimeoutError) as ei:
        await port.complete(_MESSAGES)
    assert ei.value.code == 5001
    await port.aclose()
    # 连接失败 → 5002（LLM_UNAVAILABLE）
    port2 = OpenAICompatibleModelPort(
        base_url="http://llm",
        api_key="k",
        model="m",
        client=raise_client(lambda r: httpx.ConnectError("refused", request=r)),
    )
    with pytest.raises(ModelGatewayUnavailableError) as ei2:
        _ = [piece async for piece in port2.stream_complete(_MESSAGES)]
    assert ei2.value.code == 5002
    await port2.aclose()


# ── FakeModelPort 对话面（确定性口径与生产一致）──────────────────────────


async def test_FakeModelPort对话面确定性_拼接user正文逐段产出():
    model = FakeModelPort()
    # Act：裸对话=拼接 user 角色正文（确定性，无网络）
    text = await model.complete(_MESSAGES)
    assert text == "线路A为何停电？"
    # Act：真流式=全文按 16 字符逐段
    pieces = [piece async for piece in model.stream_complete(_MESSAGES)]
    assert "".join(pieces) == text and all(len(p) <= 16 for p in pieces)

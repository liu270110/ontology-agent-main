# tests/agent/test_bridge_http_service_adapter.py
"""F3 常驻服务通用适配器单测（docs/Agent/20 §3.1；05 篇 §2.1/§4.1/§5.2）。

桩策略（零网络）：httpx.MockTransport——
- openai-chat 模式（企业 agent 通用模板）：SSE chat.completion.chunk delta 归一 + usage 末块
  提取（token_in/token_out/cache_read 双兼容）+ session_body_field 会话映射（nanobot 口径）；
- custom-rest 模式：new_session/prompt/events( SSE) 四端点驱动 + event_map 字段路径归一；
- 鉴权（20 篇 §3 ③）：bearer/apikey 请求头断言；
- 探活→degraded：probe() 面（200=健康/不可达=False→记账降级走 agent_health 既有链）；
- 失败结构化：非 200/不可达→5002（ModelUnavailableError，禁裸异常逃逸）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from services.agent.business.adapters.http_service import HttpServiceAdapter
from services.agent.business.adapters.profiles import (
    AgentProfile,
    AuthSpec,
    EndpointSpec,
    EventMapSpec,
    EventRule,
    TransportSpec,
)
from services.agent.business.adapters.session_map import InMemoryAdapterSessionMapper
from services.agent.domain.model.kernel_context import TenantContext
from services.platform.errors import ErrorCode
from services.platform.ports.model_port import ModelUnavailableError

TENANT = uuid.uuid4()


def make_ctx() -> TenantContext:
    return TenantContext(tenant_id=TENANT, scopes=("session:chat",), trace_id="trace-g2-http")


def make_turn(message: str = "线路A为何停电？", *, with_history: bool = False) -> Any:
    from services.agent.business.adapters.base import ChatTurn

    history: tuple[tuple[str, str], ...] = ()
    if with_history:
        history = (("assistant", "上轮回答：雷击跳闸。"), ("user", "上轮提问：线路A状态？"))
    return ChatTurn(
        tenant_id=TENANT,
        session_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        message=message,
        history=history,
    )


def openai_profile(**transport_kw: Any) -> AgentProfile:
    """openai-chat 画像（profiles/service/openai-compatible.yaml 的测试同构）。"""
    return AgentProfile(
        profile="openai-compatible",
        tool="http-generic",
        form="F3",
        protocol="openai-chat",
        transport=TransportSpec(
            base_url="http://svc.test/v1",
            model="svc-model",
            auth=AuthSpec(**transport_kw.pop("auth", {"type": "bearer"})),
            health_path="/models",
            session_body_field=transport_kw.pop("session_body_field", "session_id"),
        ),
        capabilities={"feed": False, "approval": False, "resume": True, "artifacts": False},
    )


def custom_rest_profile() -> AgentProfile:
    """custom-rest 画像（20 篇 §3.1 ②：endpoints 四件 + events sse + event_map）。"""
    return AgentProfile(
        profile="svc-rest",
        tool="http-generic",
        form="F3",
        protocol="custom-rest",
        session_map={"tool_session_field": "sessionID"},
        transport=TransportSpec(
            base_url="http://rest.test",
            auth=AuthSpec(type="apikey", header="X-Svc-Key"),
            endpoints={
                "new_session": EndpointSpec(method="POST", path="/session"),
                "prompt": EndpointSpec(method="POST", path="/session/{sid}/prompt"),
                "events": EndpointSpec(method="GET", path="/session/{sid}/events", type="sse"),
                "permission": EndpointSpec(method="POST", path="/session/{sid}/permissions/{pid}"),
            },
        ),
        event_map=EventMapSpec(
            kind_path="$.type",
            rules=[
                EventRule(match="message.part.updated", emit="text_delta", delta_path="$.delta"),
                EventRule(match="run.finished", emit="finish", usage_path="$.usage"),
            ],
        ),
    )


# ── openai-chat（企业 agent 通用模板）────────────────────────────────────────


def _openai_sse_lines() -> list[bytes]:
    """OpenAI 兼容 SSE 流：role 首块 / 双 delta / usage-only 末块 / [DONE]。"""
    lines = [
        'data: {"choices":[{"delta":{"role":"assistant"}}]}',  # role 首块：无 content，跳过
        'data: {"choices":[{"delta":{"content":"雷击导致"}}]}',
        'data: {"choices":[{"delta":{"content":"线路 A 跳闸。"}}]}',
        "keep-alive",  # 非 data 行：跳过不断流
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":120,'
        '"completion_tokens":16,"prompt_tokens_details":{"cached_tokens":64}}}',
        "data: [DONE]",
    ]
    return [(line + "\n\n").encode("utf-8") for line in lines]


async def _byte_stream(chunks: list[bytes]) -> Any:
    for chunk in chunks:
        yield chunk


async def test_openai_chat_SSE_delta归一与用量末块提取() -> None:
    """SSE delta→text_delta 归一拼接=全文；usage 末块提取三键（缓存双兼容 cached_tokens）。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, content=_byte_stream(_openai_sse_lines()))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = HttpServiceAdapter(openai_profile(), api_key="sk-test", client=client)
    turn = make_turn()
    events = [event async for event in adapter.stream_chat(turn, make_ctx())]
    await adapter.aclose()

    assert captured["path"] == "/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-test"
    body = captured["body"]
    assert body["model"] == "svc-model" and body["stream"] is True
    assert body["messages"][-1] == {"role": "user", "content": "线路A为何停电？"}
    assert "".join(e.delta for e in events if e.kind == "text_delta") == "雷击导致线路 A 跳闸。"
    finish = events[-1]
    assert finish.kind == "finish"
    assert finish.usage == {"token_in": 120, "token_out": 16, "cache_read_tokens": 64}


async def test_openai_chat_会话映射_首轮派生登记_次轮复用同id() -> None:
    """nanobot 口径：session_body_field 注入请求体；首轮确定性派生+登记映射表，次轮复用。"""
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, content=_byte_stream(_openai_sse_lines()))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    mapper = InMemoryAdapterSessionMapper()
    adapter = HttpServiceAdapter(openai_profile(), api_key="sk-test", client=client, session_mapper=mapper)
    turn = make_turn()
    _ = [e async for e in adapter.stream_chat(turn, make_ctx())]
    _ = [e async for e in adapter.stream_chat(make_turn(with_history=True), make_ctx())]
    await adapter.aclose()

    session_value = str(turn.session_id)
    assert bodies[0]["session_id"] == session_value  # 首轮：确定性派生（platform session id 直用）
    assert bodies[1]["session_id"] == session_value  # 次轮：映射表复用同 id
    assert await mapper.get(tenant_id=TENANT, session_id=turn.session_id) == session_value


async def test_openai_chat_非200_结构化报_5002() -> None:
    """上游非 200 → ModelUnavailableError（5002 已登记码），不裸异常逃逸。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"overloaded")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = HttpServiceAdapter(openai_profile(), api_key="k", client=client)
    with pytest.raises(ModelUnavailableError) as exc_info:
        _ = [e async for e in adapter.stream_chat(make_turn(), make_ctx())]
    await adapter.aclose()
    assert exc_info.value.code == int(ErrorCode.LLM_UNAVAILABLE)
    assert "503" in str(exc_info.value)


async def test_openai_chat_缺_base_url_调用报_5002() -> None:
    """部署期注入缺失（profile/部署两层 base_url 均空）→ 调用期结构化拒绝（宁拒不错载）。"""
    profile = AgentProfile(
        profile="openai-compatible",
        tool="http-generic",
        form="F3",
        protocol="openai-chat",
        transport=TransportSpec(base_url="", model="m"),  # 两层均未注入
    )
    adapter = HttpServiceAdapter(profile, base_url="")
    with pytest.raises(ModelUnavailableError):
        _ = [e async for e in adapter.stream_chat(make_turn(), make_ctx())]


# ── 鉴权（20 篇 §3 ③）────────────────────────────────────────────────────────


def test_鉴权_apikey_自定义头名(monkeypatch: pytest.MonkeyPatch) -> None:
    """apikey 模式：凭据自 api_key_env 环境变量 → 自定义请求头名。"""
    monkeypatch.setenv("SVC_API_KEY", "svc-secret")
    profile = openai_profile(auth={"type": "apikey", "api_key_env": "SVC_API_KEY", "header": "X-Svc-Key"})
    adapter = HttpServiceAdapter(profile)
    headers = adapter._resolve_auth_header(profile.transport.auth, api_key="")
    assert headers == {"X-Svc-Key": "svc-secret"}


def test_鉴权_none_与缺凭据_不带头() -> None:
    """auth.type=none / 凭据缺失 → 不注入鉴权头（注册成功、无凭据明文面）。"""
    profile = openai_profile(auth={"type": "none"})
    assert HttpServiceAdapter(profile)._resolve_auth_header(profile.transport.auth, api_key="") == {}
    bearer = openai_profile(auth={"type": "bearer", "api_key_env": ""})
    assert HttpServiceAdapter(bearer)._resolve_auth_header(bearer.transport.auth, api_key="") == {}


# ── custom-rest（服务画像自画像）─────────────────────────────────────────────


def _rest_sse_lines() -> list[bytes]:
    lines = [
        'data: {"type":"message.part.updated","delta":"停电原因："}',
        'data: {"type":"unknown.kind","delta":"应被忽略"}',  # 无规则命中：容错跳过
        'data: {"type":"message.part.updated","delta":"雷击"}',
        'data: {"type":"run.finished","usage":{"total_tokens":77}}',
    ]
    return [(line + "\n\n").encode("utf-8") for line in lines]


async def test_custom_rest_四端点驱动与会话映射_event_map归一() -> None:
    """new_session 提取 tool_session_field → prompt {sid} 路径替换 → events SSE event_map 归一。"""
    seen: dict[str, Any] = {"sessions": 0, "prompt_paths": [], "prompt_bodies": [], "event_paths": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session" and request.method == "POST":
            seen["sessions"] += 1
            return httpx.Response(200, json={"sessionID": "svc-123"})
        if "/prompt" in request.url.path:
            seen["prompt_paths"].append(request.url.path)
            seen["prompt_bodies"].append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={})
        if "/events" in request.url.path:
            seen["event_paths"].append(request.url.path)
            return httpx.Response(200, content=_byte_stream(_rest_sse_lines()))
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    mapper = InMemoryAdapterSessionMapper()
    adapter = HttpServiceAdapter(custom_rest_profile(), api_key="k", client=client, session_mapper=mapper)
    turn = make_turn(with_history=True)
    events = [e async for e in adapter.stream_chat(turn, make_ctx())]
    await adapter.aclose()

    assert seen["sessions"] == 1  # 会话映射：首轮建，单轮一次
    assert seen["prompt_paths"] == ["/session/svc-123/prompt"]  # {sid} 路径替换
    prompt_body = seen["prompt_bodies"][0]
    assert prompt_body["message"] == "线路A为何停电？"
    assert prompt_body["history"] == [
        {"role": "user", "content": "上轮提问：线路A状态？"},  # 近窗历史反转成时序
        {"role": "assistant", "content": "上轮回答：雷击跳闸。"},
    ]
    assert seen["event_paths"] == ["/session/svc-123/events"]
    assert "".join(e.delta for e in events if e.kind == "text_delta") == "停电原因：雷击"  # 未知 kind 容错跳过
    assert events[-1].kind == "finish" and events[-1].usage == {"total_tokens": 77}
    assert await mapper.get(tenant_id=TENANT, session_id=turn.session_id) == "svc-123"  # adapter_sessions 映射登记


def test_custom_rest_events_ws模式_进程内桩全链() -> None:
    """events.mode=ws（websockets 进程内桩）：无会话语义画像（events 即推全量）→WS 事件归一→finish。"""
    import asyncio
    import sys

    from websockets.asyncio.server import serve

    frames = [
        json.dumps({"type": "message.part.updated", "delta": "WS "}),
        json.dumps({"type": "message.part.updated", "delta": "事件流"}),
        json.dumps({"type": "run.finished", "usage": {"total_tokens": 9}}),
    ]

    async def ws_handler(websocket: Any) -> None:
        for frame in frames:
            await websocket.send(frame)

    async def scenario() -> list[Any]:
        async with serve(ws_handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            profile = custom_rest_profile()
            transport = profile.transport.model_copy(deep=True)
            transport.endpoints = {"events": EndpointSpec(method="GET", path="/ws", type="ws")}  # 仅事件面
            profile = profile.model_copy(deep=True, update={"transport": transport})
            adapter = HttpServiceAdapter(
                profile,
                base_url=f"http://127.0.0.1:{port}",
                api_key="k",
                session_mapper=InMemoryAdapterSessionMapper(),
            )
            try:
                return [e async for e in adapter.stream_chat(make_turn(), make_ctx())]
            finally:
                await adapter.aclose()

    if sys.platform == "win32":  # 本地服务桩的 win32 循环口径（同 CLI 桩：自建 Proactor）
        loop = asyncio.ProactorEventLoop()
        try:
            events = loop.run_until_complete(scenario())
        finally:
            loop.close()
            asyncio.set_event_loop(None)
    else:
        events = asyncio.run(scenario())

    assert "".join(e.delta for e in events if e.kind == "text_delta") == "WS 事件流"
    assert events[-1].kind == "finish" and events[-1].usage == {"total_tokens": 9}


async def test_custom_rest_探活_200_健康_不可达_False() -> None:
    """probe()：200→True；连接失败→False（探活失败 N 次→degraded 记账走 agent_health 既有链）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":  # base_url 含 /v1 前缀 → 探活 URL=/v1/models
            return httpx.Response(200, json={"data": []})
        raise httpx.ConnectError("refused")

    profile = openai_profile()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ok_adapter = HttpServiceAdapter(profile, api_key="k", client=client)
    assert await ok_adapter.probe() is True

    def fail_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    fail_client = httpx.AsyncClient(transport=httpx.MockTransport(fail_handler))
    fail_adapter = HttpServiceAdapter(profile, api_key="k", client=fail_client)
    assert await fail_adapter.probe() is False
    await ok_adapter.aclose()
    await fail_adapter.aclose()


async def test_custom_rest_permission_应答路径模板替换() -> None:
    """permission 端点：{sid}/{pid} 模板替换 + 决策体（审批中心接线随 B5 批，本批翻译面）。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = HttpServiceAdapter(custom_rest_profile(), api_key="k", client=client)
    await adapter.respond_permission("svc-123", "perm-9", "allow_once")
    await adapter.aclose()
    assert captured["path"] == "/session/svc-123/permissions/perm-9"
    assert captured["body"] == {"decision": "allow_once"}


async def test_custom_rest_无_events端点_结构化报_5002() -> None:
    """画像不完整（无 events 端点）→ 调用期结构化拒绝，不静默空转。"""
    profile = custom_rest_profile()
    transport = profile.transport.model_copy(deep=True)
    transport.endpoints = {k: v for k, v in transport.endpoints.items() if k != "events"}
    profile = profile.model_copy(deep=True, update={"transport": transport})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sessionID": "svc-1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = HttpServiceAdapter(profile, api_key="k", client=client)
    with pytest.raises(ModelUnavailableError):
        _ = [e async for e in adapter.stream_chat(make_turn(), make_ctx())]
    await adapter.aclose()


def test_适配器注册面_可进内核分发器() -> None:
    """AgentSlot 契约（02 §4.2）：meta 语义标注齐备，register_agent_slot 拒绝即不合规。"""
    from services.agent.business.kernel.dispatcher import ExtensionDispatcher

    dispatcher = ExtensionDispatcher()
    dispatcher.register_agent_slot(HttpServiceAdapter(openai_profile(), api_key="k"))
    dispatcher.register_agent_slot(HttpServiceAdapter(custom_rest_profile(), api_key="k"))
    assert dispatcher.agent_slot("chat.http_generic") is not None

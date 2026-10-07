# tests/infra/test_llm.py
"""OllamaClient 单测：httpx MockTransport 注入，不发真实请求。"""

import httpx
import pytest

from services.platform.llm.ollama_json import LlmError, OllamaClient


def _client(handler) -> OllamaClient:
    transport = httpx.MockTransport(handler)
    return OllamaClient(base_url="http://test", timeout=5.0, transport=transport)


async def test_chat_json_returns_parsed_content():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        body = request.read()
        assert b"json" in body
        assert b"qwen3:8b" in body
        assert "抽取记忆".encode() in body
        return httpx.Response(200, json={"message": {"content": '{"kind": "mem:Goal"}'}})

    client = _client(handler)
    out = await client.chat_json(model="qwen3:8b", system="抽取记忆", user="文本")
    assert out == {"kind": "mem:Goal"}


async def test_chat_json_bad_json_raises():
    client = _client(lambda req: httpx.Response(200, json={"message": {"content": "not-json"}}))
    with pytest.raises(LlmError):
        await client.chat_json(model="m", system="s", user="u")


async def test_chat_json_http_error_raises():
    client = _client(lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(LlmError):
        await client.chat_json(model="m", system="s", user="u")


async def test_chat_json_non_dict_body_raises():
    client = _client(lambda req: httpx.Response(200, json=[1, 2]))
    with pytest.raises(LlmError):
        await client.chat_json(model="m", system="s", user="u")


async def test_chat_json_missing_key_raises():
    client = _client(lambda req: httpx.Response(200, json={"error": "x"}))
    with pytest.raises(LlmError):
        await client.chat_json(model="m", system="s", user="u")

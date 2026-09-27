"""LLM 薄封装（Ollama /api/chat JSON 模式；M4 计划 1+2 落位于本包——融合不新建 services/platform/llm.py）。

与包内 OpenAICompatibleModelPort（gateway.py，OpenAI 兼容 /chat/completions + schema 校验 +
审计/预算装饰）并存：本模块服务沉淀管线的 chat_json 窄协议（终审绿行为原样保留；Ollama 原生
端点，无需 api_key）。接口按 LiteLLM 形态设计（model + messages 语义），plan3 切 LiteLLM
SDK 时两实现一并收敛，只换实现不动调用方。
"""

from __future__ import annotations

import json
from typing import Protocol

import httpx

from services.platform.config import Settings, get_settings


class LlmError(Exception):
    """LLM 调用失败（网络/HTTP/输出非 JSON）。"""


class LlmClient(Protocol):
    async def chat_json(self, *, model: str, system: str, user: str) -> dict[str, object]: ...


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)

    async def chat_json(self, *, model: str, system: str, user: str) -> dict[str, object]:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": "json",
            "stream": False,
        }
        try:
            resp = await self._client.post("/api/chat", json=payload)
            resp.raise_for_status()
            body = resp.json()
            content = body["message"]["content"] if isinstance(body, dict) else None
            parsed = json.loads(content) if isinstance(content, str) else content
            if not isinstance(parsed, dict):
                raise LlmError("llm output is not a JSON object")
            return parsed
        except (httpx.HTTPError, KeyError, TypeError, json.JSONDecodeError) as e:
            raise LlmError(f"ollama chat_json failed: {e}") from e

    async def aclose(self) -> None:
        """关闭底层 httpx 客户端（ARQ on_shutdown / 进程收尾）。"""
        await self._client.aclose()


def get_llm_client(settings: Settings | None = None) -> LlmClient:
    s = settings or get_settings()
    return OllamaClient(base_url=s.ollama_base_url, timeout=s.llm_timeout_seconds)

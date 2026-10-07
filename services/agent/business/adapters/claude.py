"""claude 适配器：Anthropic Messages API 直连通道（锚点 01 §3.3 保留通道，吃 prompt caching）。

- httpx.AsyncClient 异步流式（SSE 行解析），timeout 必设（standards/01 §2.5）；
  read 超时按 TTFT 口径钳制（docs/Agent §5：流式超时按首 token 判定）；
- prompt caching：system 块带 ``cache_control: ephemeral``（记忆+证据前缀跨轮复用）；
- 无 API key：**注册成功**（组合根恒装配），调用时抛 5002 LLM_UNAVAILABLE（已登记码，
  ModelUnavailableError）；审计/预算挂接随 M4 直连通道收口（builtin 走 AuditedModelPort
  先行，本通道为文档明文的保留例外）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from services.agent.business.adapters.base import CHAT_ACTION_IRI, ChatAdapter, ChatTurn, GenerationEvent
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.platform.ports.model_port import ModelTimeoutError, ModelUnavailableError

_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_BASE_URL = "https://api.anthropic.com"
_MAX_TOKENS = 2048  # 单轮回答上限（示例值；随 agent 级配置收口）
_CONNECT_TIMEOUT_S = 10.0


def _build_system_block(turn: ChatTurn) -> list[dict[str, Any]]:
    """system 块（带 cache_control）：长前缀命中缓存——锚点 §3.3 直连通道的存在理由。

    技能目录段（竖线② L1）为进程级稳定前缀，置于 context_text 之前——轮间易变尾只在
    证据段，cache_control 前缀命中不受影响。
    """
    text = "你是 ontology-agent 平台对话助手：仅依据给定的记忆与知识证据回答，证据不足时明确说明。"
    if turn.skills_catalog:
        text = f"{text}\n{turn.skills_catalog}"
    text = f"{text}\n{turn.context_text}"
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _build_messages(turn: ChatTurn) -> list[dict[str, Any]]:
    """近窗历史（新→旧反转成时序）+ 本条消息。"""
    messages = [{"role": role, "content": content} for role, content in reversed(turn.history)]
    messages.append({"role": "user", "content": turn.message})
    return messages


class ClaudeAdapter(ChatAdapter):
    """claude：SDK 直连通道的 httpx 形态（Agent 服务设计 §3.2 M3 双适配器之一）。"""

    meta = ExtensionMeta(
        name="chat.claude",
        version="1.0.0",
        semantic_annotation={"action_iri": CHAT_ACTION_IRI},
    )
    adapter_name = "claude"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-5",
        base_url: str = _DEFAULT_BASE_URL,
        timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        # 无 key 仍可构造（注册成功）；客户端惰性创建，避免占位适配器持有连接池
        self._client = client

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"anthropic-version": _ANTHROPIC_VERSION}
            if self._api_key:
                headers["x-api-key"] = self._api_key
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=_CONNECT_TIMEOUT_S, read=self._timeout_s, write=30.0, pool=10.0),
                headers=headers,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def stream_chat(
        self, turn: ChatTurn, ctx: TenantContext, *, timeout_ms: int = 30_000
    ) -> AsyncIterator[GenerationEvent]:
        if not self._api_key:
            raise ModelUnavailableError("claude 直连通道未配置 ANTHROPIC API key（5002，注册成功调用拒绝）")
        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": _MAX_TOKENS,
            "stream": True,
            "system": _build_system_block(turn),
            "messages": _build_messages(turn),
        }
        client = self._ensure_client()
        usage: dict[str, Any] = {}
        finish_reason = "stop"
        try:
            # 硬上限由内核单工具超时（asyncio.wait_for）钳制；传输层超时=构造期 httpx.Timeout
            async with client.stream("POST", f"{self._base_url}/v1/messages", json=body) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", errors="replace")[:200]
                    raise ModelUnavailableError(f"Anthropic 返回 {resp.status_code}: {detail}")
                async for generated in self._iter_events(resp, usage):
                    if generated.kind != "finish":
                        yield generated
                    else:
                        usage = generated.usage or usage
                        finish_reason = generated.finish_reason or finish_reason
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError(f"Anthropic 流式响应超时: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(f"Anthropic 不可达（{self._base_url}）: {exc}") from exc
        yield GenerationEvent(kind="finish", usage=usage, finish_reason=finish_reason)

    async def _iter_events(self, resp: httpx.Response, usage: dict[str, Any]) -> AsyncIterator[GenerationEvent]:
        """Anthropic SSE 行解析：content_block_delta 取文本，message_* 取用量与停止原因。"""
        finish_reason: str | None = None
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            try:
                payload = json.loads(line[len("data:") :].strip())
            except ValueError:
                continue
            kind = payload.get("type")
            if kind == "content_block_delta":
                delta = payload.get("delta") or {}
                if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                    yield GenerationEvent(kind="text_delta", delta=delta["text"])
            elif kind == "message_start":
                message_usage = (payload.get("message") or {}).get("usage") or {}
                usage["token_in"] = int(message_usage.get("input_tokens") or 0)
                cache_read = message_usage.get("cache_read_input_tokens")
                if isinstance(cache_read, int):
                    usage["cache_read_tokens"] = cache_read
            elif kind == "message_delta":
                message_usage = payload.get("usage") or {}
                usage["token_out"] = int(message_usage.get("output_tokens") or 0)
                stop_reason = (payload.get("delta") or {}).get("stop_reason")
                finish_reason = "stop" if stop_reason in (None, "end_turn") else str(stop_reason)
            elif kind == "message_stop":
                yield GenerationEvent(kind="finish", usage=dict(usage), finish_reason=finish_reason)
                return
        yield GenerationEvent(kind="finish", usage=dict(usage), finish_reason=finish_reason)

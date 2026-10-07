"""F3 常驻服务通用适配器（docs/Agent/05 §2.1/§4.1/§5.2 + 20 篇 §3.1）。

三协议模式（profile.protocol）：
- **openai-chat**（企业 agent 通用模板——任何 OpenAI 兼容端点零代码接入）：POST
  ``{base}/chat/completions`` + SSE ``choices[0].delta`` 归一（gateway.py 同口径：``data:`` 行、
  ``[DONE]`` 终止、keep-alive 跳过、usage 末块提取 token_in/token_out/cache_read）；
  nanobot 口径会话复用：``transport.session_body_field``（如 ``session_id``）注入请求体，
  映射经 adapter_sessions（session_map.py）登记——首轮确定性派生（platform session id 直用），
  进程重启不丢映射；
- **custom-rest**（服务画像自画像）：``transport.endpoints{new_session,prompt,events,
  permission}`` 驱动；events.mode=sse（``data:`` 行 + event_map 归一）|ws（websockets 懒加载）；
  会话映射经 new_session 响应 ``session_map.tool_session_field`` 路径提取；
- 鉴权（20 篇 §3 落地项 ③）：``transport.auth``——bearer=Authorization 头；apikey=自定义头。

超时口径与 claude 直连通道一致：内核单工具超时（asyncio.wait_for）为硬上限，本适配器传输层
超时=构造期 httpx.Timeout（read 按 TTFT 判定）；失败一律结构化 ModelPortError 族（5001/5002），
不裸异常逃逸（02 §4.1 ④）。探活 :meth:`probe` 供 health-check 端点接线——连续失败 N 次→
degraded（既有裁决，agent_health.py 驱动，本批提供探针面）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import httpx

from services.agent.business.adapters.base import CHAT_ACTION_IRI, ChatAdapter, ChatTurn, GenerationEvent
from services.agent.business.adapters.profiles import AgentProfile, is_missing, resolve_json_path
from services.agent.business.adapters.session_map import AdapterSessionMapper, InMemoryAdapterSessionMapper
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.platform.ports.model_port import ModelTimeoutError, ModelUnavailableError

_CONNECT_TIMEOUT_S = 10.0


def _chronological_history(turn: ChatTurn) -> list[dict[str, str]]:
    """近窗历史（新→旧）反转成时序（builtin/claude 同口径）。"""
    return [{"role": role, "content": content} for role, content in reversed(turn.history)]


def _ws_scheme(base_url: str) -> str:
    """http(s) → ws(s)（custom-rest events.mode=ws 的 URL 推导）。"""
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}"


class HttpServiceAdapter(ChatAdapter):
    """http-generic：F3 常驻服务画像驱动的通用生成通道（profile 数据文件，非代码）。"""

    meta = ExtensionMeta(
        name="chat.http_generic",
        version="1.0.0",
        semantic_annotation={"action_iri": CHAT_ACTION_IRI},
    )
    adapter_name = "http-generic"

    def __init__(
        self,
        profile: AgentProfile,
        *,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
        session_mapper: AdapterSessionMapper | None = None,
    ) -> None:
        self._profile = profile
        transport = profile.transport
        self._base_url = (base_url or transport.base_url).rstrip("/")
        self._model = model or transport.model
        self._timeout_s = timeout_s
        self._client = client
        self._session_mapper = session_mapper or InMemoryAdapterSessionMapper()
        # 鉴权（20 篇 §3 ③）：显式 api_key 优先；否则 profile.api_key_env 环境变量（值不落盘）
        self._auth_header = self._resolve_auth_header(transport.auth, api_key)
        self._body_session_field = transport.session_body_field
        self._tool_session_field = str(profile.session_map.get("tool_session_field", ""))
        self._session_binding: str | None = None  # 进程内缓存（登记映射后的对端 id）

    @staticmethod
    def _resolve_auth_header(auth: Any, api_key: str) -> dict[str, str]:
        import os

        key = api_key or (os.environ.get(auth.api_key_env, "") if auth.api_key_env else "")
        if not key or auth.type == "none":
            return {}
        if auth.type == "bearer":
            return {"Authorization": f"Bearer {key}"}
        if auth.type == "apikey":
            return {auth.header: key}
        return {}

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=_CONNECT_TIMEOUT_S, read=self._timeout_s, write=30.0, pool=10.0),
                headers=self._auth_header,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── 探活面（探活失败 N 次→degraded 既有裁决；agent_health 记账驱动）────────
    async def probe(self) -> bool:
        """GET ``{base}{health_path}``（profile.transport.health_path；空=不可探活按失败口径）。"""
        path = self._profile.transport.health_path
        if not path:
            return False
        try:
            resp = await self._ensure_client().get(f"{self._base_url}{path}", headers=self._auth_header or None)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    # ── 生成通道（ChatAdapter 契约）──────────────────────────────────────────
    async def stream_chat(
        self, turn: ChatTurn, ctx: TenantContext, *, timeout_ms: int = 30_000
    ) -> AsyncIterator[GenerationEvent]:
        if not self._base_url:
            raise ModelUnavailableError("http-generic profile 未配置 base_url（5002，部署期注入缺失）")
        if self._profile.protocol == "openai-chat":
            async for event in self._stream_openai_chat(turn, ctx, timeout_ms=timeout_ms):
                yield event
            return
        async for event in self._stream_custom_rest(turn, ctx, timeout_ms=timeout_ms):
            yield event

    # ── openai-chat（企业 agent 通用模板）────────────────────────────────────
    async def _openai_session_id(self, turn: ChatTurn) -> str | None:
        """会话复用（nanobot 口径）：映射表取→确定性派生（platform session id 直用）→登记。"""
        if not self._body_session_field:
            return None
        if self._session_binding is None:
            foreign = await self._session_mapper.get(tenant_id=turn.tenant_id, session_id=turn.session_id)
            if foreign is None:
                foreign = str(turn.session_id)  # 确定性派生：重启不丢映射（无随机性）
                await self._session_mapper.put(
                    tenant_id=turn.tenant_id,
                    session_id=turn.session_id,
                    foreign_id=foreign,
                    metadata={"adapter": "http-generic"},
                )
            self._session_binding = foreign
        return self._session_binding

    async def _stream_openai_chat(
        self, turn: ChatTurn, ctx: TenantContext, *, timeout_ms: int
    ) -> AsyncIterator[GenerationEvent]:
        messages = _chronological_history(turn)
        messages.append({"role": "user", "content": turn.message})
        body: dict[str, Any] = {"model": self._model, "stream": True, "messages": messages}
        session_id = await self._openai_session_id(turn)
        if session_id is not None:
            body[self._body_session_field] = session_id
        usage: dict[str, Any] = {}
        finish_reason = "stop"
        client = self._ensure_client()
        try:
            # 鉴权头随请求注入（而非客户端默认头——注入客户端/自建客户端同口径，测试可注 MockTransport）
            async with client.stream(
                "POST", f"{self._base_url}/chat/completions", json=body, headers=self._auth_header or None
            ) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", errors="replace")[:200]
                    raise ModelUnavailableError(f"openai-chat 端点返回 {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue  # 非 data 行（event:/keep-alive）跳过不断流
                    data_text = line[len("data:") :].strip()
                    if data_text == "[DONE]":
                        break
                    try:
                        payload = json.loads(data_text)
                    except ValueError:
                        continue  # 非 JSON 数据行容错
                    if isinstance(payload.get("usage"), dict):
                        usage = _openai_usage(payload["usage"])
                    try:
                        delta = payload["choices"][0]["delta"]
                    except (KeyError, IndexError, TypeError):
                        continue  # role 首块 / finish 块 / usage-only 末块均无 delta
                    if not isinstance(delta, dict):
                        continue
                    content = delta.get("content")
                    reasoning = delta.get("reasoning_content")  # vLLM/DeepSeek 推理增量
                    if isinstance(reasoning, str) and reasoning:
                        yield GenerationEvent(kind="reasoning_delta", delta=reasoning)
                    if isinstance(content, str) and content:
                        yield GenerationEvent(kind="text_delta", delta=content)
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError(f"openai-chat 流式响应超时: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(f"openai-chat 端点不可达（{self._base_url}）: {exc}") from exc
        yield GenerationEvent(kind="finish", usage=usage, finish_reason=finish_reason)

    # ── custom-rest（服务画像自画像）────────────────────────────────────────
    async def _tool_session(self, turn: ChatTurn) -> str | None:
        """custom-rest 会话映射：缓存→映射表→new_session 端点建（无该端点=无会话语义 None）。"""
        if self._session_binding is not None:
            return self._session_binding
        foreign = await self._session_mapper.get(tenant_id=turn.tenant_id, session_id=turn.session_id)
        if foreign is None:
            spec = self._profile.transport.endpoints.get("new_session")
            if spec is None or not spec.path:
                return None
            resp = await self._ensure_client().request(
                spec.method, f"{self._base_url}{spec.path}", json={}, headers=self._auth_header or None
            )
            if resp.status_code != 200:
                raise ModelUnavailableError(f"new_session 返回 {resp.status_code}: {resp.text[:200]}")
            foreign = _extract_session_id(resp, self._tool_session_field)
            if foreign is None:
                raise ModelUnavailableError(
                    f"new_session 响应缺会话字段（session_map.tool_session_field={self._tool_session_field}）"
                )
            await self._session_mapper.put(
                tenant_id=turn.tenant_id,
                session_id=turn.session_id,
                foreign_id=foreign,
                metadata={"adapter": "http-generic"},
            )
        self._session_binding = foreign
        return foreign

    async def _stream_custom_rest(
        self, turn: ChatTurn, ctx: TenantContext, *, timeout_ms: int
    ) -> AsyncIterator[GenerationEvent]:
        session_id = await self._tool_session(turn)
        await self._post_prompt(turn, session_id)
        events = self._profile.transport.endpoints.get("events")
        if events is None or not events.path:
            # 无事件流=同步问答面：prompt 响应即答案（尾部聚合面本批不展开，fail-closed 明示）
            raise ModelUnavailableError("custom-rest profile 未声明 events 端点（5002，画像不完整）")
        path = events.path.replace("{sid}", session_id or "")
        collected_usage: dict[str, Any] = {}
        finish_reason = "stop"
        if events.type == "ws":
            async for kind, delta, payload in self._iter_ws_events(path):
                if kind == "finish":
                    collected_usage = payload if isinstance(payload, dict) else {}
                    break
                if delta:
                    yield GenerationEvent(kind=kind, delta=delta)
        else:
            async for kind, delta, payload in self._iter_sse_events(path, timeout_ms=timeout_ms):
                if kind == "finish":
                    collected_usage = payload if isinstance(payload, dict) else {}
                    break
                if delta:
                    yield GenerationEvent(kind=kind, delta=delta)
        yield GenerationEvent(kind="finish", usage=collected_usage, finish_reason=finish_reason)

    async def _post_prompt(self, turn: ChatTurn, session_id: str | None) -> None:
        """prompt 端点：本条消息+近窗历史投递（事件经 events 流回传；响应体不承载增量）。"""
        spec = self._profile.transport.endpoints.get("prompt")
        if spec is None or not spec.path:
            return  # 无 prompt 端点=events 即推全量（画像允许），直连事件流
        path = spec.path.replace("{sid}", session_id or "")
        body: dict[str, Any] = {"message": turn.message, "history": _chronological_history(turn)}
        try:
            resp = await self._ensure_client().request(
                spec.method, f"{self._base_url}{path}", json=body, headers=self._auth_header or None
            )
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError(f"custom-rest prompt 超时: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(f"custom-rest prompt 不可达（{self._base_url}）: {exc}") from exc
        if resp.status_code != 200:
            raise ModelUnavailableError(f"prompt 返回 {resp.status_code}: {resp.text[:200]}")

    def _map_source_event(self, payload: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        """event_map 归一：kind 判别路径 → 规则匹配 → (GenerationEvent.kind, delta, usage)。

        缺 kind/无规则命中 → ("", "", {})（逐条容错跳过，不断流——20 篇 §3.4 缺字段容错）。
        """
        kind = resolve_json_path(payload, self._profile.event_map.kind_path)
        if is_missing(kind) or not isinstance(kind, str):
            return "", "", {}
        for rule in self._profile.event_map.rules:
            if rule.match != kind:
                continue
            if rule.emit == "finish":
                usage = resolve_json_path(payload, rule.usage_path)
                return "finish", "", usage if isinstance(usage, dict) else {}
            delta = resolve_json_path(payload, rule.delta_path)
            return rule.emit, (delta if isinstance(delta, str) else ""), {}
        return "", "", {}

    async def _iter_sse_events(
        self, path: str, *, timeout_ms: int
    ) -> AsyncIterator[tuple[str, str, dict[str, Any]]]:
        """events SSE（``data:`` 行 → JSON → event_map 归一；事件流断开=本轮终止）。"""
        client = self._ensure_client()
        try:
            async with client.stream("GET", f"{self._base_url}{path}", headers=self._auth_header or None) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", errors="replace")[:200]
                    raise ModelUnavailableError(f"events SSE 返回 {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_text = line[len("data:") :].strip()
                    if data_text == "[DONE]":
                        return
                    try:
                        payload = json.loads(data_text)
                    except ValueError:
                        continue
                    if isinstance(payload, dict):
                        yield self._map_source_event(payload)
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError(f"events SSE 流超时: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(f"events SSE 不可达（{self._base_url}）: {exc}") from exc

    async def _iter_ws_events(self, path: str) -> AsyncIterator[tuple[str, str, dict[str, Any]]]:
        """events WS（websockets 懒加载；连接断开=本轮终止）。"""
        try:
            import websockets  # noqa: PLC0415  懒加载（uvicorn[standard] 传递依赖）
        except ImportError as exc:  # pragma: no cover — 环境缺 websockets 时结构化报错
            raise ModelUnavailableError("events.mode=ws 需 websockets 库（未安装）") from exc
        url = f"{_ws_scheme(self._base_url)}{path}"
        try:
            async with websockets.connect(url) as ws:
                async for raw in ws:
                    try:
                        payload = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(payload, dict):
                        yield self._map_source_event(payload)
        except (OSError, websockets.WebSocketException) as exc:
            raise ModelUnavailableError(f"events WS 不可达（{url}）: {exc}") from exc

    # ── 审批应答面（permission 端点；审批中心接线随 B5 批，本批提供翻译面）──────
    async def respond_permission(self, session_id: str, request_id: str, decision: str) -> None:
        """permission 端点应答：``{sid}``/``{pid}`` 路径模板替换 + permission_map 决策透传。"""
        spec = self._profile.transport.endpoints.get("permission")
        if spec is None or not spec.path:
            raise ModelUnavailableError("custom-rest profile 未声明 permission 端点（画像无审批面）")
        path = spec.path.replace("{sid}", session_id).replace("{pid}", request_id)
        resp = await self._ensure_client().request(
            spec.method, f"{self._base_url}{path}", json={"decision": decision}
        )
        if resp.status_code != 200:
            raise ModelUnavailableError(f"permission 应答返回 {resp.status_code}")


def _openai_usage(usage: dict[str, Any]) -> dict[str, Any]:
    """OpenAI 兼容用量归一（gateway._capture_usage 双兼容口径：DeepSeek 缓存键优先）。"""
    normalized: dict[str, Any] = {
        "token_in": usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), int) else 0,
        "token_out": usage.get("completion_tokens") if isinstance(usage.get("completion_tokens"), int) else 0,
    }
    cache_read = usage.get("prompt_cache_hit_tokens")
    if not isinstance(cache_read, int):
        details = usage.get("prompt_tokens_details")
        cache_read = details.get("cached_tokens") if isinstance(details, dict) else None
    normalized["cache_read_tokens"] = cache_read if isinstance(cache_read, int) else 0
    return normalized


def _extract_session_id(resp: httpx.Response, field_path: str) -> str | None:
    """new_session 响应提取对端会话 id（session_map.tool_session_field 字段路径）。"""
    try:
        payload: Any = resp.json()
    except ValueError:
        return None
    value = resolve_json_path(payload, f"$.{field_path}" if field_path else "$")
    if is_missing(value) or not isinstance(value, str) or not value:
        return None
    return value


def build_http_service_adapter(
    profile: AgentProfile,
    *,
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    session_mapper: AdapterSessionMapper | None = None,
) -> HttpServiceAdapter:
    """装配面（组合根消费；audit 包裹面随桥审计批——本批探活/生成面先行）。"""
    return HttpServiceAdapter(
        profile,
        base_url=base_url,
        api_key=api_key,
        model=model,
        session_mapper=session_mapper,
    )

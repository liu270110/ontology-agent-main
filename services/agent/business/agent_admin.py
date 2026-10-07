"""agents 管理面扩展用例（api/01 §5.1 ★ 预登记四端点编排层；契约源=mock platform-handlers.ts §5.1）。

- GET /agents/adapter-schemas：适配器 config schema 下发（RJSF 渲染源）。内容=pydantic 常量
  （键集与领域 ``_CONFIG_KEYS`` 同源——model/temperature/tool_whitelist/num_ctx，平台不另设
  第二份词表）；枚举源=agent_adapters 表行（按 agent_tool 去重），无库表数据回落
  ALLOWED_AGENT_TOOLS 常量（装机空态）；
- POST /agents/connection-test：预注册连接测试（先测后注册，mcp discover 同构）——对目标发
  一次最小补全（platform/llm OpenAICompatibleModelPort，单渠道 OpenAI 兼容口径同组合根
  _build_model_port）；失败结构化 200 {ok:false,error} 不上 500；传输经
  ``app.state.llm_probe_factory`` 注入（组合根可选装配位，缺省真传输；测试注桩零真网）；
- POST /agents/{id}/enable|disable：agents.status 翻转——聚合方法唯一写路径（03 §6.1）；
  幂等=已在目标状态零操作（不触发状态机，重复调用同形返回）。**disabled 语义=新会话拒绑**
  （Agent.ensure_usable_for_new_session，创建会话端点强制）——不级联改 sessions（存量会话/
  Run 跑完不中断，04 §10 degraded 同裁决），terminated_sessions 恒 0（前端字段占位）；
- POST /agents/{id}/debug-chat：调试面单轮生成——复用编排器最小生成面（ChatAdapter.stream_chat，
  与 chat 主链同一 builtin/claude 通道与超时口径），**不落 sessions/messages/tasks 行**
  （调试对话不计正式历史，IX-AGT-02；审计仍由网关 AuditLogMiddleware 承担）。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from services.agent.business.adapters.base import ChatTurn, GenerationEvent
from services.agent.business.adapters.claude import ClaudeAdapter
from services.agent.domain.model.agent import ALLOWED_AGENT_TOOLS, Agent, AgentAdapterInfo, AgentStatus
from services.agent.domain.model.kernel_context import TenantContext
from services.platform.errors import trace_id_ctx
from services.platform.ports.model_port import ModelPort

# ---------------------------------------------------------------- adapter-schemas（pydantic 常量 schema）


class _BuiltinConfigSchema(BaseModel):
    """builtin 适配器 config 面板（键集=领域 _CONFIG_KEYS；标题/描述供 RJSF 渲染）。"""

    model: str = Field(default="", title="模型别名 model", description="平台模型网关内的模型别名（缺省=平台缺省模型）")
    temperature: float = Field(default=0.7, title="采样温度 temperature", description="0~2；对话档建议 0.7")
    tool_whitelist: list[str] = Field(
        default_factory=list, title="工具白名单 tool_whitelist", description="该 agent 可绑定的工具清单"
    )
    num_ctx: int = Field(default=0, title="上下文窗口 num_ctx", description="Ollama/vLLM 语义窗口；不支持时忽略")


class _ClaudeConfigSchema(BaseModel):
    """claude 适配器 config 面板（键集同上；claude 直连通道语义描述）。"""

    model: str = Field(default="claude-sonnet-4-5", title="模型 model", description="Anthropic 模型名（直连通道）")
    temperature: float = Field(default=0.7, title="采样温度 temperature", description="0~2；对话档建议 0.7")
    tool_whitelist: list[str] = Field(
        default_factory=list, title="工具白名单 tool_whitelist", description="该 agent 可绑定的工具清单"
    )
    num_ctx: int = Field(default=0, title="上下文窗口 num_ctx", description="上下文预算上限；端点不支持时忽略")


class _AcpConfigSchema(BaseModel):
    """acp 适配器 config 面板（G1 批，20 篇 §2；acp_profile=adapters/profiles/acp/<名>.yaml）。"""

    acp_profile: str = Field(
        default="opencode",
        title="ACP 画像 acp_profile",
        description="profiles/acp 下的画像名（opencode-acp/goose-acp 所在文件名；缺省回落平台配置）",
    )
    tool_whitelist: list[str] = Field(
        default_factory=list, title="工具白名单 tool_whitelist", description="该 agent 可绑定的工具清单"
    )


class AdapterSchemaEntry(BaseModel):
    """adapter-schemas 单项（mock ADAPTER_SCHEMAS 逐字段：key/name/vendor/capability/schema）。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    key: str
    name: str
    vendor: str
    capability: str
    config_schema: dict[str, Any] = Field(default_factory=dict, alias="schema")  # JSON Schema（FastAPI 按别名序列化）


_ADAPTER_REGISTRY: dict[str, AdapterSchemaEntry] = {
    "builtin": AdapterSchemaEntry(
        key="builtin",
        name="builtin（内置 harness）",
        vendor="平台内置",
        capability="ModelPort 直驱 · 真流式 · 平台审计/预算内建",
        schema=_BuiltinConfigSchema.model_json_schema(by_alias=True),
    ),
    "claude": AdapterSchemaEntry(
        key="claude",
        name="claude（Anthropic 直连）",
        vendor="Anthropic · 保留通道",
        capability="Messages API 直连 · prompt caching · SSE 流式",
        schema=_ClaudeConfigSchema.model_json_schema(by_alias=True),
    ),
    # G1（20 篇 §2）：F4 ACP 通用适配器——一个适配器吃全部 ACP 系（opencode/goose/hermes-acp…）
    "acp": AdapterSchemaEntry(
        key="acp",
        name="acp（ACP 标准协议）",
        vendor="Bridge · 通用形态 F4",
        capability="stdio JSON-RPC · profile 驱动 · 权限桥接审批中心",
        schema=_AcpConfigSchema.model_json_schema(by_alias=True),
    ),
}


def adapter_schema_items(adapters: list[AgentAdapterInfo]) -> list[AdapterSchemaEntry]:
    """枚举=agent_adapters 表行（按 agent_tool 去重保序）；空表回落 ALLOWED_AGENT_TOOLS 常量。

    未登记 agent_tool 行（防御：表行只能由 ensure_platform_adapter 建，枚举受限）给空 schema
    兜底项，不静默隐藏。
    """
    tools = list(dict.fromkeys(a.agent_tool for a in adapters)) or list(ALLOWED_AGENT_TOOLS)
    return [
        _ADAPTER_REGISTRY.get(tool)
        or AdapterSchemaEntry(key=tool, name=tool, vendor="未登记", capability="", config_schema={})
        for tool in tools
    ]


# ---------------------------------------------------------------- connection-test（预注册连接测试）

# 探测口径（最小补全）：1 token 足以验证「可达+鉴权+模型在位」整链；超时取窄值（先测后注册
# 面向交互式向导，久等无意义）；api_key 缺省传占位（本地 vLLM 无密钥，组合根 _LLM_KEY_PLACEHOLDER 同口径）。
_PROBE_TIMEOUT_S = 5.0
_PROBE_MAX_TOKENS = 1
_PROBE_KEY_PLACEHOLDER = "EMPTY"

# 传输工厂：*(base_url, api_key, model) → 全文（缺省=OpenAICompatibleModelPort 真传输；
# app.state.llm_probe_factory 注入点，测试注桩零真网——mcp_client_factory 同款纪律）。
ProbeFn = Callable[..., Awaitable[str]]

_ERROR_MAX_CHARS = 500  # 结构化 error 截断（上游报文可能携带响应体全文）


async def _default_probe(*, base_url: str, api_key: str, model: str) -> str:
    """缺省真传输：OpenAI 兼容 /chat/completions 一次最小补全（L7 惰性绑定，组合根同款 DIP）。"""
    from services.platform.llm.gateway import OpenAICompatibleModelPort  # noqa: PLC0415  L7 惰性绑定

    port = OpenAICompatibleModelPort(base_url=base_url, api_key=api_key, model=model, timeout_s=_PROBE_TIMEOUT_S)
    try:
        return await port.complete(
            [{"role": "user", "content": "ping"}],
            max_tokens=_PROBE_MAX_TOKENS,
            timeout_s=_PROBE_TIMEOUT_S,
            trace_id=trace_id_ctx.get(),
        )
    finally:
        await port.aclose()


async def probe_connection(
    *, provider: str, base_url: str, api_key: str | None, model: str, probe: ProbeFn | None = None
) -> dict[str, Any]:
    """预注册连接测试：一次最小补全；任何失败结构化 200 {ok:false, error}（不上 500）。

    provider 当前仅登记不分流（平台唯一 OpenAI 兼容通道，M0 口径；多渠道 fallback 随
    foundation/llm 收口）；返回即 ConnectionTestOut 形状（api 层零再加工）。
    """
    transport = probe or _default_probe
    started = time.monotonic()
    try:
        await transport(base_url=base_url, api_key=api_key or _PROBE_KEY_PLACEHOLDER, model=model)
    except Exception as exc:  # noqa: BLE001  探测失败结构化返回（码前缀 5xxx 随消息透传可追溯）
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "model": model,
            "error": str(exc)[:_ERROR_MAX_CHARS],
        }
    return {"ok": True, "latency_ms": int((time.monotonic() - started) * 1000), "model": model, "error": None}


# ---------------------------------------------------------------- enable/disable（幂等翻转）


def set_agent_enabled(agent: Agent, *, enabled: bool) -> int:
    """幂等状态翻转：已在目标状态零操作（状态机禁同态迁移，重放幂等靠此处短路）。

    返回 terminated_sessions——恒 0：disabled 语义=新会话拒绑（ensure_usable_for_new_session，
    创建会话端点强制），**不强改 sessions**（存量会话/Run 跑完不中断，04 §10 同裁决）；
    字段为前端 mock 形状占位（api.ts stopAgent 读取）。
    """
    if (agent.status is AgentStatus.ENABLED) == enabled:
        return 0
    if enabled:
        agent.enable()
    else:
        agent.disable()
    return 0


# ---------------------------------------------------------------- debug-chat（调试面单轮生成）

_DEBUG_TIMEOUT_MS_DEFAULT = 30_000  # 与 ChatAnswerTool.invoke 缺省一致（chat 主链同口径）
_REPLY_TIMEOUT_MS_MAX = 120_000  # 调试面硬上限（防误配长挂）


async def debug_reply(
    *, agent: Agent, message: str, params: dict[str, Any] | None, model_port: ModelPort | None, user_id: uuid.UUID
) -> dict[str, Any]:
    """调试面单轮生成：编排器最小生成面（ChatAdapter.stream_chat），零持久化。

    - builtin → BuiltinAdapter(model_port)（None=平台未配模型通道，调用 5002 结构化）；
      claude → ClaudeAdapter()（无 key 调用 5002，注册成功调用拒绝同口径）；
    - session_id/run_id 为一次性占位 UUID（ChatTurn 契约必填；调试面无会话语义，同
      base._ChatSubRunRunner 占位先例），不落库；
    - params.timeout_ms 可覆盖单轮超时（其余 params 键预留不消费）。
    返回 DebugChatOut 形状（reply/usage/latency_ms）。
    """
    adapter = _debug_adapter(agent, model_port)
    timeout_ms = _debug_timeout_ms(params)
    turn = ChatTurn(
        tenant_id=agent.tenant_id,
        session_id=uuid.uuid4(),  # 调试占位（不触发任何 L1/PG 回写——本用例不落库）
        run_id=uuid.uuid4(),
        message=message,
        history=(),  # 调试面无近窗历史（单轮）
        system_prompt=agent.system_prompt,
        num_ctx=agent.config.get("num_ctx"),
    )
    ctx = TenantContext(tenant_id=agent.tenant_id, user_id=user_id, scopes=(), trace_id=trace_id_ctx.get() or "")
    started = time.monotonic()
    parts: list[str] = []
    usage: dict[str, Any] = {}
    event: GenerationEvent
    async for event in adapter.stream_chat(turn, ctx, timeout_ms=timeout_ms):
        if event.kind == "text_delta":
            parts.append(event.delta)
        elif event.kind == "finish":
            usage = dict(event.usage)
    return {
        "reply": "".join(parts),
        "usage": usage,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


def _debug_adapter(agent: Agent, model_port: ModelPort | None):
    """按 agent_tool 选调试生成通道（与 chat 主链 build_chat_orchestrator 同构）。"""
    if agent.agent_tool == "builtin":
        from services.agent.business.adapters.builtin import BuiltinAdapter  # noqa: PLC0415  惰性（同组合根）
        from services.platform.ports.model_port import ModelUnavailableError  # noqa: PLC0415

        if model_port is None:
            # 组合根未配 llm_base_url → builtin 不装配（build_chat_orchestrator 同口径）：fail-closed 5002
            raise ModelUnavailableError("平台未配置模型通道（LLM_UNAVAILABLE）")
        return BuiltinAdapter(model_port)
    if agent.agent_tool == "claude":
        return ClaudeAdapter()  # 无 key 注册成功调用拒绝（claude.py 契约）
    if agent.agent_tool == "acp":
        # G1（20 篇 §2）：调试面 acp 通道——profile 缺失=422（注册校验白名单同源）；映射走
        # 内存桩（调试面零持久化，同「不落 sessions/messages/tasks 行」口径）；审批桥未装配
        # →权限请求默认拒绝（fail-closed）。
        from services.agent.business.adapters.acp import (  # noqa: PLC0415
            AcpAdapter,
            AcpProfileNotFoundError,
            InMemoryAdapterSessionStore,
            load_acp_profile,
            resolve_acp_profile_name,
        )
        from services.platform.config import get_settings  # noqa: PLC0415
        from services.platform.errors import GatewayError  # noqa: PLC0415

        settings = get_settings()
        try:
            profile = load_acp_profile(
                resolve_acp_profile_name(agent.config, settings.acp_default_profile),
                profiles_dir=settings.acp_profiles_dir,
            )
        except AcpProfileNotFoundError as exc:
            raise GatewayError(3001, str(exc), status_code=422) from exc
        return AcpAdapter.from_settings(profile, store=InMemoryAdapterSessionStore(), settings=settings)
    from services.platform.errors import GatewayError  # noqa: PLC0415

    raise GatewayError(3001, f"适配器 {agent.agent_tool} 无调试通道（允许 {ALLOWED_AGENT_TOOLS}）", status_code=422)


def _debug_timeout_ms(params: dict[str, Any] | None) -> int:
    """params.timeout_ms 覆盖（正整数；非法值忽略走缺省——调试面参数从宽，值不经采样）。"""
    if not isinstance(params, dict):
        return _DEBUG_TIMEOUT_MS_DEFAULT
    raw = params.get("timeout_ms")
    if isinstance(raw, int) and not isinstance(raw, bool) and 0 < raw <= _REPLY_TIMEOUT_MS_MAX:
        return raw
    return _DEBUG_TIMEOUT_MS_DEFAULT

# tests/agent/test_cap_mcp_bridge.py
"""MCP registry → 内核工具绑定桥测试（竖线①，2026-10-05 批）。

纪律：伪 registry（真实 CapabilityRegistry + 桩 provider，同 tests/mcp/test_registry.py
「纯 stdlib 零 fastmcp」口径）+ duck-typed 连接器桩，零网络零 DB；AAA + 中文命名。
覆盖面（批设计点名）：投影/命名/degraded 剔除与结构化错误/租户上下文透传（registry
list_tools 当前全集语义如实断言，M5 收口位不谎报）/authorize 透传（deny-by-default）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from services.agent.api.sessions import _build_mcp_tool_bindings
from services.agent.business.capabilities.mcp_bridge import (
    MCP_ACTION_IRI_PREFIX,
    McpToolBinding,
    build_mcp_tool_bindings,
)
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.extensions import ToolPort
from services.agent.domain.model.kernel_actions import ToolCall
from services.mcp.errors import McpToolError
from services.mcp.registry import CapabilityRegistry, ExternalInvoker
from services.platform.errors import ErrorCode
from services.platform.ports.capability_provider import (
    KERNEL_LOOP_VERSION,
    CallContext,
    CapabilityDescriptor,
    CapabilityError,
    CapabilityResult,
)
from tests.agent.conftest import make_ctx

# ── 夹具与桩 ─────────────────────────────────────────────────────────────


def _descriptor(
    name: str,
    *,
    description: str = "测试工具",
    scopes: tuple[str, ...] = (),
    schema: dict[str, Any] | None = None,
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        name=name,
        description=description,
        input_schema=schema or {"type": "object"},
        required_scopes=scopes,
    )


class _StubProvider:
    """记录调用并按预设返回/抛出的桩 provider（结构化满足 CapabilityProvider）。"""

    def __init__(
        self,
        namespace: str,
        *,
        result: dict[str, Any] | None = None,
        failure: tuple[int, str] | None = None,
        raise_exc: Exception | None = None,
        descriptors: tuple[CapabilityDescriptor, ...] = (),
    ) -> None:
        self.provider_version = "0.1.0"
        self.supported_loop_versions = [KERNEL_LOOP_VERSION]
        self._namespace = namespace
        self._result = result
        self._failure = failure
        self._raise = raise_exc
        self._descriptors = list(descriptors)
        self.calls: list[tuple[str, dict[str, Any], CallContext]] = []

    def namespace(self) -> str:
        return self._namespace

    def capabilities(self) -> list[CapabilityDescriptor]:
        return list(self._descriptors)

    async def invoke(self, name: str, params: dict[str, Any], ctx: CallContext) -> CapabilityResult:
        self.calls.append((name, params, ctx))
        if self._raise is not None:
            raise self._raise
        if self._failure is not None:
            return CapabilityResult.failure(*self._failure)
        return CapabilityResult.success(dict(self._result or {}))


@dataclass(slots=True)
class _FakeConnectorState:
    degraded: bool = False
    failure_count: int = 0


@dataclass(slots=True)
class _FakeConnector:
    """duck-typed ExternalMcpConnector 桩（桥只消费 state.degraded）。"""

    name: str
    state: _FakeConnectorState = field(default_factory=_FakeConnectorState)


def _platform_registry(
    *,
    scopes: tuple[str, ...] = ("kb:read",),
    result: dict[str, Any] | None = None,
    failure: tuple[int, str] | None = None,
    raise_exc: Exception | None = None,
) -> tuple[CapabilityRegistry, _StubProvider]:
    """单平台工具（knowledge.search）的伪 registry：直接挂桩 provider 便于观测调用面。"""
    stub = _StubProvider(
        "knowledge",
        result=result if result is not None else {"hits": []},
        failure=failure,
        raise_exc=raise_exc,
        descriptors=(_descriptor("knowledge.search", scopes=scopes),),
    )
    registry = CapabilityRegistry()
    registry.register(stub)
    return registry, stub


def _external_registry(*, server: str = "weather", tool: str = "lookup") -> tuple[CapabilityRegistry, list[str]]:
    async def _invoke(tool_full: str, arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:  # pragma: no cover
        return {}

    registry = CapabilityRegistry()
    registered = registry.register_external(
        server,
        [_descriptor(f"{server}.{tool}", description="外部天气查询")],
        ExternalInvoker(server, _invoke),
    )
    return registry, registered


def _make_call(action_iri: str | None = None) -> ToolCall:
    return ToolCall(
        action_iri=action_iri or f"{MCP_ACTION_IRI_PREFIX}knowledge.search",
        parameters={"q": "线路A"},
        param_hash="0" * 64,
    )


# ── 投影与命名 ───────────────────────────────────────────────────────────


def test_投影_平台工具_命名与schema按registry全名映射():
    registry, _stub = _platform_registry()
    bindings = build_mcp_tool_bindings(registry)
    assert len(bindings) == 1
    binding = bindings[0]
    assert binding.name == "knowledge.search"
    assert binding.meta.name == "mcp.knowledge.search"  # 命名对齐设计口径 mcp.{registry 全名}
    assert binding.meta.semantic_annotation["action_iri"] == f"{MCP_ACTION_IRI_PREFIX}knowledge.search"
    assert binding.description == "测试工具"
    assert binding.input_schema == {"type": "object"}
    assert isinstance(binding, ToolPort)  # runtime_checkable：结构化满足内核 tools.bindings 契约


def test_投影_外部工具_保留server前缀命名():
    registry, registered = _external_registry()
    assert registered == ["weather.lookup"]  # registry 外部规则 {server}.{local}
    bindings = build_mcp_tool_bindings(registry)
    assert [b.meta.name for b in bindings] == ["mcp.weather.lookup"]
    assert {b.meta.semantic_annotation["action_iri"] for b in bindings} == {
        f"{MCP_ACTION_IRI_PREFIX}weather.lookup"
    }


def test_投影_绑定过内核注册面_行动类唯一不冲突():
    combined = CapabilityRegistry()
    stub = _StubProvider("knowledge", descriptors=(_descriptor("knowledge.search", scopes=("kb:read",)),))
    combined.register(stub)

    async def _invoke(tool_full: str, arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:  # pragma: no cover
        return {}

    combined.register_external("weather", [_descriptor("weather.lookup")], ExternalInvoker("weather", _invoke))
    bindings = build_mcp_tool_bindings(combined)
    assert len(bindings) == 2
    dispatcher = ExtensionDispatcher()
    for binding in bindings:  # 每工具一 action_iri，注册面全部接受（meta 形状/唯一性内核校验）
        dispatcher.register_tool(binding)
    assert dispatcher.tool_for(f"{MCP_ACTION_IRI_PREFIX}knowledge.search") is not None
    assert dispatcher.tool_for(f"{MCP_ACTION_IRI_PREFIX}weather.lookup") is not None


def test_投影_registry缺位_返回空不阻塞():
    assert build_mcp_tool_bindings(None) == ()


def test_投影_当前注册全集_租户过滤位为M5收口如实不谎报():
    registry, _stub = _platform_registry()
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    # registry 现状：list_tools(tenant_id) 恒返回全集（registry.py docstring，M5 mcp_tools 收口）
    assert len(registry.list_tools(tenant_a)) == len(registry.list_tools(tenant_b)) == 1
    # 桥投影透传该语义（不谎报过滤；租户强制在 invoke PDP + M5 可见性收口）
    assert len(build_mcp_tool_bindings(registry)) == 1


# ── degraded（连续失败 ≥5 熔断）────────────────────────────────────────


def test_投影_熔断server的外部工具被剔除_健康server保留():
    registry, _reg = _external_registry(server="weather")
    degraded = _FakeConnector("weather", state=_FakeConnectorState(degraded=True, failure_count=5))
    assert build_mcp_tool_bindings(registry, connectors={"weather": degraded}) == ()  # 目录剔除
    healthy = _FakeConnector("weather")
    assert len(build_mcp_tool_bindings(registry, connectors={"weather": healthy})) == 1
    assert len(build_mcp_tool_bindings(registry)) == 1  # 未供连接器池=不剔除（invoke 期仍受保护）


async def test_调用_投影后转熔断_结构化5003不裸崩():
    ext_registry, _reg = _external_registry(server="weather")
    connector = _FakeConnector("weather")
    binding = build_mcp_tool_bindings(ext_registry, connectors={"weather": connector})[0]
    connector.state.degraded = True  # 投影后翻转（连续失败累计 ≥5 由连接器置位）
    result = await binding.invoke(
        _make_call(action_iri=binding.meta.semantic_annotation["action_iri"]),
        make_ctx(),
    )
    assert result.ok is False
    assert result.error_code == int(ErrorCode.MCP_TARGET_UNAVAILABLE)
    assert "熔断" in (result.error_message or "")


# ── authorize 透传（PDP deny-by-default）────────────────────────────────


async def test_调用_scope命中_透传tenant_trace_scopes到provider():
    registry, stub = _platform_registry(scopes=("kb:read",))
    binding = build_mcp_tool_bindings(registry)[0]
    ctx = make_ctx(scopes=("kb:read", "session:chat"))
    result = await binding.invoke(_make_call(), ctx)
    assert result.ok is True
    assert result.output == {"hits": []}
    assert result.usage == {"total_tokens": 0}
    assert len(stub.calls) == 1
    name, params, call_ctx = stub.calls[0]
    assert name == "knowledge.search"
    assert params == {"q": "线路A"}  # 值不经采样原样透传
    assert call_ctx.scopes == ("kb:read", "session:chat")  # 调用方 scopes 透传（出口 PDP 同源）
    assert call_ctx.tenant_id == ctx.tenant_id  # 租户贯穿（api/03 §6）
    assert call_ctx.trace_id == ctx.trace_id
    assert call_ctx.caller_type == "agent"


async def test_调用_scope不足_deny拒绝且provider不被触达():
    registry, stub = _platform_registry(scopes=("kb:read",))
    binding = build_mcp_tool_bindings(registry)[0]
    result = await binding.invoke(_make_call(), make_ctx(scopes=()))  # 空 scopes=deny-by-default
    assert result.ok is False
    assert result.error_code == int(ErrorCode.SCOPE_INSUFFICIENT)
    assert stub.calls == []  # PDP 在 provider 之前


async def test_调用_有其他scope但不匹配仍拒_精确匹配非包含():
    registry, stub = _platform_registry(scopes=("ontology:write",))
    binding = build_mcp_tool_bindings(registry)[0]
    result = await binding.invoke(_make_call(), make_ctx(scopes=("kb:read",)))
    assert result.ok is False
    assert result.error_code == int(ErrorCode.SCOPE_INSUFFICIENT)
    assert stub.calls == []


async def test_调用_外部工具空required_scopes_现状语义放行():
    registry, _reg = _external_registry()  # 外部 descriptor required_scopes=()（M4.1 现状，M5 收口）
    binding = build_mcp_tool_bindings(registry)[0]
    result = await binding.invoke(
        _make_call(action_iri=binding.meta.semantic_annotation["action_iri"]),
        make_ctx(scopes=()),
    )
    assert result.ok is True  # 空要求不构成拒绝（F-1 双层授权为独立 P0，桥不补位不重构）


# ── 结果与错误映射（结构化，会话不崩）────────────────────────────────────


async def test_调用_provider失败_错误码透传结构化返回():
    registry, _stub = _platform_registry(failure=(3001, "参数错"))
    binding = build_mcp_tool_bindings(registry)[0]
    result = await binding.invoke(_make_call(), make_ctx(scopes=("kb:read",)))
    assert result.ok is False
    assert result.error_code == 3001
    assert result.error_message == "参数错"


async def test_调用_provider抛McpToolError_收口结构化不逃逸():
    registry, _stub = _platform_registry(raise_exc=McpToolError(5003, "外部 server 熔断中: weather"))
    binding = build_mcp_tool_bindings(registry)[0]
    result = await binding.invoke(_make_call(), make_ctx(scopes=("kb:read",)))
    assert result.ok is False
    assert result.error_code == 5003
    assert "熔断" in (result.error_message or "")


async def test_调用_provider抛CapabilityError_收口结构化():
    registry, _stub = _platform_registry(raise_exc=CapabilityError(5004, "制品缺失"))
    binding = build_mcp_tool_bindings(registry)[0]
    result = await binding.invoke(_make_call(), make_ctx(scopes=("kb:read",)))
    assert result.ok is False
    assert result.error_code == 5004


async def test_调用_provider抛未分类异常_5999兜底不裸逃逸():
    registry, _stub = _platform_registry(raise_exc=RuntimeError("boom"))
    binding = build_mcp_tool_bindings(registry)[0]
    result = await binding.invoke(_make_call(), make_ctx(scopes=("kb:read",)))
    assert result.ok is False
    assert result.error_code == int(ErrorCode.INTERNAL_ERROR)
    assert "boom" in (result.error_message or "")


async def test_调用_发现刷新撤销后_结构化5003不崩():
    ext_registry, _reg = _external_registry()
    binding = build_mcp_tool_bindings(ext_registry)[0]
    ext_registry.unregister_server("weather")  # 连接器重连/发现刷新撤销
    result = await binding.invoke(
        _make_call(action_iri=binding.meta.semantic_annotation["action_iri"]), make_ctx()
    )
    assert result.ok is False
    assert result.error_code == int(ErrorCode.MCP_TARGET_UNAVAILABLE)


# ── 组装点（sessions 工厂开关 gate）─────────────────────────────────────


def test_组装_开关关_返回空绑定():
    state = SimpleNamespace(
        settings=SimpleNamespace(mcp_bridge_enabled=False),
        mcp_registry=_platform_registry()[0],
    )
    assert _build_mcp_tool_bindings(state) == ()


def test_组装_开关开_投影registry全集():
    registry, _stub = _platform_registry()
    state = SimpleNamespace(settings=SimpleNamespace(mcp_bridge_enabled=True), mcp_registry=registry)
    bindings = _build_mcp_tool_bindings(state)
    assert len(bindings) == 1
    assert isinstance(bindings[0], McpToolBinding)


def test_组装_registry缺位_返回空():
    state = SimpleNamespace(settings=SimpleNamespace(mcp_bridge_enabled=True))
    assert _build_mcp_tool_bindings(state) == ()


def test_组装_旧settings无开关字段_缺省开启():
    registry, _stub = _platform_registry()
    state = SimpleNamespace(settings=SimpleNamespace(), mcp_registry=registry)  # getattr 兜底缺省 True
    assert len(_build_mcp_tool_bindings(state)) == 1

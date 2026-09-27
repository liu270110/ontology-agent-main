"""外部 MCP 接入测试（连接器/发现缓存/超时/熔断半开/mcp_targets 配置；MCP 篇 §4）。

Fake Client 注入（同接口零网络）：default_client_factory 为唯一传输装配点，测试经
client_factory 注入内存桩——满足 mcp.types.Tool / CallToolResult 形状即可。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest

mcp_types = pytest.importorskip("mcp.types")

from services.mcp.audit import InMemoryAuditSink  # noqa: E402
from services.mcp.client.connectors import ExternalMcpConnector, ExternalMcpManager  # noqa: E402
from services.mcp.client.targets import McpTargetConfig, load_targets  # noqa: E402
from services.mcp.errors import McpToolError  # noqa: E402
from services.mcp.registry import CapabilityRegistry  # noqa: E402
from services.platform.ports.capability_provider import CallContext  # noqa: E402

CTX = CallContext(tenant_id=uuid.uuid4(), trace_id="t-client", scopes=())


class FakeRemoteClient:
    """fastmcp Client 形状桩：tools 清单 / 调用结果 / 故障与延迟可编程。"""

    def __init__(
        self,
        *,
        tools: list[dict[str, Any]] | None = None,
        fail: bool = False,
        slow_s: float = 0.0,
        structured: bool = True,
    ) -> None:
        self.tools = tools if tools is not None else [{"name": "echo", "description": "回声工具"}]
        self.fail = fail
        self.slow_s = slow_s
        self.structured = structured
        self.list_calls = 0
        self.call_calls = 0

    async def __aenter__(self) -> FakeRemoteClient:
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def list_tools(self) -> list[Any]:
        self.list_calls += 1
        if self.fail:
            raise ConnectionError("远端不可达")
        if self.slow_s:
            await asyncio.sleep(self.slow_s)
        return [
            mcp_types.Tool(name=t["name"], description=t.get("description", ""), inputSchema={"type": "object"})
            for t in self.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.call_calls += 1
        if self.fail:
            raise ConnectionError("远端不可达")
        if self.slow_s:
            await asyncio.sleep(self.slow_s)
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=json.dumps(arguments, ensure_ascii=False))],
            structuredContent={"echo": arguments} if self.structured else None,
            isError=False,
        )

    async def close(self) -> None:
        return None


def _target(**kw: Any) -> McpTargetConfig:
    base: dict[str, Any] = {"name": "power-erp", "transport": "streamable_http", "url": "http://127.0.0.1:1/mcp"}
    base.update(kw)
    return McpTargetConfig(**base)


# ---------------------------------------------------------------- 发现缓存


async def test_发现缓存_TTL内复用不重复拉取():
    client = FakeRemoteClient()
    connector = ExternalMcpConnector(_target(tool_ttl_s=300.0), client_factory=lambda t: client)
    first = await connector.list_tools()
    second = await connector.list_tools()
    # Assert：第二次命中缓存（拉取计数不变）
    assert client.list_calls == 1
    assert first == second and first[0]["name"] == "echo"
    forced = await connector.list_tools(force=True)
    assert client.list_calls == 2 and forced[0]["name"] == "echo"
    await connector.close()


# ---------------------------------------------------------------- 调用转发与超时


async def test_call_tool转发_结构化结果优先():
    client = FakeRemoteClient(structured=True)
    connector = ExternalMcpConnector(_target(), client_factory=lambda t: client)
    value = await connector.call_tool("power-erp.echo", {"order": "A001"})
    assert value == {"echo": {"order": "A001"}}
    assert client.call_calls == 1
    await connector.close()


async def test_外部调用超时映射5003():
    client = FakeRemoteClient(slow_s=1.0)
    connector = ExternalMcpConnector(_target(timeout_s=0.1), client_factory=lambda t: client)
    with pytest.raises(McpToolError) as exc_info:
        await connector.call_tool("power-erp.echo", {})
    assert exc_info.value.code == 5003
    assert "超时" in exc_info.value.message
    await connector.close()


async def test_isError结果转5003工具错误():
    client = FakeRemoteClient()

    async def failing_call(name: str, arguments: dict[str, Any]) -> Any:
        client.call_calls += 1
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="boom")], structuredContent=None, isError=True
        )

    client.call_tool = failing_call  # type: ignore[method-assign]
    connector = ExternalMcpConnector(_target(), client_factory=lambda t: client)
    with pytest.raises(McpToolError) as exc_info:
        await connector.call_tool("power-erp.echo", {})
    assert exc_info.value.code == 5003
    await connector.close()


# ---------------------------------------------------------------- 熔断与半开（MCP 篇 §4）


async def test_连续失败达阈值进入熔断_不再触达远端():
    client = FakeRemoteClient(fail=True)
    connector = ExternalMcpConnector(_target(), client_factory=lambda t: client, failure_threshold=3)
    for _ in range(3):
        with pytest.raises(McpToolError):
            await connector.call_tool("power-erp.echo", {})
    assert connector.state.degraded is True
    calls_before = client.call_calls
    with pytest.raises(McpToolError) as exc_info:
        await connector.call_tool("power-erp.echo", {})
    # Assert：熔断态直接拒绝（不触达远端），提示替代方案语义
    assert client.call_calls == calls_before
    assert "熔断" in exc_info.value.message
    await connector.close()


async def test_半开探测成功恢复调用():
    healthy = FakeRemoteClient()
    state = {"client": FakeRemoteClient(fail=True)}
    connector = ExternalMcpConnector(_target(), client_factory=lambda t: state["client"], failure_threshold=2)
    for _ in range(2):
        with pytest.raises(McpToolError):
            await connector.list_tools(force=True)
    assert connector.state.degraded is True
    state["client"] = healthy  # 远端恢复
    await connector.close()  # 断开熔断期缓存连接（重连语义）
    assert await connector.probe() is True  # 半开探测成功
    value = await connector.call_tool("power-erp.echo", {"k": 1})
    assert value == {"echo": {"k": 1}}
    await connector.close()


# ---------------------------------------------------------------- 接入管理器与命名空间登记


async def test_manager_refresh_外部tool以server前缀登记进注册表():
    client = FakeRemoteClient(tools=[{"name": "create_order", "description": "创建工单"}])
    manager = ExternalMcpManager([_target()], client_factory=lambda t: client)
    registry = CapabilityRegistry()
    registered = await manager.refresh(registry)
    # Assert：全名隔离 + 可路由 + 来源标注
    assert registered == ["power-erp.create_order"]
    found, descriptor = registry.get("power-erp.create_order")  # type: ignore[misc]
    assert found is not None and descriptor.external is True
    result = await found.invoke("power-erp.create_order", {"a": 1}, CTX)  # type: ignore[misc]
    assert result.value == {"echo": {"a": 1}}
    await manager.close()


async def test_manager出向调用审计留痕_含circuit_open状态():
    audit = InMemoryAuditSink()
    state = {"client": FakeRemoteClient()}  # 先健康：发现登记成功
    manager = ExternalMcpManager(
        [_target()], client_factory=lambda t: state["client"], audit_sink=audit, failure_threshold=1
    )
    registry = CapabilityRegistry()
    await manager.refresh(registry)
    found, _descriptor = registry.get("power-erp.echo")  # type: ignore[misc]
    # 远端故障：断开既有连接（重连时经工厂取新桩），首次失败 → error，随后熔断拒绝 → circuit_open
    await manager.connector("power-erp").close()
    state["client"] = FakeRemoteClient(fail=True)
    with pytest.raises(McpToolError):
        await found.invoke("power-erp.echo", {"a": 1}, CTX)  # type: ignore[misc]
    with pytest.raises(McpToolError):
        await found.invoke("power-erp.echo", {"a": 2}, CTX)  # type: ignore[misc]
    # Assert：出向失败留痕（07 §8 熔断演练），trace 贯穿
    statuses = [e.status for e in audit.entries]
    assert statuses == ["error", "circuit_open"]
    assert all(e.trace_id == CTX.trace_id for e in audit.entries)
    await manager.close()


# ---------------------------------------------------------------- mcp_targets 配置


def test_load_targets_合法文件加载(tmp_path):
    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps({"targets": [{"name": "erp", "transport": "stdio", "command": "python", "args": ["-m", "x"]}]}),
        encoding="utf-8",
    )
    targets = load_targets(path)
    assert len(targets) == 1 and targets[0].name == "erp" and targets[0].transport == "stdio"


def test_target校验_非法传输与保留名拒绝():
    with pytest.raises(ValueError):
        _target(transport="websocket")
    with pytest.raises(ValueError):  # 保留命名空间段（防冒充平台 tool）
        _target(name="knowledge")
    with pytest.raises(ValueError):  # http 必带 url
        McpTargetConfig(name="erp", transport="streamable_http")

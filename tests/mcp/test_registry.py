"""注册表与 CapabilityProvider 协议测试（07 篇 §1 / MCP 篇 §3；纯 stdlib，零 fastmcp 依赖）。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from services.mcp.registry import (
    RESERVED_NAMESPACES,
    CapabilityRegistry,
    ExternalInvoker,
    FunctionCapabilityProvider,
    RegistryConflictError,
)
from services.platform.ports.capability_provider import (
    KERNEL_LOOP_VERSION,
    CallContext,
    CapabilityDescriptor,
    CapabilityError,
    CapabilityProvider,
    CapabilityResult,
    IncompatibleProviderError,
)

CTX = CallContext(tenant_id=uuid.uuid4(), trace_id="t-registry-1", scopes=("kb:read",))


class _StubProvider:
    """结构化满足 CapabilityProvider 的桩实现（版本可注入以测握手）。"""

    def __init__(self, namespace: str, *, loop_versions: list[str] | None = None) -> None:
        self.provider_version = "0.1.0"
        self.supported_loop_versions = loop_versions if loop_versions is not None else [KERNEL_LOOP_VERSION]
        self._namespace = namespace

    def namespace(self) -> str:
        return self._namespace

    def capabilities(self) -> list[CapabilityDescriptor]:
        return [
            CapabilityDescriptor(
                name=f"{self._namespace}.probe",
                description="探针能力",
                required_scopes=("kb:read",),
            )
        ]

    async def invoke(self, name: str, params: dict[str, Any], ctx: CallContext) -> CapabilityResult:
        return CapabilityResult.success({"echo": params})


# ---------------------------------------------------------------- 协议与握手


def test_Fake能力结构化满足CapabilityProvider协议():
    provider = _StubProvider("ontology")
    # Arrange/Act/Assert：runtime_checkable 结构化判定（07 §1 协议面）
    assert isinstance(provider, CapabilityProvider)


def test_版本握手失败_兼容区间无交集拒注册():
    registry = CapabilityRegistry()
    with pytest.raises(IncompatibleProviderError):
        registry.register(_StubProvider("ontology", loop_versions=["9.x"]))


def test_重复命名空间注册冲突抛错():
    registry = CapabilityRegistry()
    registry.register(_StubProvider("ontology"))
    with pytest.raises(RegistryConflictError):
        registry.register(_StubProvider("ontology"))


# ---------------------------------------------------------------- 路由与调用 roundtrip


async def test_注册后经注册表路由调用roundtrip():
    registry = CapabilityRegistry()
    provider = _StubProvider("ontology")
    registry.register(provider)
    # Act：按全名路由并调用
    entry = registry.get("ontology.probe")
    assert entry is not None
    found, descriptor = entry
    result = await found.invoke("ontology.probe", {"q": 1}, CTX)
    # Assert：结果与清单
    assert result.ok and result.value == {"echo": {"q": 1}}
    assert [d.name for d in registry.list_tools()] == ["ontology.probe"]


async def test_register_capability快捷注册_name_handler_metadata():
    registry = CapabilityRegistry()

    async def handler(params: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        return {"ok": True}

    metadata = CapabilityDescriptor(name="action.invoke", description="回写桩", required_scopes=("action:invoke",))
    registry.register_capability("action.invoke", handler, metadata)
    # Assert：4.2 action_dispatcher 将以此路径注册执行面
    found, _descriptor = registry.get("action.invoke")  # type: ignore[misc]
    assert isinstance(found, FunctionCapabilityProvider)
    result = await found.invoke("action.invoke", {}, CTX)
    assert result.value == {"ok": True}


async def test_能力内抛CapabilityError_携带已登记错误码():
    registry = CapabilityRegistry()

    async def failing(params: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        raise CapabilityError(3001, "参数不合法")

    registry.register_capability("memory.probe", failing, CapabilityDescriptor(name="memory.probe", description="x"))
    found, _d = registry.get("memory.probe")  # type: ignore[misc]
    with pytest.raises(CapabilityError) as exc_info:
        await found.invoke("memory.probe", {}, CTX)  # type: ignore[misc]
    assert exc_info.value.code == 3001


# ---------------------------------------------------------------- 外部命名空间隔离（红线）


def _external_desc(server: str, local: str) -> CapabilityDescriptor:
    return CapabilityDescriptor(name=f"{server}.{local}", description="外部工具")


async def _echo(full: str, params: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
    return {"external": full}


def test_外部server名占用平台保留命名空间被拒():
    registry = CapabilityRegistry()
    for reserved in ("knowledge", "ontology", "memory", "action"):
        assert reserved in RESERVED_NAMESPACES
    with pytest.raises(RegistryConflictError):
        registry.register_external(
            "knowledge", [_external_desc("knowledge", "search")], ExternalInvoker("knowledge", _echo)
        )


def test_外部tool缺server前缀被拒():
    registry = CapabilityRegistry()
    impostor = CapabilityDescriptor(name="knowledge.search", description="冒充平台 tool")
    with pytest.raises(RegistryConflictError):
        registry.register_external("evil", [impostor], ExternalInvoker("evil", _echo))


def test_外部tool与平台tool撞名被拒():
    registry = CapabilityRegistry()
    registry.register(_StubProvider("ontology"))  # 平台 tool: ontology.probe
    collision = CapabilityDescriptor(name="ontology.probe", description="撞名")  # server="ontology" 本身也被保留段拒绝
    with pytest.raises(RegistryConflictError):
        registry.register_external("ontology", [collision], ExternalInvoker("ontology", _echo))


async def test_外部tool注册后带隔离前缀且可撤销():
    registry = CapabilityRegistry()
    registered = registry.register_external(
        "power-erp", [_external_desc("power-erp", "create_order")], ExternalInvoker("power-erp", _echo)
    )
    # Assert：全名 = {server}.{local}，descriptor 标注 external 来源
    assert registered == ["power-erp.create_order"]
    found, descriptor = registry.get("power-erp.create_order")  # type: ignore[misc]
    assert descriptor.external is True
    result = await found.invoke("power-erp.create_order", {}, CTX)  # type: ignore[misc]
    assert result.value == {"external": "power-erp.create_order"}
    # 撤销后不可路由
    assert registry.unregister_server("power-erp") == 1
    assert registry.get("power-erp.create_order") is None

"""MCP 能力注册表（07 篇 §1 CapabilityRegistry；MCP 篇 §3 命名空间路由）。

职责：
- 平台能力注册：``register(provider)``（版本握手 + 命名空间去重，冲突抛错）；
  简单能力（薄函数 handler）走 ``register_capability(name, handler, metadata)`` 快捷面——
  内部包装为满足 CapabilityProvider 协议的 FunctionCapabilityProvider；
- 外部能力注册：``register_external(server, descriptors, invoker)``——外部 tool 全名强制
  ``{server}.{local}`` 前缀隔离（防冒充平台 tool）：server 名禁用平台保留命名空间段，
  且注册前逐条校验全名不与平台 tool / 保留段冲突（MCP 篇 §4「外部工具默认不可信」的
  命名空间面向；可见性授权/审核态随 mcp_servers 表 M5 收口）；
- 路由与清单：``get(tool_name)`` → (provider, descriptor)；``list_tools()`` → 全量清单
  （按租户可见性过滤随 mcp_tools 表登记，M5；当前返回注册全集，调用方自行过滤）。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from services.platform.ports.capability_provider import (
    KERNEL_LOOP_VERSION,
    CallContext,
    CapabilityDescriptor,
    CapabilityProvider,
    CapabilityResult,
    IncompatibleProviderError,
    loop_versions_compatible,
)

# 平台保留命名空间段（api/03 §3 七 tool + 本篇补充项的第一段；外部 server 名/外部 tool
# 首段禁用，防冒充平台 tool——红线用例见 tests/mcp/test_registry.py）
RESERVED_NAMESPACES: frozenset[str] = frozenset(
    {"knowledge", "ontology", "memory", "action", "writeback", "chat", "task", "platform", "ontology-agent"}
)

_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}(\.[a-zA-Z0-9_-]{1,100}){1,4}$")  # {ns}.{local}，≥1 段点

HandlerFn = Callable[[dict[str, Any], CallContext], Awaitable[dict[str, Any]]]


class RegistryConflictError(Exception):
    """注册冲突：命名空间重复 / tool 全名冲突 / 外部命名冒充平台保留段。"""


class ExternalInvoker:
    """外部 tool 调用器（services/mcp/client 连接器满足此协议；registry 不 import client）。"""

    def __init__(
        self, server: str, invoke_fn: Callable[[str, dict[str, Any], CallContext], Awaitable[dict[str, Any]]]
    ) -> None:
        self.server = server
        self.invoke_fn = invoke_fn


class FunctionCapabilityProvider:
    """薄函数能力包装（register_capability 快捷面的内部载体；结构化满足 CapabilityProvider）。"""

    def __init__(
        self,
        namespace: str,
        handlers: dict[str, HandlerFn],
        descriptors: list[CapabilityDescriptor],
        *,
        provider_version: str = "0.1.0",
    ) -> None:
        self.provider_version = provider_version
        self.supported_loop_versions = [KERNEL_LOOP_VERSION]
        self._namespace = namespace
        self._handlers = handlers
        self._descriptors = descriptors

    def namespace(self) -> str:
        return self._namespace

    def capabilities(self) -> list[CapabilityDescriptor]:
        return list(self._descriptors)

    async def invoke(self, name: str, params: dict[str, Any], ctx: CallContext) -> CapabilityResult:
        local = name.split(".", 1)[1] if "." in name else name
        handler = self._handlers.get(local) or self._handlers.get(name)
        if handler is None:
            return CapabilityResult.failure(3001, f"能力未实现: {name}")
        value = await handler(params, ctx)
        return CapabilityResult.success(value)


class ExternalToolProvider:
    """外部 tool 的 provider 侧适配：invoke 直转 invoker（结果包 CapabilityResult）。"""

    def __init__(self, invoker: ExternalInvoker) -> None:
        self._invoker = invoker
        self.provider_version = "external"
        self.supported_loop_versions = [KERNEL_LOOP_VERSION]

    def namespace(self) -> str:
        return self._invoker.server

    def capabilities(self) -> list[CapabilityDescriptor]:
        return []

    async def invoke(self, name: str, params: dict[str, Any], ctx: CallContext) -> CapabilityResult:
        value = await self._invoker.invoke_fn(name, params, ctx)
        return CapabilityResult.success(value)


class CapabilityRegistry:
    """出口 tool 注册表：平台 provider + 外部 server 隔离命名（含 L3/L5 注册握手）。"""

    def __init__(self) -> None:
        self._providers: dict[str, CapabilityProvider] = {}  # namespace → provider
        self._tools: dict[str, tuple[CapabilityProvider, CapabilityDescriptor]] = {}
        self._external_servers: set[str] = set()

    # ---------------------------------------------------------------- 平台能力

    def register(self, provider: CapabilityProvider) -> None:
        """注册平台能力 provider（07 §1：按命名空间去重，冲突抛错；版本握手 fail-fast）。"""
        if not loop_versions_compatible(list(provider.supported_loop_versions)):
            raise IncompatibleProviderError(
                f"provider {provider.namespace()} 兼容区间 {provider.supported_loop_versions} "
                f"与内核 loop 契约版本 {KERNEL_LOOP_VERSION} 无交集，拒注册"
            )
        ns = provider.namespace()
        if ns in self._providers:
            raise RegistryConflictError(f"命名空间已注册: {ns}")
        self._providers[ns] = provider
        for descriptor in provider.capabilities():
            self._check_platform_name(descriptor.name)
            self._tools[descriptor.name] = (provider, descriptor)

    def register_capability(self, name: str, handler: HandlerFn, metadata: CapabilityDescriptor) -> None:
        """简单能力注册（register(name, handler, metadata) 快捷面）：同名命名空间内合并。"""
        namespace, _, local = name.partition(".")
        existing = self._providers.get(namespace)
        if isinstance(existing, FunctionCapabilityProvider):
            existing._handlers[local or name] = handler  # noqa: SLF001 — 同模块组合面
            self._tools[name] = (existing, metadata)
            return
        if existing is not None:
            raise RegistryConflictError(f"命名空间已注册: {namespace}")
        provider = FunctionCapabilityProvider(namespace, {local or name: handler}, [metadata])
        self.register(provider)

    def _check_platform_name(self, name: str) -> None:
        if not _TOOL_NAME_RE.match(name):
            raise RegistryConflictError(f"平台 tool 名须为 {{namespace}}.{{local}}: {name}")
        if name in self._tools:
            raise RegistryConflictError(f"tool 已注册: {name}")

    # ---------------------------------------------------------------- 外部能力

    def register_external(
        self, server: str, descriptors: Iterable[CapabilityDescriptor], invoker: ExternalInvoker
    ) -> list[str]:
        """注册外部 server 的 tool 清单：全名强制 {server}.{local} 前缀，防冒充平台 tool。

        拒绝三态（红线用例）：server 名占用平台保留段；外部 tool 全名与平台 tool 撞名；
        外部 tool 全名落入保留段前缀（如 server="foo" 注册全名 "knowledge.search"）。
        返回实际登记的 tool 全名清单（发现刷新时先 unregister_server 再注册）。
        """
        if server in RESERVED_NAMESPACES:
            raise RegistryConflictError(f"外部 server 名占用平台保留命名空间: {server}")
        registered: list[str] = []
        for descriptor in descriptors:
            full_name = descriptor.name
            if not full_name.startswith(f"{server}."):
                raise RegistryConflictError(f"外部 tool 缺 server 前缀: {full_name}（须为 {server}.{{local}}）")
            if full_name.split(".", 1)[0] in RESERVED_NAMESPACES:
                raise RegistryConflictError(f"外部 tool 冒充平台命名空间: {full_name}")
            if full_name in self._tools:
                raise RegistryConflictError(f"tool 已注册: {full_name}")
            marked = CapabilityDescriptor(
                name=full_name,
                description=descriptor.description,
                input_schema=dict(descriptor.input_schema),
                required_scopes=descriptor.required_scopes,
                annotations=dict(descriptor.annotations),
                semantic=dict(descriptor.semantic),
                tags=("external", *descriptor.tags),
                external=True,
            )
            self._tools[full_name] = (ExternalToolProvider(invoker), marked)
            registered.append(full_name)
        self._external_servers.add(server)
        return registered

    def unregister_server(self, server: str) -> int:
        """撤销外部 server 的全部 tool（连接器重连/发现刷新用），返回撤销数。"""
        doomed = [name for name, (_, d) in self._tools.items() if d.external and name.startswith(f"{server}.")]
        for name in doomed:
            self._tools.pop(name, None)
        self._external_servers.discard(server)
        return len(doomed)

    # ---------------------------------------------------------------- 查询

    def get(self, tool_name: str) -> tuple[CapabilityProvider, CapabilityDescriptor] | None:
        """按全名路由（"namespace.tool"；未注册返回 None，出口映射 -32601 语义）。"""
        return self._tools.get(tool_name)

    def list_tools(self, tenant_id: uuid.UUID | None = None) -> list[CapabilityDescriptor]:
        """工具清单（api/03 §2：CapabilityRegistry list_tools(tenant_id)）。

        tenant_id 过滤位随 mcp_tools 可见性登记收口（M5，07 §1 序列图 tools_list(ctx)）；
        当前返回注册全集（外部 tool 以 descriptor.external 标注来源，调用方可再过滤）。
        """
        return [d for _, d in self._tools.values()]

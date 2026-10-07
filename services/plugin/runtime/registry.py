"""plugin 运行时最小版（M5-1；docs/Skills §5 plugin-daemon 模式的进程内形态）。

职责（任务 5 批交付物 4）：已发布插件的**加载/启停/健康探活**（进程内注册表）+ 沙箱供给
对接面。M5 最小版边界：
- ``mcp_server``/``rest_api`` 远程插件：注册表登记 + 代理调用面（RemoteInvoker 注入，缺省
  调用返回 5003 结构化失败——不阻塞加载与健康探活）；
- 制品型（代码执行）插件：**fail-closed**——无沙箱后端拒绝加载（Skills §5 默认完全无网 +
  Sandbox §3.2 禁止静默降级），绝不进程内直跑不可信代码；
- 独立守护进程容器形态、出网白名单 egress 下发、配额回收随 Sandbox/deploy 批次。

沙箱供给对接（任务要求「只经 services/sandbox 公开面」论证）：pyproject 契约九
（kb.retrieval 与 sandbox.runtime 模块私有）**禁止 plugin→services.sandbox.runtime import
且无豁免边**（本批禁改 pyproject），故此处以 :class:`PluginSandboxBackend` 结构化协议镜像
sandbox.runtime 公开面（``supports/create/exec/destroy`` 字面同形，runtime_checkable）——
真实后端（DockerBackend）由具备豁免边的组合根注入（契约需求见模块报告），测试以 Fake 满足
同形协议；形状漂移由 tests/plugin/test_runtime.py 镜像断言锚定。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from services.platform.kernel import DomainError
from services.plugin.domain.model.manifest import manifest_transport
from services.plugin.domain.model.plugin import Plugin, PluginVersion


class PluginRuntimeState(StrEnum):
    """运行时实例状态（进程内；启停面）。"""

    RUNNING = "running"
    SUSPENDED = "suspended"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class HealthReport:
    """健康探活报告（探活=运行时最小闭环；OTel/容器级探针随观测批次）。"""

    plugin_id: uuid.UUID
    ok: bool
    state: PluginRuntimeState
    detail: str
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": str(self.plugin_id),
            "ok": self.ok,
            "state": self.state.value,
            "detail": self.detail,
            "checked_at": self.checked_at.isoformat(),
        }


@runtime_checkable
class PluginSandboxBackend(Protocol):
    """沙箱后端结构化协议（镜像 services/sandbox/runtime 公开面子集，见模块 docstring）。"""

    def supports(self, trust_level: str, scenario: str) -> bool: ...

    async def create(self, spec: dict[str, Any]) -> Any: ...

    async def exec(self, handle: Any, cmd: Any) -> Any: ...

    async def destroy(self, handle: Any, *, remove_workspace: bool = True) -> None: ...


@runtime_checkable
class RemoteInvoker(Protocol):
    """远程插件代理调用面（mcp/client 连接器同形；M5 最小版由组合根/测试注入）。"""

    async def invoke(self, plugin_id: uuid.UUID, url: str, tool: str, params: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    """运行时登记条目（进程内；不含凭据——外部默认不可信，Skills §1 信任要求）。"""

    plugin_id: uuid.UUID
    slug: str
    version: str
    kind: str
    state: PluginRuntimeState = PluginRuntimeState.STOPPED
    transport: dict[str, Any] = field(default_factory=dict)
    server_json: dict[str, Any] = field(default_factory=dict)  # 清单快照（能力清单声明源）


class PluginRuntime:
    """进程内插件注册表：load（已发布插件）→ start/stop（启停）→ health_check（探活）。

    只收**已发布**版本（人工终审后 runtime 才可见——候选非成品门禁在 runtime 侧的落点）；
    非 published 版本 load 即拒（4502）。
    """

    def __init__(
        self,
        *,
        sandbox_backend: PluginSandboxBackend | None = None,
        remote_invoker: RemoteInvoker | None = None,
    ) -> None:
        self._sandbox = sandbox_backend
        self._invoker = remote_invoker
        self._loaded: dict[uuid.UUID, LoadedPlugin] = {}

    # ---- 加载/卸载 ----

    async def load(self, plugin: Plugin, version: PluginVersion) -> LoadedPlugin:
        """加载已发布插件版本（状态机校验 + 沙箱 fail-closed 断言）。"""
        from services.plugin.domain.model.plugin import PluginStatus, PluginVersionStatus

        if version.status is not PluginVersionStatus.PUBLISHED or plugin.status is not PluginStatus.PUBLISHED:
            raise DomainError(
                f"4502 PLUGIN_NOT_PUBLISHED: 仅已发布插件可加载运行（plugin={plugin.status.value}, "
                f"version={version.status.value}；候选非成品门禁）"
            )
        transport = manifest_transport(version.server_json)
        needs_sandbox = plugin.kind.value in ("skill", "prompt") or (transport.get("type") in (None, "stdio", "local"))
        if needs_sandbox:
            # 制品/本地型插件必须进沙箱（S1/S2）：fail-closed，无后端拒绝，禁止进程内直跑
            if self._sandbox is None:
                raise DomainError(
                    "4506 SANDBOX_BACKEND_UNAVAILABLE: 沙箱后端不可达，制品型插件拒绝加载"
                    "（fail-closed，Sandbox §3.2 禁止静默降级）"
                )
            if not self._sandbox.supports("T2", "S1"):
                raise DomainError(
                    "4506 SANDBOX_BACKEND_UNAVAILABLE: 沙箱后端不支持 (T2, S1) 信任组合，拒绝加载（fail-closed）"
                )
        loaded = LoadedPlugin(
            plugin_id=plugin.id,
            slug=plugin.slug,
            version=version.version,
            kind=plugin.kind.value,
            state=PluginRuntimeState.STOPPED,
            transport=transport,
            server_json=dict(version.server_json),
        )
        self._loaded[plugin.id] = loaded
        return loaded

    def unload(self, plugin_id: uuid.UUID) -> None:
        """卸载登记条目（下架/弃用联动）。"""
        self._loaded.pop(plugin_id, None)

    # ---- 启停（published ↔ suspended 的运行时执行位）----

    def start(self, plugin_id: uuid.UUID) -> PluginRuntimeState:
        loaded = self._require(plugin_id)
        self._loaded[plugin_id] = LoadedPlugin(
            plugin_id=loaded.plugin_id,
            slug=loaded.slug,
            version=loaded.version,
            kind=loaded.kind,
            state=PluginRuntimeState.RUNNING,
            transport=loaded.transport,
            server_json=loaded.server_json,
        )
        return self._loaded[plugin_id].state

    def stop(self, plugin_id: uuid.UUID) -> PluginRuntimeState:
        loaded = self._require(plugin_id)
        self._loaded[plugin_id] = LoadedPlugin(
            plugin_id=loaded.plugin_id,
            slug=loaded.slug,
            version=loaded.version,
            kind=loaded.kind,
            state=PluginRuntimeState.STOPPED,
            transport=loaded.transport,
            server_json=loaded.server_json,
        )
        return self._loaded[plugin_id].state

    # ---- 健康探活 ----

    async def health_check(self, plugin_id: uuid.UUID) -> HealthReport:
        """探活：运行中=ok（远程插件经 invoker 可达性由部署档位决定，最小版按登记态判定）。"""
        loaded = self._loaded.get(plugin_id)
        if loaded is None:
            return HealthReport(plugin_id=plugin_id, ok=False, state=PluginRuntimeState.STOPPED, detail="未加载")
        ok = loaded.state is PluginRuntimeState.RUNNING
        detail = "running" if ok else f"state={loaded.state.value}"
        return HealthReport(plugin_id=plugin_id, ok=ok, state=loaded.state, detail=detail)

    # ---- 查询/代理调用 ----

    def get(self, plugin_id: uuid.UUID) -> LoadedPlugin | None:
        return self._loaded.get(plugin_id)

    def list_loaded(self) -> list[LoadedPlugin]:
        return list(self._loaded.values())

    async def invoke_tool(self, plugin_id: uuid.UUID, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        """代理调用（远程插件；M5 最小版无 invoker/非运行中一律 5003 结构化失败，不伪成功）。"""
        loaded = self._loaded.get(plugin_id)
        if loaded is None or loaded.state is not PluginRuntimeState.RUNNING:
            raise DomainError(f"5003 MCP_TARGET_UNAVAILABLE: 插件未运行，拒绝代理调用: {plugin_id}")
        if self._invoker is None:
            raise DomainError("5003 MCP_TARGET_UNAVAILABLE: 远程调用器未装配（部署档位限制）")
        url = str(loaded.transport.get("url") or "")
        if not url:
            raise DomainError("5003 MCP_TARGET_UNAVAILABLE: 插件清单缺少 transport.url")
        return await self._invoker.invoke(plugin_id, url, tool, params)

    def _require(self, plugin_id: uuid.UUID) -> LoadedPlugin:
        loaded = self._loaded.get(plugin_id)
        if loaded is None:
            raise DomainError(f"4502 PLUGIN_NOT_LOADED: 插件未加载: {plugin_id}")
        return loaded

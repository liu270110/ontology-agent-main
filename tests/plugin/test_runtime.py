"""plugin 运行时测试：加载门禁（候选非成品/fail-closed）、启停、健康探活、能力清单出口。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from services.platform.kernel import DomainError
from services.plugin.domain.model.plugin import Plugin, PluginKind, PluginVersion
from services.plugin.runtime.provider import PluginCapabilityProvider
from services.plugin.runtime.registry import PluginRuntime, PluginRuntimeState, PluginSandboxBackend
from services.sandbox.runtime import DockerBackend
from tests.plugin.helpers import published_plugin


class FakeInvoker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def invoke(self, plugin_id: uuid.UUID, url: str, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, url))
        return {"echo": tool, "params": params}


class FakeSandbox:
    """PluginSandboxBackend 形状 Fake（supports 可配）。"""

    def __init__(self, supported: bool = True) -> None:
        self.supported = supported
        self.created: list[str] = []

    def supports(self, trust_level: str, scenario: str) -> bool:
        return self.supported

    async def create(self, spec: dict[str, Any]) -> Any:
        self.created.append(str(spec))
        return {"handle": "fake"}

    async def exec(self, handle: Any, cmd: Any) -> Any:
        return {"exit_code": 0}

    async def destroy(self, handle: Any, *, remove_workspace: bool = True) -> None:
        return None


def _artifact_plugin() -> tuple[Any, Any]:
    """制品型（skill）插件：无 transport（本地制品），必须进沙箱。"""
    server_json = {
        "name": "io.ontology-agent/local-skill",
        "display_name": "本地技能",
        "version": "1.0.0",
        "description": "本地制品插件",
        "transport": {"type": "local"},
        "x-platform": {"schema_version": "1", "category": "skills", "required_scopes": ["skill:use"]},
    }
    return published_plugin("local-skill", server_json=server_json)


async def test_runtime_仅发布插件可加载_未发布4502拒绝():
    # Arrange
    plugin, version = published_plugin()
    draft = Plugin(slug="draft-p", name="d", kind=PluginKind.MCP_SERVER)
    draft_version = PluginVersion(
        plugin_id=draft.id,
        version="0.1.0",
        artifact_key=f"plugin-packages/{draft.id}/0.1.0/package.zip",
        checksum="c" * 64,
    )
    runtime = PluginRuntime()
    # Act / Assert：draft 插件 + submitted 版本 → 4502（候选非成品门禁在 runtime 的落点）
    with pytest.raises(DomainError) as exc:
        await runtime.load(draft, draft_version)
    assert int(str(exc.value)[:4]) == 4502
    # Act：已发布 → 加载成功（STOPPED 待启）
    loaded = await runtime.load(plugin, version)
    assert loaded.state is PluginRuntimeState.STOPPED


async def test_runtime_制品型插件无沙箱fail_closed拒绝_禁进程内直跑():
    # Arrange：skill 制品型（transport=local）
    plugin, version = _artifact_plugin()
    # Act / Assert：无沙箱后端 → 4506 拒绝（Sandbox §3.2 禁止静默降级）
    runtime = PluginRuntime()
    with pytest.raises(DomainError) as exc:
        await runtime.load(plugin, version)
    assert int(str(exc.value)[:4]) == 4506
    # Act / Assert：沙箱不支持 (T2, S1) → 仍拒绝
    runtime_denied = PluginRuntime(sandbox_backend=FakeSandbox(supported=False))
    with pytest.raises(DomainError) as exc:
        await runtime_denied.load(plugin, version)
    assert int(str(exc.value)[:4]) == 4506
    # Act：沙箱支持 → 加载成功
    runtime_ok = PluginRuntime(sandbox_backend=FakeSandbox(supported=True))
    loaded = await runtime_ok.load(plugin, version)
    assert loaded is not None


async def test_runtime_启停与健康探活_停止态探活不健康():
    # Arrange
    plugin, version = published_plugin()
    runtime = PluginRuntime()
    await runtime.load(plugin, version)
    # Assert：加载即探活=不健康（未运行）
    report = await runtime.health_check(plugin.id)
    assert report.ok is False and report.state is PluginRuntimeState.STOPPED
    # Act：start → 探活健康；stop → 回不健康
    assert runtime.start(plugin.id) is PluginRuntimeState.RUNNING
    assert (await runtime.health_check(plugin.id)).ok is True
    runtime.stop(plugin.id)
    assert (await runtime.health_check(plugin.id)).ok is False
    # Assert：未加载插件探活 → 不健康不抛（探活为只读诊断面）
    assert (await runtime.health_check(uuid.uuid4())).ok is False


async def test_runtime_代理调用_未运行或无invoker一律5003不伪成功():
    # Arrange
    plugin, version = published_plugin()
    runtime = PluginRuntime(remote_invoker=FakeInvoker())
    await runtime.load(plugin, version)
    # Act / Assert：未运行 → 5003
    with pytest.raises(DomainError) as exc:
        await runtime.invoke_tool(plugin.id, "weather.query", {"city": "杭州"})
    assert int(str(exc.value)[:4]) == 5003
    # Act：运行中 → 代理调用成功
    runtime.start(plugin.id)
    value = await runtime.invoke_tool(plugin.id, "weather.query", {"city": "杭州"})
    assert value == {"echo": "weather.query", "params": {"city": "杭州"}}
    # Act / Assert：无 invoker → 5003
    runtime_no_invoker = PluginRuntime()
    await runtime_no_invoker.load(plugin, version)
    runtime_no_invoker.start(plugin.id)
    with pytest.raises(DomainError) as exc:
        await runtime_no_invoker.invoke_tool(plugin.id, "weather.query", {})
    assert int(str(exc.value)[:4]) == 5003


async def test_provider_能力清单_运行中插件全名导出_停用不出清单():
    # Arrange
    plugin, version = published_plugin()
    invoker = FakeInvoker()
    runtime = PluginRuntime(remote_invoker=invoker)
    provider = PluginCapabilityProvider(runtime)
    await runtime.load(plugin, version)
    # Assert：STOPPED 不出清单（候选非成品/未运行不出口）
    assert provider.capabilities() == []
    runtime.start(plugin.id)
    # Act
    descriptors = provider.capabilities()
    # Assert：全名 plugin.{slug}.{tool}（防冒名）；annotations 仅随行不进授权
    assert [d.name for d in descriptors] == ["plugin.weather.weather.query"]
    assert descriptors[0].required_scopes == ("weather:read",)


async def test_provider_invoke_路由到宿主插件_未运行3001_畸形名3001():
    # Arrange
    plugin, version = published_plugin()
    invoker = FakeInvoker()
    runtime = PluginRuntime(remote_invoker=invoker)
    provider = PluginCapabilityProvider(runtime)
    await runtime.load(plugin, version)
    # Act / Assert：未运行 → 3001
    result = await provider.invoke("plugin.weather.weather.query", {"city": "杭州"}, _ctx())
    assert result.ok is False and result.code == 3001
    # Act：运行中 → 路由成功
    runtime.start(plugin.id)
    result = await provider.invoke("plugin.weather.weather.query", {"city": "杭州"}, _ctx())
    # Assert
    assert result.ok is True and result.value is not None
    assert result.value["version"] == "1.0.0"
    # Act / Assert：畸形全名 → 3001
    bad = await provider.invoke("weather.query", {}, _ctx())
    assert bad.ok is False and bad.code == 3001


async def test_provider_沙箱协议镜像_真实DockerBackend结构化满足公开面形状():
    # Assert：services/sandbox.runtime 公开面 DockerBackend 满足 PluginSandboxBackend 结构协议
    # （形状漂移锚定——组合根注入真实后端时的契约需求，见模块报告）
    backend = DockerBackend()
    assert isinstance(backend, PluginSandboxBackend)
    # supports 语义镜像：T3/S4 拒（fail-closed 语义一致）
    from services.sandbox.runtime.types import Scenario, TrustLevel

    assert backend.supports(TrustLevel.T2_MARKET, Scenario.S1_MARKET) is True
    assert backend.supports(TrustLevel.T3_ADVERSARIAL, Scenario.S4_EVAL) is False


def _ctx():
    from services.platform.ports.capability_provider import CallContext

    return CallContext(tenant_id=uuid.uuid4(), trace_id="rt-test", scopes=("tool:invoke",))

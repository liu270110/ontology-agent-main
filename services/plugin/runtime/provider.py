"""插件能力供给方：PluginCapabilityProvider（结构化满足 CapabilityProvider 协议）。

依赖倒置（锚点 01 §3.4 同款）：插件能力经 platform.ports 的协议注册进 MCP registry
（M5 前置=出口白名单机制就绪前，L2 能力包通道只对平台方签名包开放——Skills §3.4.4；
本批仅登记**已发布且运行中**插件声明的工具为能力清单，运行期异常自动 suspend 联动由
启停面承载）。红线：descriptor.annotations 仅 UI 提示，永不进授权路径（08 §2.3）。

装配位：组合根（gateway lifespan）构建 PluginRuntime 后将本 provider 注册进
mcp.registry.CapabilityRegistry——bootstrap.build_capability_registry 为 mcp 既有文件
（本批禁改、无 providers 钩子），注册动作落组合根，契约需求见模块报告。
"""

from __future__ import annotations

from typing import Any

from services.platform.ports.capability_provider import (
    CallContext,
    CapabilityDescriptor,
    CapabilityResult,
)
from services.plugin.domain.model.manifest import manifest_tools
from services.plugin.runtime.registry import PluginRuntime, PluginRuntimeState


class PluginCapabilityProvider:
    """已发布插件的能力出口（namespace=``plugin``；非平台保留段，registry 冲突检测兜底）。"""

    provider_version = "1.0.0"
    supported_loop_versions = ["1.x"]

    def __init__(self, runtime: PluginRuntime) -> None:
        self._runtime = runtime

    def namespace(self) -> str:
        return "plugin"

    def capabilities(self) -> list[CapabilityDescriptor]:
        """运行中插件声明的工具 → 能力清单（全名=``plugin.{slug}.{tool}``，防跨插件冒名）。"""
        descriptors: list[CapabilityDescriptor] = []
        for loaded in self._runtime.list_loaded():
            if loaded.state is not PluginRuntimeState.RUNNING:
                continue
            for tool in manifest_tools(loaded.server_json):
                descriptors.append(
                    CapabilityDescriptor(
                        name=f"plugin.{loaded.slug}.{tool.name}",
                        description=tool.description,
                        input_schema=dict(tool.input_schema),
                        required_scopes=tuple(tool.required_scopes) or ("tool:invoke",),
                        annotations=dict(tool.annotations),  # UI 提示（不可信，不进授权路径）
                        semantic=({"action_iri": tool.ontology_action_iri} if tool.ontology_action_iri else {}),
                        tags=("plugin", loaded.slug),
                    )
                )
        return descriptors

    async def invoke(self, name: str, params: dict[str, Any], ctx: CallContext) -> CapabilityResult:
        """路由到所属插件的代理调用面；未运行/无清单 3001，目标不可达 5003。"""
        parts = name.split(".")
        if len(parts) < 3 or parts[0] != "plugin":
            return CapabilityResult.failure(3001, f"插件能力全名须为 plugin.{{slug}}.{{tool}}: {name}")
        slug, tool = parts[1], ".".join(parts[2:])
        loaded = next((x for x in self._runtime.list_loaded() if x.slug == slug), None)
        if loaded is None or loaded.state is not PluginRuntimeState.RUNNING:
            return CapabilityResult.failure(3001, f"插件未运行: {slug}")
        try:
            value = await self._runtime.invoke_tool(loaded.plugin_id, tool, params)
        except Exception as exc:  # noqa: BLE001 ——领域错误带码直译，其余按目标不可达
            code = getattr(exc, "code", 5003)
            return CapabilityResult.failure(int(code), str(exc))
        return CapabilityResult.success(
            {"plugin_id": str(loaded.plugin_id), "version": loaded.version, "result": value}
        )

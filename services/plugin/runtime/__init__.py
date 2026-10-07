"""plugin 运行时（L7 最小版：进程内注册表 + 沙箱供给协议 + 能力注册）。"""

from .provider import PluginCapabilityProvider
from .registry import (
    HealthReport,
    LoadedPlugin,
    PluginRuntime,
    PluginRuntimeState,
    PluginSandboxBackend,
    RemoteInvoker,
)

__all__ = [
    "HealthReport",
    "LoadedPlugin",
    "PluginCapabilityProvider",
    "PluginRuntime",
    "PluginRuntimeState",
    "PluginSandboxBackend",
    "RemoteInvoker",
]

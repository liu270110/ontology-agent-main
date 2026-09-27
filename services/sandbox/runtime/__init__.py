"""L7 统一沙箱运行时（模块 13；docs/Sandbox §4——会话/插件/流水线/评测训练共用）。

对 agent 侧的入口是工具注册中心的 core-exec 内置工具组（Sandbox §5.0），
本包只暴露后端协议与实现；daemon 服务形态（独立容器）随 deploy 编排落地。
"""

from .backend import (
    BackendUnavailableError,
    ExecCommand,
    ExecLimitError,
    ExecResult,
    ProvisionSpec,
    SandboxBackend,
    SandboxHandle,
    SandboxRuntimeError,
    SnapshotRef,
    assert_supported,
    spec_from_mapping,
)
from .docker_backend import DockerBackend

__all__ = [
    "BackendUnavailableError",
    "DockerBackend",
    "ExecCommand",
    "ExecLimitError",
    "ExecResult",
    "ProvisionSpec",
    "SandboxBackend",
    "SandboxHandle",
    "SandboxRuntimeError",
    "SnapshotRef",
    "assert_supported",
    "spec_from_mapping",
]

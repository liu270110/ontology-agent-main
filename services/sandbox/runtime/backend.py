"""沙箱执行后端协议（Sandbox §4.2 execution.backends；研究整理/07 §7.2-7 落位）。

契约五操作：create / exec / snapshot / restore / destroy；async 签名（对齐平台 async 原生），
实现方内部自行处理同步 SDK 的线程包装。**fail-closed 是协议级义务**：supports() 为 False 的
（信任级, 场景）组合，实现方必须拒绝 create 并抛 BackendUnavailableError，禁止静默降级（Sandbox §3.2）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from services.sandbox.runtime.types import Scenario, TrustLevel


class SandboxRuntimeError(Exception):
    """沙箱运行时错误基类（错误码映射随 api/01 分段归属登记）。"""


class BackendUnavailableError(SandboxRuntimeError):
    """fail-closed：所需隔离强度的后端在当前部署不可达——调用方应显式拒绝供给。"""


class ExecLimitError(SandboxRuntimeError):
    """执行超限（超时 / 输出配额 / pids）——超限即终止并留痕（Sandbox §7.1）。"""


@dataclass(frozen=True)
class ExecCommand:
    cmd: list[str]
    timeout_seconds: int = 120  # 单命令超时（Sandbox §7.1 基线）
    workdir: str = "/workspace"


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout_b: bytes
    stderr_b: bytes
    truncated: bool = False  # 单命令 64KB 截断标记（Sandbox §7.1）

    @property
    def stdout(self) -> str:
        return self.stdout_b.decode(errors="replace")

    @property
    def stderr(self) -> str:
        return self.stderr_b.decode(errors="replace")


@dataclass(frozen=True)
class ProvisionSpec:
    """一次供给需求（由 SandboxProfile 派生，daemon 侧执行）。"""

    instance_id: str  # 平台侧 SandboxInstance.id（容器名/卷名派生源）
    base_image: str
    cpu_limit: float = 0.5
    mem_limit_mb: int = 512
    pids_limit: int = 256
    tmpfs_size_mb: int = 64  # 显式 size——tmpfs 不计 cgroup OOM 的实测对策（Sandbox §6 隔离矩阵）
    scenario: Scenario = Scenario.S0_SESSION
    trust_level: TrustLevel = TrustLevel.T2_MARKET
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxHandle:
    instance_id: str
    container_name: str
    workspace_volume: str


@dataclass(frozen=True)
class SnapshotRef:
    instance_id: str
    object_key: str  # MinIO key（v1 兼容本地路径）
    size_bytes: int


@runtime_checkable
class SandboxBackend(Protocol):
    """后端协议（Sandbox §4.2；对齐 docs/Sandbox 设计的四后端：docker/fncall/microvm/remote）。"""

    def supports(self, trust_level: TrustLevel, scenario: Scenario) -> bool: ...

    async def create(self, spec: ProvisionSpec) -> SandboxHandle: ...

    async def exec(self, handle: SandboxHandle, cmd: ExecCommand) -> ExecResult: ...

    async def snapshot(self, handle: SandboxHandle, source: str = "hibernate") -> SnapshotRef: ...

    async def restore(self, snapshot: SnapshotRef, spec: ProvisionSpec) -> SandboxHandle: ...

    async def destroy(self, handle: SandboxHandle, *, remove_workspace: bool = True) -> None: ...


def assert_supported(backend: SandboxBackend, spec: ProvisionSpec) -> None:
    """fail-closed 门前断言（供 daemon/用例层复用；错误信息必须明确不可达原因）。"""
    if not backend.supports(spec.trust_level, spec.scenario):
        raise BackendUnavailableError(
            f"fail-closed：后端 {type(backend).__name__} 不支持 "
            f"(trust={spec.trust_level.value}, scenario={spec.scenario.value})——拒绝供给，禁止静默降级（Sandbox §3.2）"
        )


def spec_from_mapping(data: dict[str, Any]) -> ProvisionSpec:
    """从 profile/配置字典构造（daemon 侧反序列化入口）。"""
    return ProvisionSpec(
        instance_id=str(data["instance_id"]),
        base_image=str(data.get("base_image", "docker.m.daocloud.io/library/python:3.12-slim")),
        cpu_limit=float(data.get("cpu_limit", 0.5)),
        mem_limit_mb=int(data.get("mem_limit_mb", 512)),
        pids_limit=int(data.get("pids_limit", 256)),
        tmpfs_size_mb=int(data.get("tmpfs_size_mb", 64)),
        scenario=Scenario(str(data.get("scenario", "S0"))),
        trust_level=TrustLevel(str(data.get("trust_level", "T2"))),
    )

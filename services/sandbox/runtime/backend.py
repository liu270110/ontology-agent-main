"""沙箱执行后端协议（Sandbox §4.2 execution.backends；研究整理/07 §7.2-7 落位）。

契约五操作：create / exec / snapshot / restore / destroy；async 签名（对齐平台 async 原生），
实现方内部自行处理同步 SDK 的线程包装。**fail-closed 是协议级义务**：supports() 为 False 的
（信任级, 场景）组合，实现方必须拒绝 create 并抛 BackendUnavailableError，禁止静默降级（Sandbox §3.2）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from services.sandbox.runtime.types import Scenario, TrustLevel

logger = logging.getLogger(__name__)


class SandboxRuntimeError(Exception):
    """沙箱运行时错误基类（错误码映射随 api/01 分段归属登记）。"""


class BackendUnavailableError(SandboxRuntimeError):
    """fail-closed：所需隔离强度的后端在当前部署不可达——调用方应显式拒绝供给。"""


class ExecLimitError(SandboxRuntimeError):
    """执行超限（超时 / 输出配额 / pids）——超限即终止并留痕（Sandbox §7.1）。"""


# 宿主 env 刷洗表（K5 门 3，方案依据=docs/Agent/13 §10；上游参照 prime-agent #3345 env
# 剥离表 + deer-flow 宿主刷洗 §7）：沙箱是不可信执行面，宿主凭证族变量直灌即泄漏面——
# GIT_ASKPASS/GIT_SSH* 可劫持 git 凭证助手与传输，SSH_AUTH_SOCK 可借宿主 agent 签名，
# *_TOKEN/*_API_KEY/*_SECRET 是云与 LLM 供应商凭证族；命中即剥离并告警（先于容器组装）。
#
# 枚举制残余风险（K9-c/B-6，方案依据=docs/Agent/13 §15）：未列名且不落在下方后缀通配族的
# 新凭证变量不剥离（如路径类 GOOGLE_APPLICATION_CREDENTIALS 型、非标准后缀私有族）——
# **新凭证族须同步扩表**（精确项）；标准凭证后缀族由 *_API_KEY/*_TOKEN/*_SECRET 通配兜底。
HOST_ENV_DENYLIST: frozenset[str] = frozenset(
    {
        "GIT_ASKPASS",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_HTTP_LOW_SPEED_LIMIT",
        "GIT_HTTP_LOW_SPEED_TIME",
        "SSH_AUTH_SOCK",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AZURE_CLIENT_SECRET",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "HF_TOKEN",
    }
)

# 后缀通配族（K9-c/B-6 第二层）：变量名以后缀命中即剥（精确枚举为主、通配为辅）——新厂商
# 按命名惯例的凭证变量（如 MYAPP_API_KEY/VENDOR_TOKEN/X_SECRET）自动覆盖，无需扩表；
# 大小写敏感（env 变量惯例大写）。代价：非凭证的罕见同名后缀变量会被一并剥离（fail-safe 取向）。
HOST_ENV_DENYLIST_SUFFIXES: tuple[str, ...] = ("_API_KEY", "_TOKEN", "_SECRET")


def _is_denied_env_name(name: str) -> bool:
    """双层命中判定（K9-c/B-6）：精确枚举（主）+ 凭证后缀通配（辅）。"""
    return name in HOST_ENV_DENYLIST or name.endswith(HOST_ENV_DENYLIST_SUFFIXES)


def sanitize_env(env: dict[str, str]) -> tuple[dict[str, str], tuple[str, ...]]:
    """宿主 env 刷洗（纯函数；K5 门 3）：命中刷洗表即剥离并返回剥离清单。

    命中判定为双层（K9-c/B-6）：HOST_ENV_DENYLIST 精确枚举 + 凭证后缀通配
    （HOST_ENV_DENYLIST_SUFFIXES，*_API_KEY/*_TOKEN/*_SECRET 命中即剥）。
    不改入参（防御拷贝）；返回 (剥离后 env, 剥离变量名元组按命中序)。env 组装点
    （docker_backend.create 容器直灌、spec_from_mapping 反序列化边界）统一先过本函数。
    """
    stripped = tuple(name for name in env if _is_denied_env_name(name))
    if not stripped:
        return dict(env), ()
    return {name: value for name, value in env.items() if not _is_denied_env_name(name)}, stripped


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
    """一次供给需求（由 SandboxProfile 派生，daemon 侧执行）。

    env 契约（K9-c/B-6）：**env 必经 sanitize_env 方可到达容器**——现接线点=
    spec_from_mapping（反序列化边界）与 docker_backend.create（容器组装点）；
    新增 env 供给通路不得绕过该刷洗门。
    """

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
    """从 profile/配置字典构造（daemon 侧反序列化入口）。

    env 可选透传（缺省空 dict 与既有调用零差），并在反序列化边界先过 K5 门 3 刷洗表
    （命中剥离 + warning 告警）——入口拒绝严于容器组装点兜底。
    """
    env, stripped = sanitize_env({str(k): str(v) for k, v in (data.get("env") or {}).items()})
    if stripped:
        logger.warning(
            "沙箱 spec 反序列化剥离宿主敏感 env: %s (instance=%s)", ",".join(stripped), data.get("instance_id")
        )
    return ProvisionSpec(
        instance_id=str(data["instance_id"]),
        base_image=str(data.get("base_image", "docker.m.daocloud.io/library/python:3.12-slim")),
        cpu_limit=float(data.get("cpu_limit", 0.5)),
        mem_limit_mb=int(data.get("mem_limit_mb", 512)),
        pids_limit=int(data.get("pids_limit", 256)),
        tmpfs_size_mb=int(data.get("tmpfs_size_mb", 64)),
        scenario=Scenario(str(data.get("scenario", "S0"))),
        trust_level=TrustLevel(str(data.get("trust_level", "T2"))),
        env=env,
    )

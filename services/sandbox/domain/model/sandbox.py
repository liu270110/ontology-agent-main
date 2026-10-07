"""L4 领域模型：沙箱聚合（docs/Sandbox §4.1 领域模型 + §3 场景/信任级 + §9 状态机权威）。

聚合纪律对齐 session.py：validate_assignment=True；不变式只写聚合方法；
状态机迁移表本文件为权威（database/01 §3.10 的 CHECK 枚举与本文件一致）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Scenario(StrEnum):
    """六场景（Sandbox §3.1）。"""

    S0_SESSION = "S0"  # 会话沙箱：agent 启动即用的执行底座
    S1_PLUGIN = "S1"  # 插件沙箱
    S2_PIPELINE = "S2"  # 流水线短执行
    S3_SLOT_HOST = "S3"  # 外部 agent 插槽宿主
    S4_EVAL = "S4"  # 评测回放（RSI 消费）
    S5_TRAINING = "S5"  # 训练 rollout（v2）


class TrustLevel(StrEnum):
    """四信任级（Sandbox §3.2）。fail-closed：所需后端不可达即拒绝，禁静默降级。"""

    T0_BUILTIN = "T0"
    T1_OFFICIAL = "T1"
    T2_MARKET = "T2"
    T3_ADVERSARIAL = "T3"


class QoSClass(StrEnum):
    LS = "ls"  # 时延敏感（agent 交互步）
    BE = "be"  # 尽力而为（后台批量）


class InstanceStatus(StrEnum):
    """状态机（Sandbox §9；paused 仅审批短窗 ≤30min，daemon 代理心跳）。"""

    CREATING = "creating"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    HIBERNATED = "hibernated"
    FAILED = "failed"
    TERMINATED = "terminated"


_VALID_TRANSITIONS: dict[InstanceStatus, set[InstanceStatus]] = {
    InstanceStatus.CREATING: {InstanceStatus.READY, InstanceStatus.FAILED},
    InstanceStatus.READY: {InstanceStatus.RUNNING, InstanceStatus.FAILED, InstanceStatus.TERMINATED},
    InstanceStatus.RUNNING: {
        InstanceStatus.PAUSED,
        InstanceStatus.HIBERNATED,
        InstanceStatus.FAILED,
        InstanceStatus.TERMINATED,
    },
    InstanceStatus.PAUSED: {InstanceStatus.RUNNING, InstanceStatus.TERMINATED},
    InstanceStatus.HIBERNATED: {InstanceStatus.RUNNING, InstanceStatus.TERMINATED},  # 保留期内唤醒=快照还原重建
    InstanceStatus.FAILED: {InstanceStatus.TERMINATED},
    InstanceStatus.TERMINATED: set(),
}

_OWNER_KINDS = {"session", "pipeline", "plugin", "eval_run"}


class SandboxError(Exception):
    """领域错误（fail-closed 与非法迁移都从这里出，不允许静默降级）。"""


class EgressRule(BaseModel):
    """出口白名单单条（Sandbox §6：域名:端口:协议；缺省集为空=全拒）。"""

    model_config = ConfigDict(frozen=True)

    host: str
    port: int = Field(default=443, ge=1, le=65535)
    proto: str = Field(default="tcp", pattern="^(tcp|udp)$")
    phase: str | None = None  # 任务阶段标签（Sandbox §6 阶段动态更新）


class SandboxProfile(BaseModel):
    """隔离需求声明（Sandbox §4.1）。租户只能在平台内置模板上收紧（§3.2 治理边界）。"""

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID | None = None  # None = 平台内置模板
    name: str = Field(min_length=1, max_length=128)
    scenario: Scenario
    trust_level: TrustLevel
    cpu_limit: float = Field(default=0.5, gt=0, le=8)
    mem_limit_mb: int = Field(default=512, gt=0, le=8192)
    pids_limit: int = Field(default=256, ge=16)
    output_quota_kb: int = Field(default=10240, ge=64)  # 任务级累计输出配额（Sandbox §7.1）
    egress_policy_id: uuid.UUID | None = None  # None = 默认全拒
    base_image: str = "docker.m.daocloud.io/library/python:3.12-slim"
    toolkit_ref: dict[str, Any] = Field(default_factory=dict)
    qos: QoSClass = QoSClass.LS
    is_builtin: bool = False

    def tighten_only(self, base: SandboxProfile) -> None:
        """租户覆盖校验：只允许收紧（配额下调/白名单收窄），不允许放宽（Sandbox §3.2）。"""
        if self.cpu_limit > base.cpu_limit or self.mem_limit_mb > base.mem_limit_mb:
            raise SandboxError("租户配置只能收紧：资源配额不得超过平台内置模板")
        if self.trust_level != base.trust_level:
            raise SandboxError("信任级由平台内置映射裁定，租户不可改")


class SandboxInstance(BaseModel):
    """运行实体（Sandbox §4.1；归属四选一 = owner_kind + owner_ref）。"""

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    profile_id: uuid.UUID
    owner_kind: str  # session | pipeline | plugin | eval_run
    owner_ref: uuid.UUID
    status: InstanceStatus = InstanceStatus.CREATING
    backend: str = "docker"
    instance_ref: str | None = None  # 后端句柄（docker 容器名）
    workspace_volume: str | None = None
    credential_jti: uuid.UUID | None = None  # 沙箱凭据（销毁即吊销，Sandbox §5.4）
    last_heartbeat_at: datetime | None = None

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        if self.owner_kind not in _OWNER_KINDS:
            raise SandboxError(f"非法归属类型 {self.owner_kind}（四选一：{_OWNER_KINDS}）")

    @property
    def is_dead(self) -> bool:
        return self.status in (InstanceStatus.TERMINATED,)

    def transition(self, to: InstanceStatus) -> None:
        if to not in _VALID_TRANSITIONS[self.status]:
            raise SandboxError(f"非法状态迁移 {self.status} → {to}（Sandbox §9 状态机）")
        self.status = to

    def heartbeat(self) -> None:
        self.last_heartbeat_at = datetime.now(UTC)


class SandboxSnapshot(BaseModel):
    """可写层增量快照（Sandbox §8.2 pack_diff v1 = 卷 tar）。"""

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    instance_id: uuid.UUID
    object_key: str  # MinIO key（v1 兼容本地目录路径）
    source: str = Field(pattern="^(hibernate|checkpoint|artifact)$")
    sanitized: bool = False  # 残留清理断言（两段式治理）
    retain_until: datetime | None = None  # 保留期（默认 72h，Sandbox §5.5）
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SandboxEvent(BaseModel):
    """生命周期与出口审计事件（只追加；Sandbox §10）。"""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    instance_id: uuid.UUID | None = None
    # 枚举：provisioned/ready/exec/paused/resumed/hibernated/awakened/snapshotted/
    # destroyed/egress_denied/quota_exceeded/fail_closed（Sandbox §10）
    event: str
    detail: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

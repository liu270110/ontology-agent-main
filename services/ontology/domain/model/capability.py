"""L4 领域模型：能力运行台账值对象（06 篇 §ONT-2.3；database/01 §ONT-2 DDL 状态/通道封闭集）。

五态机（0034 runs + 0050 修正）：pending → despatched → {succeeded | partial | failed}；
终态不可再推进。台账只记不裁（at-most-once 派发闸=E3 待专题，06 篇 §ONT-2.3 明示本批
只记台账不接调用点——写点选型属 ONT-3 接线批）。
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CapabilityRunStatus(StrEnum):
    """运行台账五态（ck_capability_runs_status 同口径）。"""

    PENDING = "pending"
    DESPATCHED = "despatched"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


TERMINAL_RUN_STATUSES: tuple[CapabilityRunStatus, ...] = (
    CapabilityRunStatus.SUCCEEDED,
    CapabilityRunStatus.PARTIAL,
    CapabilityRunStatus.FAILED,
)

_LEGAL_TRANSITIONS: dict[CapabilityRunStatus, tuple[CapabilityRunStatus, ...]] = {
    CapabilityRunStatus.PENDING: (CapabilityRunStatus.DESPATCHED, *TERMINAL_RUN_STATUSES),
    CapabilityRunStatus.DESPATCHED: TERMINAL_RUN_STATUSES,
    CapabilityRunStatus.SUCCEEDED: (),
    CapabilityRunStatus.PARTIAL: (),
    CapabilityRunStatus.FAILED: (),
}


class CapabilityRunChannel(StrEnum):
    """派发通道（ck_capability_runs_channel 同口径；四通道=Agent/02 内核/能力层划分）。"""

    KERNEL = "kernel"
    MCP = "mcp"
    API = "api"
    SKILL = "skill"


def assert_run_transition(current: str, target: str) -> None:
    """状态机守卫（repo 层推进唯一入口的纯函数）：非法迁移 ValueError，终态不可再推进。"""
    try:
        cur, tgt = CapabilityRunStatus(current), CapabilityRunStatus(target)
    except ValueError as exc:
        raise ValueError(f"未知台账状态: {current!r}/{target!r}（五态封闭集）") from exc
    if tgt not in _LEGAL_TRANSITIONS[cur]:
        raise ValueError(f"非法台账迁移 {cur.value} → {tgt.value}（pending→despatched→终态，0050 五态机）")


class CapabilityRunIn(BaseModel):
    """台账开单（insert 面入参；E5 三交集身份 requested_by/session 待 ONT-4 补，06 篇 §ONT-2.3）。"""

    model_config = ConfigDict(extra="forbid")

    capability_iri: str = Field(min_length=1, max_length=256)
    capability_id: uuid.UUID | None = None
    action_iri: str | None = Field(default=None, max_length=256)
    channel: CapabilityRunChannel
    requested_by: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    trace_id: str | None = Field(default=None, max_length=64)
    input_digest: dict | None = None  # 参数摘要（≤4KB 由调用方钳制；本批只存不裁）
    target_ref_type: str | None = Field(default=None, max_length=32)
    target_ref_id: uuid.UUID | None = None


class CapabilityRunClose(BaseModel):
    """部分关闭/终态推进（partial close 面入参）：status 必为终态三值之一。"""

    model_config = ConfigDict(extra="forbid")

    status: CapabilityRunStatus  # 仓储层校验 ∈ 终态集
    result_digest: dict | None = None  # ≤4KB 摘录
    target_ref_type: str | None = Field(default=None, max_length=32)
    target_ref_id: uuid.UUID | None = None

    def validated(self) -> CapabilityRunStatus:
        if self.status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"close 仅收终态 {[s.value for s in TERMINAL_RUN_STATUSES]}，得 {self.status.value}")
        return self.status

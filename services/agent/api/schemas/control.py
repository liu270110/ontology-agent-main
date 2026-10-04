"""L2 网关 DTO：运行中输入面（M4.5-A inbox 提交 + admin estop；docs/Agent/12 §1.4）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class InboxSubmitIn(BaseModel):
    """inbox 提交入参（§1.1 三通道；text 非空由 DTO 先挡，容量上限在内核 4203 结构化拒绝）。"""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["followup", "steer", "inject"]
    text: str = Field(min_length=1, max_length=8_000, description="注入文本（用户指令，审计必需内容）")


class InboxSubmitOut(BaseModel):
    """inbox 提交回执：seq=收件箱内单调序号（对账键）。"""

    model_config = ConfigDict(extra="forbid")
    run_id: str
    seq: int
    kind: str
    status: str = "accepted"


class EStopActivateIn(BaseModel):
    """紧急停止激活入参（§1.2：reason 必填，审计留痕）。"""

    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2_000, description="激活原因（审计必需）")


class EStopStateOut(BaseModel):
    """紧急停止状态视图：active=false 时 reason/by/at 为 None（轮询友好）。"""

    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    active: bool
    reason: str | None = None
    by: str | None = None
    at: float | None = None

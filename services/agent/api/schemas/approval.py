"""L2 网关 DTO：运行中审批（H-0b；api/01 §5.15 ★ 行——与领域模型严格分离，铁律同 task.py）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ApprovalDecisionIn(BaseModel):
    """裁决入参（api/01 §5.15）：decision/param_hash 必填，reason/create_ticket/rule_hint 可选。"""

    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    param_hash: str = Field(min_length=8, max_length=128, description="待审批动作的参数哈希（B5 绑定键）")
    reason: str | None = Field(default=None, max_length=2000, description="reject 理由（审计留痕）；approve 可选备注")
    create_ticket: bool = Field(default=False, description="审批中心联动：自动建 target_type=run_approval 工单")
    rule_hint: str | None = Field(
        default=None,
        min_length=1,
        max_length=2000,
        description="审批人附带的策略修正提示，非空时回流规则候选队列供人工终审"
        "（K19-b：仅 approve 生效，回流≠自动放行生效）",
    )


class PendingApprovalOut(BaseModel):
    """待审批动作视图（action_* 为 None=当前无可审批动作；轮询友好恒 200）。"""

    model_config = ConfigDict(extra="forbid")
    task_id: str
    run_id: str
    run_status: str
    action_iri: str | None = None
    param_hash: str | None = None
    execution_mode: str | None = None
    waiting_since: str | None = None


class ApprovalDecisionOut(BaseModel):
    """裁决出参：approve→run_status=running（resume 已触发）；reject→cancelled。"""

    model_config = ConfigDict(extra="forbid")
    decision: str
    task_id: str
    run_id: str
    run_status: str
    ticket_id: str | None = None
    review_ticket_id: str | None = None
    review_linkage: str = "skipped"  # created | degraded | skipped

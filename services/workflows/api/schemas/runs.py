"""X16 工作流运行 DTO（api/01 §5.11 runs 族六端点；信封=api/01 §3.1 {data, meta}）。

形状出处：api/01 §5.11 预登记行（test/runs=202→task；runs 列表对齐任务中心过滤；
run 详情=节点状态聚合视图；resume/abort=202）+ 40 篇 §4.2 事件 payload 字段面。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from services.platform.schemas import EmptyMeta, PageMeta


class WorkflowTestIn(BaseModel):
    """试运行入参（POST /workflows/{id}/test；27 篇 §3 试运行断点+入参变量）。"""

    model_config = ConfigDict(extra="forbid")

    breakpoints: list[str] = Field(default_factory=list)  # 断点节点集（命中即暂停 waiting_approval）
    variables: dict[str, Any] = Field(default_factory=dict)  # 入口变量（condition/template 求值面）


class WorkflowRunIn(BaseModel):
    """正式运行入参（POST /workflows/{id}/runs；执行 head 不可变版本快照）。"""

    model_config = ConfigDict(extra="forbid")

    variables: dict[str, Any] = Field(default_factory=dict)


class WorkflowRunAcceptedOut(BaseModel):
    """受理响应（202；tasks/runs 既有管道异步执行，前端轮询详情端点）。"""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    run_id: UUID
    kind: Literal["workflow_run", "workflow_test"]
    version: int | None = None  # workflow_run=执行版本；test=None（草稿快照）
    status: Literal["queued"] = "queued"


class WorkflowRunSummaryOut(BaseModel):
    """运行历史行（GET /workflows/{id}/runs；对齐任务中心过滤视图）。"""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    task_id: UUID
    kind: str
    version: int | None = None
    task_status: str  # pending/running/succeeded/failed/cancelled（tasks 五态）
    run_status: str | None = None  # runs 七态投影（queued/.../waiting_tool）
    created_at: datetime | None = None


class WorkflowRunsOut(BaseModel):
    """运行历史响应（api/01 §3.1 {data, meta} 信封）。"""

    model_config = ConfigDict(extra="forbid")

    data: list[WorkflowRunSummaryOut]
    meta: PageMeta


class WorkflowRunDetailOut(BaseModel):
    """运行详情（节点状态聚合视图；payload.workflow_state 投影——27 篇 §3 试运行面板取数口）。"""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    task_id: UUID
    workflow_id: UUID
    kind: str
    version: int | None = None
    task_status: str
    run_status: str | None = None
    paused_node: str | None = None  # 暂停节点（approval/breakpoint；27 篇 §3 断点语义）
    paused_kind: str | None = None
    nodes: dict[str, dict[str, Any]]  # node_id → {status,attempt,title,parallel_id,started_at,...}
    outputs: dict[str, Any] = Field(default_factory=dict)  # 节点出参表（下游输入面）
    error: dict[str, Any] | None = None
    created_at: datetime | None = None


class WorkflowRunDetailEnvelope(BaseModel):
    """详情响应（api/01 §3.1 {data, meta} 信封）。"""

    model_config = ConfigDict(extra="forbid")

    data: WorkflowRunDetailOut
    meta: EmptyMeta = Field(default_factory=EmptyMeta)


class WorkflowResumeIn(BaseModel):
    """断点恢复入参（POST /workflows/{id}/runs/{run_id}/resume；27 篇 §3 time-travel）。"""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"] = "approve"
    param_hash: str | None = Field(default=None, max_length=128)  # 审批类恢复必携（B5 哈希绑定）
    params: dict[str, Any] | None = None  # 修参（合入暂停节点 params_override，续跑生效）
    note: str | None = Field(default=None, max_length=512)


class WorkflowAbortIn(BaseModel):
    """运行中止入参（POST /workflows/{id}/runs/{rid}/abort；对齐 tasks/cancel 语义）。"""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=512)


class WorkflowRunControlOut(BaseModel):
    """resume/abort 响应（202；续跑/中止异步生效，终态以详情端点轮询为准）。"""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    decision: str  # approve | reject | abort
    run_status: str
    resumed_node: str | None = None

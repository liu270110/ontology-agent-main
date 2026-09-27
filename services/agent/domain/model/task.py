"""L4 领域模型：Task 聚合（含 Run、TaskEvent 实体）。

权威：04 篇 §2 task 行不变式 + §3 task/run 状态机（唯一版本，模块文档只引用不复制）。
2026-09-26 M1 批次新增，未改动 session.py 既有不变式。纪律同 04 §1：
validate_assignment=True；实体按 id 判等；业务不变式只写聚合方法；
TaskEvent 只追加（经 TaskRepository.append_event 持久化，不走全量 save）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TaskError(Exception):
    """领域错误（错误码映射 02 篇 §7：41xx session 段 4102 TASK_ALREADY_RUNNING 等）"""


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_TOOL = "waiting_tool"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


# 状态机（04 §3 权威图的代码化）：单向至终态，终态不可逆
_VALID_TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.RUNNING},
    TaskStatus.RUNNING: {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}

_VALID_RUN_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.CANCELLED},
    RunStatus.RUNNING: {
        RunStatus.WAITING_TOOL,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.TIMEOUT,
        RunStatus.CANCELLED,
    },
    RunStatus.WAITING_TOOL: {RunStatus.RUNNING, RunStatus.CANCELLED},
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: set(),
    RunStatus.TIMEOUT: set(),
    RunStatus.CANCELLED: set(),
}

_ACTIVE_RUN_STATES = frozenset({RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_TOOL})


class Run(BaseModel):
    """Run 实体（task 聚合内，04 §2「一次执行尝试」）：按 id 判等。

    usage/时间戳由执行侧与仓储回填；attempt≤3 的重试建模为「重建新 Run」（04 §3）。
    """

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    task_id: uuid.UUID
    seq_start: int = 0  # 本 Run 的 task_events.seq 起始（跨 Run 连续递增，04 §2）
    status: RunStatus = RunStatus.QUEUED
    usage: dict[str, Any] = Field(default_factory=dict)  # tokens/cost 汇总（LiteLLM 回传，M3+）
    error: dict[str, Any] | None = None  # {code,message,retryable}
    started_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime | None = None

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Run) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    @property
    def is_active(self) -> bool:
        """活跃态（queued/running/waiting_tool）=PG 部分唯一索引 uk_runs_one_active 同一口径。"""
        return self.status in _ACTIVE_RUN_STATES

    def cancel(self) -> None:
        """取消（04 §3 run 状态机：queued/running/waiting_tool → cancelled）。

        cancelled 落终态前的取消清单化传播（子 Run 级联/在途工具中止/租约释放）归
        执行编排（04 §3 cancelled 注记，M3+）；本方法只做状态迁移断言。
        """
        self._transition(RunStatus.CANCELLED)

    def _transition(self, to: RunStatus) -> None:
        if to not in _VALID_RUN_TRANSITIONS[self.status]:
            raise TaskError(f"非法状态迁移 run {self.status} → {to}（04 篇 §3 状态机）")
        self.status = to


class TaskEvent(BaseModel):
    """任务事件实体（只追加；SSE 转发源，02 §5：id=task_events.seq 语义）：按 id 判等。

    seq 由仓储分配（严格递增、先落库后推送，04 §2），本实体不自行分配。
    """

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    task_id: uuid.UUID
    seq: int | None = None
    event_type: str
    data: dict[str, Any] = Field(default_factory=dict)  # 事件载荷 JSON
    created_at: datetime | None = None

    def __eq__(self, other: object) -> bool:
        return isinstance(other, TaskEvent) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)


class Task(BaseModel):
    """task 聚合根：状态单向迁移至终态、终态不可逆（04 §2）。

    attempt_count ≤3 的断言随重试用例（M3+）在聚合方法内补齐；「同一会话至多一个
    活跃 Run」属并发不变式，由 PG 部分唯一索引强制（04 §2.1），聚合内只断言同实例
    重复 start_run。runs 为聚合内实体，save 全量保存（04 §4 约定）。
    """

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID
    type: str
    status: TaskStatus = TaskStatus.PENDING
    session_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    attempt_count: int = 0
    active_run_id: uuid.UUID | None = None  # 当前活跃 Run 指针（与 tasks.active_run_id 同义）
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    runs: list[Run] = Field(default_factory=list)
    created_at: datetime | None = None

    def start_run(self) -> Run:
        """受理 → 执行：pending→running 由首个 Run 承载（04 §3 task 状态机）。返回新建 Run（queued）。"""
        if self.status is not TaskStatus.PENDING:
            raise TaskError(f"非法状态迁移 {self.status} → running（04 篇 §3 状态机）")
        if self._active_run() is not None:
            raise TaskError("4102 TASK_ALREADY_RUNNING: 任务已存在活跃 Run")
        run = Run(tenant_id=self.tenant_id, task_id=self.id)
        self.runs.append(run)
        self.active_run_id = run.id
        self.status = TaskStatus.RUNNING
        return run

    def cancel(self) -> Run | None:
        """用户取消（running→cancelled，04 §3）：活跃 Run 置 cancelled 后聚合落终态。

        非运行态取消返回 False（4102 由路由层兜底，2026-09-27 验收修正注释；错误码登记 api/01 §5.2 cancel 行）。
        返回被取消的 Run（无则 None）。
        """
        self._transition(TaskStatus.CANCELLED)
        run = self._active_run()
        if run is not None and run.is_active:
            run.cancel()
            self.active_run_id = None
        return run

    def _active_run(self) -> Run | None:
        if self.active_run_id is None:
            return None
        return next((r for r in self.runs if r.id == self.active_run_id), None)

    def _transition(self, to: TaskStatus) -> None:
        if to not in _VALID_TASK_TRANSITIONS[self.status]:
            raise TaskError(f"非法状态迁移 {self.status} → {to}（04 篇 §3 状态机）")
        self.status = to

"""L4 领域模型：Task 聚合（含 Run、TaskEvent 实体）。

权威：04 篇 §2 task 行不变式 + §3 task/run 状态机（唯一版本，模块文档只引用不复制）。
2026-09-26 M1 批次新增，未改动 session.py 既有不变式。纪律同 04 §1：
validate_assignment=True；实体按 id 判等；业务不变式只写聚合方法；
TaskEvent 只追加（经 TaskRepository.append_event 持久化，不走全量 save）。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
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

_MAX_TASK_ATTEMPTS = 3  # attempt_count ≤3 含首次（至多 2 次重试；Agent 服务设计 §2 补全表）


class RunRetryPolicy(BaseModel):
    """Run 级重试策略（Agent 服务设计 §2 补全表定稿）：退避四元组缺一即评审打回。

    重试是**编排器主权动作**（模型无权宣布重试，对齐 02 §2 A4 同构约束）；仅
    ``run_error.retryable=true`` 的失败可触发（cancelled/判据已满足 completed/预算耗尽
    类失败不重试）；``retry_budget_total`` 为任务重建 Run 的全局预算，每次扣 1，余额
    不足快速失败（不建新 Run，task 直接 failed 并下发 RUN_ERROR 5005）。
    """

    base_seconds: float = 5.0  # Run 级基数 5s（高于平台默认 1s：Run 重建含上下文重组成本）
    multiplier: float = 2.0  # 5s × 2ⁿ
    cap_seconds: float = 60.0  # 上限 60s
    jitter_ratio: float = 0.2  # jitter ±20%
    retry_budget_total: int = 10  # 任务级重试预算（03 §1 默认 N=10）

    def backoff_seconds(self, retry_index: int, *, rng: Callable[[], float]) -> float:
        """第 retry_index 次重试（0 起）的退避秒数；rng 注入保证测试确定性（standards 纪律）。"""
        delay = min(self.base_seconds * (self.multiplier**retry_index), self.cap_seconds)
        jitter = 1.0 + (rng() * 2 - 1) * self.jitter_ratio
        return round(delay * jitter, 3)


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
    # 40 篇 R1（2026-10-04）：子 Run 血统四字段——NULL=根 Run；子 Run=内核 spawn_sub 派生，
    # 沿用同一七态状态机；depth 0=根，上限护栏=R10（统一配置层，后续批次）。
    parent_run_id: uuid.UUID | None = None
    label: str | None = None  # 子代理显示名（根 Run 为 None）
    goal: str | None = None  # 子任务目标（根 Run 为 None）
    depth: int = 0
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

    def start(self) -> None:
        """认领执行（queued→running，04 §3「适配器 spawn 成功」）：worker 认领/内联受理共用。"""
        self._transition(RunStatus.RUNNING)

    def complete(self, usage: dict[str, Any] | None = None) -> None:
        """正常完成（running→completed），用量随终态落账。"""
        self._transition(RunStatus.COMPLETED)
        if usage:
            self.usage = usage

    def fail(self, error: dict[str, Any] | None = None) -> None:
        """失败终态（running→failed），error={code,message,retryable} 结构化留痕。"""
        self._transition(RunStatus.FAILED)
        if error is not None:
            self.error = error

    def timeout(self) -> None:
        """超时终态（running→timeout，04 §3 run 七态之一）。"""
        self._transition(RunStatus.TIMEOUT)

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
        """受理 → 执行：pending→running 由首个 Run 承载（04 §3 task 状态机）。返回新建 Run（queued）。

        attempt_count=已受理 Run 数（含首次，§2 补全表「≤3 含首次」的计数口径）。
        断言语义只约束**根 Run**（40 篇 R1）：PENDING 前置与活跃互斥断言均针对根 Run——
        子 Run（parent_run_id 非空）不走本方法，另经仓储独立写入口落库（session_repo）。
        """
        if self.status is not TaskStatus.PENDING:
            raise TaskError(f"非法状态迁移 {self.status} → running（04 篇 §3 状态机）")
        if self._active_run() is not None:
            raise TaskError("4102 TASK_ALREADY_RUNNING: 任务已存在活跃 Run")
        run = Run(tenant_id=self.tenant_id, task_id=self.id)
        self.runs.append(run)
        self.active_run_id = run.id
        self.attempt_count += 1
        self.status = TaskStatus.RUNNING
        return run

    def start_retry_run(self) -> Run:
        """失败重试（编排器主权动作，§2 补全表；04 §3 权威图：重试期间 task 保持 RUNNING，
        `running→failed` 仅在「Run failed 且重试耗尽」时发生）。

        前置：task 处于 RUNNING（活跃 Run 已终态）或 FAILED（耗尽后的恢复路径）；
        attempt≤3 含首次；新 Run 重新消费触发消息（重放），消息 seq 不变（messages 只追加
        不变式不破）；失败 Run 的部分输出仅留 task_events 审计流。调用方（编排器/监督者）
        须先核验 ``run_error.retryable`` 与 RunRetryPolicy 预算余额，本方法只断言聚合内不变式。
        """
        if self.status not in (TaskStatus.RUNNING, TaskStatus.FAILED):
            raise TaskError(f"非法状态迁移 {self.status} → running（重试仅限 running/failed 任务，04 §3）")
        active = self._active_run()
        if active is not None and active.is_active:
            raise TaskError("4102 TASK_ALREADY_RUNNING: 活跃 Run 未终态，禁止重建")
        if self.attempt_count >= _MAX_TASK_ATTEMPTS:
            raise TaskError(f"重试预算耗尽：attempt_count={self.attempt_count} 已达上限 {_MAX_TASK_ATTEMPTS}（含首次）")
        run = Run(tenant_id=self.tenant_id, task_id=self.id)
        self.runs.append(run)
        self.active_run_id = run.id
        self.attempt_count += 1
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
        # 40 篇 R1：活跃断言只约束根 Run——子 Run 不经聚合落库（仓储侧 WHERE parent_run_id
        # IS NULL 隔离）；此处防御性再过滤，防调用方手工注入子 Run 误触 4102 断言。
        return next(
            (r for r in self.runs if r.id == self.active_run_id and r.parent_run_id is None),
            None,
        )

    def succeed(self) -> None:
        """成功终态（running→succeeded，04 §3：Run completed 承载）。"""
        self._transition(TaskStatus.SUCCEEDED)

    def fail(self) -> None:
        """失败终态（running→failed，04 §3：Run failed 且重试耗尽；调用方=编排器/监督者）。"""
        self._transition(TaskStatus.FAILED)

    def _transition(self, to: TaskStatus) -> None:
        if to not in _VALID_TASK_TRANSITIONS[self.status]:
            raise TaskError(f"非法状态迁移 {self.status} → {to}（04 篇 §3 状态机）")
        self.status = to

"""L2 网关 DTO：Task / Run（02 篇 §6——与领域模型严格分离，转换函数同文件）。

铁律：extra="forbid"、snake_case、只数据无行为；api/01 §5.2 tasks 行契约。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from services.agent.domain.model.task import Run, Task
from services.platform.schemas import PageMeta


class RunOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    status: str
    seq_start: int
    usage: dict
    error: dict | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


class TaskOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    type: str
    status: str
    session_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    attempt_count: int
    active_run_id: uuid.UUID | None
    created_at: datetime | None = None  # 域内已有（Task.created_at），列表/详情统一透出（台账 B1④）


class TaskDetailOut(TaskOut):
    model_config = ConfigDict(extra="forbid")
    payload: dict
    result: dict | None = None
    error: str | None = None
    runs: list[RunOut]


class TaskListOut(BaseModel):
    """任务列表（api/01 §3.1 信封：{data, meta:{page,page_size,total}}，B1 批统一）。"""

    model_config = ConfigDict(extra="forbid")
    data: list[TaskOut]
    meta: PageMeta


class TaskEventOut(BaseModel):
    """任务事件（只追加；api/01 §5.2 GET /tasks/{id}/events 行，seq=Last-Event-ID 口径）。"""

    model_config = ConfigDict(extra="forbid")
    seq: int
    event_type: str
    data: dict
    created_at: datetime | None = None


class TaskEventPageOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[TaskEventOut]
    next_after_seq: int | None = None
    limit: int


# ── W1 缺口补齐批（2026-10-04）：logs 行视图 + retry（契约冻结 2026-10-04 前端 W3 形状）──


class TaskLogOut(BaseModel):
    """run 日志行（api/01 §5.2 GET /tasks/{id}/logs；契约源=frontend tasks/api.ts TaskLog）。

    数据源=task_events 投影：ts=事件 created_at、level 由 event_type 关键词收敛
    （ERROR/FAIL→error，WARN→warn，余 info）、line=event_type + 紧凑 data JSON。"""

    model_config = ConfigDict(extra="forbid")
    ts: datetime | None = None
    level: str
    line: str


class TaskLogPageOut(BaseModel):
    """日志行分页（游标 after_seq 续读；契约冻结「以 items 形状为冻结口径」，next_cursor
    为字符串游标，取尽为 null——与 mocks/admin-handlers 形状一致）。"""

    model_config = ConfigDict(extra="forbid")
    items: list[TaskLogOut] = []
    next_cursor: str | None = None


class TaskRetryIn(BaseModel):
    """重试请求体（api/01 §5.2 POST /tasks/{id}/retry；契约冻结 body {scope}）。"""

    model_config = ConfigDict(extra="forbid")
    scope: Literal["failed_steps", "all"] = "all"


class TaskRetryOut(BaseModel):
    """重试受理（202 异步口径；status=queued 指重建 Run 已入队交 worker 认领——聚合任务行
    状态经 start_retry_run 落 running（04 §3 权威路径），终态真值以 GET /tasks/{id} 轮询为准；
    run_id 为附加回执面便于前端跟踪新 Run）。"""

    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    status: str
    scope: str
    run_id: uuid.UUID | None = None


def run_from_domain(r: Run) -> RunOut:
    return RunOut(
        id=r.id,
        status=r.status.value,
        seq_start=r.seq_start,
        usage=r.usage,
        error=r.error,
        started_at=r.started_at,
        ended_at=r.ended_at,
    )


def task_from_domain(t: Task) -> TaskOut:
    return TaskOut(
        id=t.id,
        type=t.type,
        status=t.status.value,
        session_id=t.session_id,
        agent_id=t.agent_id,
        attempt_count=t.attempt_count,
        active_run_id=t.active_run_id,
        created_at=t.created_at,
    )


def task_detail_from_domain(t: Task) -> TaskDetailOut:
    return TaskDetailOut(
        id=t.id,
        type=t.type,
        status=t.status.value,
        session_id=t.session_id,
        agent_id=t.agent_id,
        attempt_count=t.attempt_count,
        active_run_id=t.active_run_id,
        created_at=t.created_at,
        payload=t.payload,
        result=t.result,
        error=t.error,
        runs=[run_from_domain(r) for r in t.runs],
    )

"""L2 网关 DTO：Task / Run（02 篇 §6——与领域模型严格分离，转换函数同文件）。

铁律：extra="forbid"、snake_case、只数据无行为；api/01 §5.2 tasks 行契约。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from services.agent.domain.model.task import Run, Task


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


class TaskDetailOut(TaskOut):
    model_config = ConfigDict(extra="forbid")
    payload: dict
    result: dict | None = None
    error: str | None = None
    runs: list[RunOut]


class TaskListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[TaskOut]
    offset: int
    limit: int


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
        payload=t.payload,
        result=t.result,
        error=t.error,
        runs=[run_from_domain(r) for r in t.runs],
    )

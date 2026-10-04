# tests/agent/test_orphan_recovery.py
"""孤儿 running Run 超时回收测试（H-0c ①，2026-09-29 批；评审行 19/20 判定）。

watchdog 分工收敛：进程死=本 sweep 兜底（updated_at 悬挂超阈值→run.fail 5006+
run.orphan_recovered 审计行+既有重试监督衔接）；运行中 hang=duration_s 预算兜底（内核
A4，不在本面）。时钟桩驱动悬挂判定（standards：测试禁真随机/禁睡钟）。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from services.agent.business.task_worker import TaskRunWorker
from services.agent.data.repo_impl.task_poller import WorkerClaim
from services.agent.domain.model.task import RunRetryPolicy, RunStatus, Task, TaskStatus

_TENANT = uuid.uuid4()
_NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)


class ClockStub:
    """时钟桩：worker/轮询侧悬挂判定的可注入时钟。"""

    def __init__(self, now: datetime = _NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class FakeTaskRepo:
    def __init__(self) -> None:
        self.tasks: dict[uuid.UUID, Task] = {}
        self.events: dict[uuid.UUID, list[Any]] = {}
        self.subrun_updates: list[tuple[uuid.UUID, str]] = []  # (run_id, status)：R6 子 Run 行回写记录

    async def get(self, task_id: uuid.UUID) -> Task | None:
        return self.tasks.get(task_id)

    async def save(self, task: Task) -> None:
        self.tasks[task.id] = task

    async def append_event(self, task_id: uuid.UUID, event: Any) -> int:
        self.events.setdefault(task_id, []).append(event)
        return len(self.events[task_id])

    async def list_events(self, task_id: uuid.UUID, **_: Any) -> list[Any]:
        return list(self.events.get(task_id, []))

    async def update_subrun_status(self, run_id: uuid.UUID, status: Any) -> bool:
        """R1 独立写入口桩（40 篇 §8 R6：撕裂子 Run 行回写由 worker 依据合成行驱动）。"""
        self.subrun_updates.append((run_id, str(getattr(status, "value", status))))
        return True


class FakeTx:
    def __init__(self, tasks: FakeTaskRepo) -> None:
        self.tasks = tasks

    async def __aenter__(self) -> FakeTx:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeUow:
    def __init__(self) -> None:
        self.task_repo = FakeTaskRepo()

    def for_tenant(self, tenant_id: uuid.UUID) -> FakeTx:
        return FakeTx(self.task_repo)


class FakeOrphanPoller:
    """轮询桩：find_orphans 按时钟桩 + 各 run 最后刷新时刻判悬挂（RunQueuePoller 同语义）。"""

    def __init__(self, repo: FakeTaskRepo, clock: ClockStub) -> None:
        self._repo = repo
        self._clock = clock
        self.run_updated_at: dict[uuid.UUID, datetime] = {}
        self.forced: list[WorkerClaim] | None = None  # 注入 TOCTOU 形态（查询后状态已变）
        self.next_queue: list[WorkerClaim] = []
        self.find_calls = 0

    async def next_work(self) -> WorkerClaim | None:
        return self.next_queue.pop(0) if self.next_queue else None

    async def find_orphans(self, *, older_than_s: float, **_: Any) -> list[WorkerClaim]:
        self.find_calls += 1
        if self.forced is not None:
            return self.forced
        now = self._clock()
        out: list[WorkerClaim] = []
        for task in self._repo.tasks.values():
            if task.status is not TaskStatus.RUNNING or task.active_run_id is None:
                continue
            run = next((r for r in task.runs if r.id == task.active_run_id), None)
            updated = self.run_updated_at.get(task.active_run_id) if run is not None else None
            if run is None or run.status is not RunStatus.RUNNING or updated is None:
                continue
            hang_s = (now - updated).total_seconds()
            if hang_s >= older_than_s:
                out.append(
                    WorkerClaim(
                        kind="orphan",
                        tenant_id=task.tenant_id,
                        task_id=task.id,
                        run_id=task.active_run_id,
                        hang_s=round(hang_s, 3),
                    )
                )
        return out


class NoopOrchestrator:
    def stream_chat(self, command: Any) -> Any:
        return self._empty(command)

    async def _empty(self, command: Any) -> Any:  # AsyncIterator 桩：无事件
        return
        yield  # pragma: no cover


def make_orphan_task(*, attempt: int = 1, run_status: RunStatus = RunStatus.RUNNING) -> tuple[Task, uuid.UUID]:
    """悬挂形态构造：task=running + 活跃 Run 卡 running（attempt 可注入耗尽态）。"""
    task = Task(tenant_id=_TENANT, type="chat")
    first = task.start_run()  # attempt=1
    first._transition(RunStatus.RUNNING)
    for _ in range(attempt - 1):
        first.fail({"code": 5001, "message": "模型超时", "retryable": True})
        nxt = task.start_retry_run()
        nxt._transition(RunStatus.RUNNING)
        first = nxt
    if run_status is not RunStatus.RUNNING:
        first._transition(run_status)
    return task, first.id


def make_worker(uow: FakeUow, poller: FakeOrphanPoller) -> TaskRunWorker:
    return TaskRunWorker(
        uow=uow,
        poller=poller,
        orchestrator_provider=lambda: NoopOrchestrator(),
        policy=RunRetryPolicy(base_seconds=0.001, cap_seconds=0.002, jitter_ratio=0.0),
        rng=lambda: 0.5,
        poll_interval_s=0.01,
        orphan_sweep_interval_s=30.0,
        orphan_running_timeout_s=300.0,
    )


def seeded_env(
    attempt: int = 1, stale_s: float = 600.0
) -> tuple[TaskRunWorker, FakeUow, FakeOrphanPoller, Task, ClockStub]:
    uow = FakeUow()
    clock = ClockStub()
    poller = FakeOrphanPoller(uow.task_repo, clock)
    task, run_id = make_orphan_task(attempt=attempt)
    uow.task_repo.tasks[task.id] = task
    poller.run_updated_at[run_id] = clock.now - timedelta(seconds=stale_s)  # 悬挂起点
    return make_worker(uow, poller), uow, poller, task, clock


# ── 回收主路径 ──────────────────────────────────────────────────────────


async def test_超时触发回收_run失败5006_任务保持running_审计行落库():
    worker, uow, _, task, _ = seeded_env(attempt=1, stale_s=600.0)
    assert await worker.sweep_once() == 1
    stored = uow.task_repo.tasks[task.id]
    run = next(r for r in stored.runs if r.status is RunStatus.FAILED)
    assert run.error is not None and run.error["code"] == 5006 and run.error["retryable"] is True
    assert stored.status is TaskStatus.RUNNING  # attempt<3：交既有重试监督（不另建通道）
    [event] = uow.task_repo.events[task.id]
    assert event.event_type == "run.orphan_recovered"
    assert event.data["orphan_recovery"] is True
    assert event.data["hang_s"] == 600.0 and event.data["will_retry"] is True
    assert event.data["run_id"] == str(run.id)


async def test_未超时不动作():
    worker, uow, _, task, _ = seeded_env(stale_s=10.0)  # 10s < 300s 阈值
    assert await worker.sweep_once() == 0
    stored = uow.task_repo.tasks[task.id]
    assert next(r for r in stored.runs if r.id == stored.active_run_id).status is RunStatus.RUNNING
    assert task.id not in uow.task_repo.events


async def test_时钟推进后才触发_时钟未过阈值前不回收():
    worker, uow, poller, task, clock = seeded_env(stale_s=250.0)  # 悬挂 250s < 300s
    assert await worker.sweep_once() == 0
    clock.advance(100.0)  # 时钟桩推进 → 悬挂 350s 越过阈值
    assert await worker.sweep_once() == 1
    assert uow.task_repo.events[task.id]


async def test_attempt耗尽_回收直接落task_failed():
    worker, uow, _, task, _ = seeded_env(attempt=3, stale_s=600.0)
    assert await worker.sweep_once() == 1
    stored = uow.task_repo.tasks[task.id]
    assert stored.status is TaskStatus.FAILED  # 04 §3「Run failed 且重试耗尽」同构终局
    [event] = uow.task_repo.events[task.id]
    assert event.data["will_retry"] is False and event.data["attempt_count"] == 3


async def test_回收后重试衔接_既有监督分支建新run():
    worker, uow, poller, task, _ = seeded_env(attempt=1, stale_s=600.0)
    assert await worker.sweep_once() == 1
    poller.next_queue.append(WorkerClaim(kind="retry", tenant_id=_TENANT, task_id=task.id, run_id=task.active_run_id))
    assert await worker.poll_once() is True  # 既有 _supervise_retry 分支自然衔接
    stored = uow.task_repo.tasks[task.id]
    assert stored.attempt_count == 2 and stored.status is TaskStatus.RUNNING
    assert next(r for r in stored.runs if r.id == stored.active_run_id).status is RunStatus.QUEUED
    types = [e.event_type for e in uow.task_repo.events[task.id]]
    assert "run.orphan_recovered" in types and "run.retry_scheduled" in types


# ── 幂等护栏（TOCTOU：查询后状态已变）───────────────────────────────────


async def test_护栏_活跃指针已替换_跳过回收():
    worker, uow, poller, task, _ = seeded_env(stale_s=600.0)
    stored = uow.task_repo.tasks[task.id]
    stored.active_run_id = uuid.uuid4()  # 已被重试替换：状态机为准
    poller.forced = [WorkerClaim(kind="orphan", tenant_id=_TENANT, task_id=task.id, run_id=stored.active_run_id)]
    assert await worker.sweep_once() == 0
    assert not uow.task_repo.events.get(task.id)


async def test_护栏_run已终态_跳过回收():
    worker, uow, poller, task, _ = seeded_env(stale_s=600.0)
    stored = uow.task_repo.tasks[task.id]
    run = next(r for r in stored.runs if r.id == stored.active_run_id)
    run.fail({"code": 5001, "message": "已由结果汇回写", "retryable": True})
    poller.forced = [WorkerClaim(kind="orphan", tenant_id=_TENANT, task_id=task.id, run_id=run.id)]
    assert await worker.sweep_once() == 0  # run 非 running：幂等护栏拒绝重复回收


async def test_护栏_task已终局_跳过回收():
    worker, uow, poller, task, _ = seeded_env(stale_s=600.0)
    stored = uow.task_repo.tasks[task.id]
    stored.status = TaskStatus.FAILED  # type: ignore[assignment]  # 桩直改（终局残留形态）
    assert await worker.sweep_once() == 0


# ── 常驻接线：run() 循环按节拍触发 sweep ────────────────────────────────


async def test_常驻循环按节拍触发sweep_stop后退出():
    uow = FakeUow()
    clock = ClockStub()
    poller = FakeOrphanPoller(uow.task_repo, clock)
    worker = make_worker(uow, poller)
    worker._orphan_sweep_interval_s = 0.05  # 型内改节拍（仅常驻接线验证，不涉业务分支）
    stop = asyncio.Event()
    runner = asyncio.create_task(worker.run(stop))
    try:
        for _ in range(200):  # 至少两次到点（2×0.05s ≪ 2s 预算）
            if poller.find_calls >= 2:
                break
            await asyncio.sleep(0.01)
        assert poller.find_calls >= 2
    finally:
        stop.set()
        await asyncio.wait_for(runner, timeout=2.0)


async def test_未提供find_orphans面的轮询器_noop():
    class BarePoller:
        async def next_work(self) -> None:
            return None

    worker = TaskRunWorker(
        uow=FakeUow(),
        poller=BarePoller(),
        orchestrator_provider=lambda: None,
        orphan_sweep_interval_s=30.0,
        orphan_running_timeout_s=300.0,
    )
    assert await worker.sweep_once() == 0  # 旧形态/测试桩无孤儿面：no-op 不致死


@pytest.mark.parametrize("stale,expected", [(299.9, 0), (300.0, 1), (600.0, 1)])
async def test_阈值边界_恰好达阈值即回收(stale: float, expected: int):
    worker, _, _, task, _ = seeded_env(stale_s=stale)
    assert await worker.sweep_once() == expected
    assert (task.id in uow_events(worker)) is (expected == 1)


def uow_events(worker: TaskRunWorker) -> dict[uuid.UUID, list[Any]]:
    return worker._uow.task_repo.events  # type: ignore[attr-defined]


# ── 撕裂子 Run 行回写（40 篇 §3.2/§8 R6，2026-10-04 批）───────────────────


async def test_孤儿回收_撕裂子Run行回写cancelled_合成终态行落task_events():
    """崩溃时子 Run 在途：孤儿 sweep 认根 Run，撕裂子 Run 由合成行驱动回写终态（不永久 running）。"""
    worker, uow, poller, task, _clock = seeded_env()
    sub_run_id = uuid.uuid4()
    uow.task_repo.events[task.id] = [
        SimpleNamespace(
            task_id=task.id,
            seq=1,
            event_type="SUBRUN_STARTED",
            data={"sub_run_id": str(sub_run_id), "parent_run_id": str(task.active_run_id), "depth": 1},
        )
    ]
    # Act
    assert await worker.sweep_once() == 1
    # Assert：子 Run 行经 R1 独立写入口定向回写 cancelled；合成 SUBRUN_FINISHED 落 task_events
    assert uow.task_repo.subrun_updates == [(sub_run_id, "cancelled")]
    finished = [e for e in uow.task_repo.events[task.id] if e.event_type == "SUBRUN_FINISHED"]
    assert len(finished) == 1
    assert finished[0].data["sub_run_id"] == str(sub_run_id) and finished[0].data["status"] == "cancelled"


async def test_孤儿回收_无子Run撕裂_零回写():
    worker, uow, _poller, task, _clock = seeded_env()
    uow.task_repo.events[task.id] = [
        SimpleNamespace(task_id=task.id, seq=1, event_type="kernel.planned", data={"steps": [1]})
    ]
    assert await worker.sweep_once() == 1
    assert uow.task_repo.subrun_updates == []

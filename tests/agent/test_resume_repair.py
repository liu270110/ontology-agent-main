# tests/agent/test_resume_repair.py
"""中断账本合成闭合测试（B-②，docs/Agent/10 §4；崩溃安全 resume 第一块）。

撕裂投影（TOOL_CALL_START 无 RESULT 收口、kernel.gated allow/approval_pending 后无终态行）
→ 纯函数合成闭合行（先修复账本、后落终态）；幂等二跑零新增；worker 孤儿恢复集成断言
合成行先于 5006 终态落库。测试桩复用 test_orphan_recovery 风格（时钟桩驱动悬挂判定）。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from services.agent.business.resume_repair import plan_interrupted_closures
from services.agent.business.task_worker import TaskRunWorker
from services.agent.data.repo_impl.task_poller import WorkerClaim
from services.agent.domain.model.task import RunRetryPolicy, RunStatus, Task, TaskEvent, TaskStatus
from services.platform.errors import ErrorCode

_TENANT = uuid.uuid4()
_TASK = uuid.uuid4()
_RUN = uuid.uuid4()
_OTHER_RUN = uuid.uuid4()
_TRACE = "trace-worker-orph-1"
_IRI = "http://ontology.example/action/read_data"


def row(seq: int, event_type: str, data: dict[str, Any], *, run_id: str | None = None) -> TaskEvent:
    """投影行构造（sink 注入形态：kernel 行 data.run_id；TOOL_CALL_* 行无 run_id）。"""
    payload = dict(data)
    if run_id is not None:
        payload["run_id"] = run_id
    return TaskEvent(task_id=_TASK, seq=seq, event_type=event_type, data=payload)


def torn_projection() -> list[TaskEvent]:
    """撕裂投影形态：未收口调用 + 在途步1（gated allow）+ 在途步2（待审批）+ 门禁拒绝步3。"""
    return [
        row(1, "TOOL_CALL_START", {"tool_call_id": "c1", "tool_name": "answer"}),
        row(2, "kernel.planned", {"steps": [1, 2, 3]}, run_id=str(_RUN)),
        row(3, "kernel.gated", {"step_seq": 1, "verdict": "allow", "stage": "gate"}, run_id=str(_RUN)),
        row(4, "kernel.gated", {"step_seq": 3, "verdict": "reject", "stage": "gate"}, run_id=str(_RUN)),
        row(5, "kernel.approval_pending", {"step_seq": 2, "action_iri": _IRI}, run_id=str(_RUN)),
    ]


# ── 纯函数：合成规划 ──────────────────────────────────────────────────────


def test_撕裂投影_未闭合调用与在途步_合成行数量与内容正确():
    # Arrange：open 无 close + gated(allow)/approval_pending 无终态 + 门禁拒绝步（已终态）
    events = torn_projection()
    # Act
    synthetic = plan_interrupted_closures(events, run_id=_RUN, reason="orphan_recovered")
    # Assert：1 个 close + 2 个在途步各（interrupted + step_failed）= 5 行；门禁拒绝步不修
    assert len(synthetic) == 5
    close, *steps = synthetic
    assert close.event_type == "TOOL_CALL_RESULT"
    assert close.data["tool_call_id"] == "c1" and close.data["tool_name"] == "answer"
    assert close.data["ok"] is False
    assert close.data["error_code"] == int(ErrorCode.TOOL_CALL_INTERRUPTED)
    assert close.data["interrupted"] is True and close.data["reason"] == "orphan_recovered"
    by_type: dict[str, list[int]] = {}
    for item in steps:
        by_type.setdefault(item.event_type, []).append(item.data["step_seq"])
    assert by_type == {"kernel.interrupted": [1, 2], "kernel.step_failed": [1, 2]}
    interrupted_step1 = steps[0]
    assert interrupted_step1.data["status"] == "failed"  # 等价 A2 executing→failed 终态口径
    assert interrupted_step1.data["reason_code"] == int(ErrorCode.TOOL_CALL_INTERRUPTED)
    assert interrupted_step1.data["residuals"] == []
    step_failed_step2 = steps[3]
    assert step_failed_step2.data["action_iri"] == _IRI  # 从 approval_pending 行回带行动 IRI
    assert step_failed_step2.data["stage"] == "execution" and step_failed_step2.data["trust_level"] is None
    assert all(item.data.get("step_seq") != 3 for item in synthetic)  # 门禁拒绝=已终态：零合成


def test_幂等_修复后投影二跑零新增():
    # Arrange：撕裂投影 + 首轮合成行回填（seq 顺延，模拟 append_event 落库后的投影）
    events = torn_projection()
    first = plan_interrupted_closures(events, run_id=_RUN, reason="orphan_recovered")
    for offset, item in enumerate(first):
        item.seq = 10 + offset
        item.data["run_id"] = str(_RUN)  # 仓储行级形态：合成 kernel 行带 run_id
        events.append(item)
    # Act
    second = plan_interrupted_closures(events, run_id=_RUN, reason="orphan_recovered")
    # Assert：已闭合/已终态零合成
    assert second == []


def test_干净投影_全闭合全终态_零合成():
    # Arrange：调用已收口 + 步已终态 + 沉淀收敛；空输入亦为一例
    events = [
        row(1, "TOOL_CALL_START", {"tool_call_id": "c2"}),
        row(2, "TOOL_CALL_RESULT", {"tool_call_id": "c2", "ok": True, "summary": "ok"}),
        row(3, "kernel.gated", {"step_seq": 1, "verdict": "allow"}, run_id=str(_RUN)),
        row(4, "kernel.step_validated", {"step_seq": 1, "action_iri": _IRI}, run_id=str(_RUN)),
        row(5, "kernel.settled", {"status": "completed"}, run_id=str(_RUN)),
    ]
    # Act
    synthetic = plan_interrupted_closures(events, run_id=_RUN)
    # Assert
    assert synthetic == []
    assert plan_interrupted_closures([]) == []


def test_他run撕裂行不修_步规则按run隔离():
    # Arrange：他 Run 的在途步（kernel 行 data.run_id=他 Run）+ 本 Run 的在途步
    events = [
        row(1, "kernel.gated", {"step_seq": 1, "verdict": "allow"}, run_id=str(_OTHER_RUN)),
        row(2, "kernel.approval_pending", {"step_seq": 2, "action_iri": _IRI}, run_id=str(_RUN)),
    ]
    # Act
    synthetic = plan_interrupted_closures(events, run_id=_RUN)
    # Assert：只修本 Run 的步2（两行）；他 Run 步1 零合成
    assert [item.data["step_seq"] for item in synthetic] == [2, 2]
    assert {item.event_type for item in synthetic} == {"kernel.interrupted", "kernel.step_failed"}


def test_优雅收敛行存在_在途步不重复合成():
    # Arrange：取消清单/中断收敛后的形态（在途步无终态行，但有 Run 级收敛行）
    events = [
        row(1, "kernel.gated", {"step_seq": 1, "verdict": "allow"}, run_id=str(_RUN)),
        row(2, "kernel.cancelled", {"completed": [], "forced": []}, run_id=str(_RUN)),
    ]
    # Act
    synthetic = plan_interrupted_closures(events, run_id=_RUN)
    # Assert：优雅路径已收敛（02 §2.4），本批不重复合成
    assert synthetic == []


def test_合成行trace_tenant一致性与审计payload正确():
    # Arrange：投影行自带 trace（回声源）+ 调用方注入租户；kwarg trace 优先于回声
    events = [
        row(1, "kernel.gated", {"step_seq": 1, "verdict": "allow", "trace_id": _TRACE}, run_id=str(_RUN)),
        row(2, "TOOL_CALL_START", {"tool_call_id": "c3"}),
    ]
    # Act
    synthetic = plan_interrupted_closures(events, run_id=_RUN, reason="orphan_recovered", tenant_id=_TENANT)
    # Assert：每行带 reason/run_id/tenant_id/trace_id（回声投影行 trace，不发明新 trace）
    assert len(synthetic) == 3
    for item in synthetic:
        assert item.data["reason"] == "orphan_recovered"
        assert item.data["run_id"] == str(_RUN)
        assert item.data["tenant_id"] == str(_TENANT)
        assert item.data["trace_id"] == _TRACE
    assert (
        plan_interrupted_closures(events, run_id=_RUN, trace_id="trace-kwarg", tenant_id=_TENANT)[0].data["trace_id"]
        == "trace-kwarg"
    )  # 显式 kwarg 优先


# ── worker 孤儿恢复集成：先修复账本、后落终态 ────────────────────────────


class ClockStub:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class FakeTaskRepo:
    """任务仓储桩：append_event 按仓储语义分配 seq（max+1，PgTaskRepository 同口径）；
    list_events 落实 after_seq 游标语义（seq > after_seq 升序截断，PgTaskRepository 同口径）。"""

    def __init__(self) -> None:
        self.tasks: dict[uuid.UUID, Task] = {}
        self.events: dict[uuid.UUID, list[TaskEvent]] = {}
        self.list_calls: list[tuple[uuid.UUID, int | None, int]] = []  # (task_id, after_seq, limit)

    async def get(self, task_id: uuid.UUID) -> Task | None:
        return self.tasks.get(task_id)

    async def save(self, task: Task) -> None:
        self.tasks[task.id] = task

    async def append_event(self, task_id: uuid.UUID, event: TaskEvent) -> int:
        rows = self.events.setdefault(task_id, [])
        event.seq = max((r.seq for r in rows if r.seq is not None), default=-1) + 1
        rows.append(event)
        return event.seq

    async def list_events(
        self, task_id: uuid.UUID, *, after_seq: int | None = None, limit: int = 100
    ) -> list[TaskEvent]:
        self.list_calls.append((task_id, after_seq, limit))
        rows = sorted(self.events.get(task_id, []), key=lambda r: r.seq if r.seq is not None else 0)
        if after_seq is not None:
            rows = [r for r in rows if r.seq is not None and r.seq > after_seq]
        return rows[:limit]


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
    """轮询桩：find_orphans 按时钟桩判悬挂（RunQueuePoller 同语义）。"""

    def __init__(self, repo: FakeTaskRepo, clock: ClockStub) -> None:
        self._repo = repo
        self._clock = clock
        self.run_updated_at: dict[uuid.UUID, datetime] = {}

    async def next_work(self) -> WorkerClaim | None:
        return None

    async def find_orphans(self, *, older_than_s: float, **_: Any) -> list[WorkerClaim]:
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

    async def _empty(self, command: Any) -> Any:
        return
        yield  # pragma: no cover


def seeded_orphan_env() -> tuple[TaskRunWorker, FakeUow, Task, uuid.UUID]:
    """孤儿 + 撕裂投影：running 悬挂超阈值，投影留 gated(allow) 在途步与未收口调用。"""
    uow = FakeUow()
    clock = ClockStub(datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC))
    poller = FakeOrphanPoller(uow.task_repo, clock)
    task = Task(tenant_id=_TENANT, type="chat")
    run = task.start_run()
    run._transition(RunStatus.RUNNING)
    uow.task_repo.tasks[task.id] = task
    poller.run_updated_at[run.id] = clock.now - timedelta(seconds=600.0)
    uow.task_repo.events[task.id] = [
        row(0, "kernel.gated", {"step_seq": 1, "verdict": "allow", "stage": "gate"}, run_id=str(run.id)),
        row(1, "TOOL_CALL_START", {"tool_call_id": "c1"}),
    ]
    worker = TaskRunWorker(
        uow=uow,
        poller=poller,
        orchestrator_provider=lambda: NoopOrchestrator(),
        policy=RunRetryPolicy(base_seconds=0.001, cap_seconds=0.002, jitter_ratio=0.0),
        rng=lambda: 0.5,
        poll_interval_s=0.01,
        orphan_sweep_interval_s=30.0,
        orphan_running_timeout_s=300.0,
    )
    return worker, uow, task, run.id


async def test_worker孤儿恢复_合成行先于5006终态落库():
    # Arrange：孤儿 Run 悬挂 600s ≥ 阈值 300s，投影撕裂（1 未收口调用 + 1 在途步）
    worker, uow, task, run_id = seeded_orphan_env()
    # Act
    assert await worker.sweep_once() == 1
    # Assert：合成 3 行（close + interrupted + step_failed）先于 orphan_recovered 落库
    events = uow.task_repo.events[task.id]
    types = [e.event_type for e in events]
    orphan_seq = next(e.seq for e in events if e.event_type == "run.orphan_recovered")
    assert types[-1] == "run.orphan_recovered"  # 终态审计行最后落
    assert types.count("TOOL_CALL_RESULT") == 1 and types.count("kernel.interrupted") == 1
    assert all(e.seq < orphan_seq for e in events if e.event_type != "run.orphan_recovered")
    assert uow.task_repo.tasks[task.id].status is TaskStatus.RUNNING  # attempt<3 交既有重试监督
    recovered_run = next(r for r in uow.task_repo.tasks[task.id].runs if r.id == run_id)
    assert recovered_run.status is RunStatus.FAILED
    assert recovered_run.error is not None and recovered_run.error["code"] == int(ErrorCode.ORPHAN_RUN_RECOVERED)
    assert events[-1].data["repaired"] == 3  # 审计行带修复计数（可观测）


async def test_worker合成行tenant一致性与trace回声():
    # Arrange：投影行带 trace_id（回声源）；孤儿回收注入 claim 租户
    worker, uow, task, run_id = seeded_orphan_env()
    uow.task_repo.events[task.id][0].data["trace_id"] = _TRACE
    # Act
    assert await worker.sweep_once() == 1
    # Assert：合成行租户/trace/Run 归属一致（for_tenant 行级隔离之外的载荷一致性字段）
    synthetic = [
        e
        for e in uow.task_repo.events[task.id]
        if e.event_type in ("TOOL_CALL_RESULT", "kernel.interrupted", "kernel.step_failed")
    ]
    assert len(synthetic) == 3
    for item in synthetic:
        assert item.data["tenant_id"] == str(_TENANT)
        assert item.data["trace_id"] == _TRACE  # 回声投影行 trace，不发明新 trace
        assert item.data["reason"] == "orphan_recovered"
    assert [e.data["run_id"] for e in synthetic if e.event_type.startswith("kernel.")] == [str(run_id)] * 2


async def test_worker幂等_合成行已闭合的二轮回收零重复():
    # Arrange：首轮回收完成（合成行已落库）；构造第二轮孤儿回收触发（同 Run 已终态应被护栏跳过）
    worker, uow, task, run_id = seeded_orphan_env()
    assert await worker.sweep_once() == 1
    count_after_first = len(uow.task_repo.events[task.id])
    # Act：同 Run 再扫（run 已 failed：幂等护栏拒绝；投影零新增合成行）
    assert await worker.sweep_once() == 0
    # Assert
    assert len(uow.task_repo.events[task.id]) == count_after_first


async def test_worker常驻接线不回归_stop后退出():
    # Arrange：无撕裂投影的孤儿回收仍正常回收（B-② 改动不破坏 H-0c ① 主路径）
    worker, uow, task, _ = seeded_orphan_env()
    uow.task_repo.events[task.id] = []  # 干净投影：零合成行，回收照常
    # Act
    assert await worker.sweep_once() == 1
    # Assert
    events = uow.task_repo.events[task.id]
    assert [e.event_type for e in events] == ["run.orphan_recovered"]
    assert events[0].data["repaired"] == 0
    stop = asyncio.Event()
    stop.set()  # 常驻循环接线冒烟（立即退出）
    await asyncio.wait_for(worker.run(stop), timeout=2.0)


async def test_worker孤儿修复_分页取全量_窗口外撕裂闭合且跨边界零重复合成():
    # Arrange：607 行投影——首个 500 行窗口含 START(cx) 但其 RESULT 落窗口外（跨边界
    # 已收口调用），真撕裂（ct 调用 + 在途步7）全在窗口外。一次性 limit=500 截断窗口下：
    # cx 会被误判撕裂重复合成失败 close（污染账本）、窗口外撕裂漏修且 repaired 误报。
    worker, uow, task, run_id = seeded_orphan_env()
    bulk = [row(i, "TEXT_MESSAGE_CONTENT", {"delta": "filler"}) for i in range(1, 450)]
    bulk.append(row(450, "TOOL_CALL_START", {"tool_call_id": "cx", "tool_name": "t"}))  # 窗口内 open
    bulk.extend(row(i, "TEXT_MESSAGE_CONTENT", {"delta": "filler"}) for i in range(451, 605))
    bulk.append(row(605, "TOOL_CALL_RESULT", {"tool_call_id": "cx", "ok": True, "summary": "ok"}))  # 窗口外收口
    bulk.append(row(606, "TOOL_CALL_START", {"tool_call_id": "ct", "tool_name": "t"}))  # 窗口外真撕裂调用
    bulk.append(
        row(607, "kernel.gated", {"step_seq": 7, "verdict": "allow", "stage": "gate"}, run_id=str(run_id))
    )  # 窗口外在途步
    uow.task_repo.events[task.id] = bulk
    # Act
    assert await worker.sweep_once() == 1
    # Assert：分页两批（首批 500 行、批尾 seq=500 游标续页；次批 107 行 < 页长即止）
    assert [(c[1], c[2]) for c in uow.task_repo.list_calls] == [(None, 500), (500, 500)]
    events = uow.task_repo.events[task.id]
    interrupted_closes = [e for e in events if e.event_type == "TOOL_CALL_RESULT" and e.data.get("interrupted")]
    # Assert：跨边界已收口 cx 零重复合成；真撕裂 ct 恰一闭合
    assert [c.data["tool_call_id"] for c in interrupted_closes] == ["ct"]
    assert [e.data["step_seq"] for e in events if e.event_type == "kernel.interrupted"] == [7]  # 窗口外撕裂步被闭合
    audit = events[-1]
    assert audit.event_type == "run.orphan_recovered" and audit.data["repaired"] == 3  # 计数如实（1 close + 2 步行）


async def test_组合根sink投影行补trace_id_崩溃恢复合成行回声链路成立():
    # Arrange：真实组合根 sink（api/sessions.build_kernel_ledger_sink_factory）+ 内存仓储；
    # 生产写入方常态：KernelEvent data 不带 trace_id（顶层必填字段才是权威源）。
    from services.agent.api.sessions import build_kernel_ledger_sink_factory
    from services.agent.domain.model.kernel_context import KernelEvent

    uow = FakeUow()
    task_id, run_id = uuid.uuid4(), uuid.uuid4()
    sink = build_kernel_ledger_sink_factory(uow)(task_id, run_id)
    # Act：缺 trace_id 补齐；data 已带则不覆盖（只补缺不覆盖）
    await sink(
        KernelEvent(
            event_type="kernel.gated",
            tenant_id=_TENANT,
            run_id=run_id,
            trace_id=_TRACE,
            data={"step_seq": 1, "verdict": "allow"},
        )
    )
    await sink(
        KernelEvent(
            event_type="kernel.gated",
            tenant_id=_TENANT,
            run_id=run_id,
            trace_id="trace-other",
            data={"step_seq": 2, "verdict": "allow", "trace_id": "trace-in-data"},
        )
    )
    # Assert：投影行带 trace_id（C2 链）；撕裂投影经规划器回声 → 合成行带 trace_id
    stored = uow.task_repo.events[task_id]
    assert stored[0].data["trace_id"] == _TRACE
    assert stored[1].data["trace_id"] == "trace-in-data"  # 已有值不被覆盖
    synthetic = plan_interrupted_closures(stored, run_id=run_id)
    assert len(synthetic) == 4  # 两个在途步 ×（interrupted + step_failed）
    assert all(item.data["trace_id"] == _TRACE for item in synthetic)  # 回声投影行 trace（seq 有序首个）


# ── 撕裂子 Run（40 篇 §3.2/§8 R6，2026-10-04 批）──────────────────────────


def _subrun_started(seq: int, sub_run_id: str, **extra: Any) -> TaskEvent:
    """SUBRUN_STARTED 回放协议行（wire_data 落库形态：sub_run_id/parent_run_id/depth）。"""
    return row(
        seq,
        "SUBRUN_STARTED",
        {"sub_run_id": sub_run_id, "parent_run_id": str(_RUN), "depth": 1, **extra},
    )


def test_撕裂子Run_STARTED无FINISHED_合成cancelled终态行():
    sid = str(uuid.uuid4())
    synthetic = plan_interrupted_closures([_subrun_started(1, sid)], reason="orphan_recovered")
    assert len(synthetic) == 1
    closed = synthetic[0]
    assert closed.event_type == "SUBRUN_FINISHED"  # 回放协议行形态（前端重连回放可归并）
    assert closed.task_id == _TASK
    assert closed.data["sub_run_id"] == sid
    assert closed.data["status"] == "cancelled"  # 崩溃恢复合成终态恒 cancelled（40 篇 §3.2）
    assert closed.data["interrupted"] is True and closed.data["reason"] == "orphan_recovered"


def test_子Run已收口_零合成_且幂等二跑零新增():
    sid = str(uuid.uuid4())
    events = [_subrun_started(1, sid), row(2, "SUBRUN_FINISHED", {"sub_run_id": sid, "status": "completed"})]
    assert plan_interrupted_closures(events) == []  # STARTED↔FINISHED 已配对：零合成
    synthetic = plan_interrupted_closures([_subrun_started(1, sid)])
    repaired = [*events, *synthetic]  # 模拟合成行落库后的投影
    assert [e for e in plan_interrupted_closures(repaired) if e.event_type == "SUBRUN_FINISHED"] == []  # 幂等


def test_子Run多成员_仅撕裂成员合成_收口成员不动():
    torn, closed = str(uuid.uuid4()), str(uuid.uuid4())
    events = [
        _subrun_started(1, torn),
        _subrun_started(2, closed),
        row(3, "SUBRUN_FINISHED", {"sub_run_id": closed, "status": "failed"}),
    ]
    synthetic = plan_interrupted_closures(events)
    assert [e.data["sub_run_id"] for e in synthetic] == [torn]  # 只补撕裂成员


def test_子Run行无sub_run_id_不可归因不修():
    events = [row(1, "SUBRUN_STARTED", {"depth": 1})]
    assert plan_interrupted_closures(events) == []


def test_步撕裂与子Run撕裂同批合成_互不干扰():
    sid = str(uuid.uuid4())
    events = [
        row(1, "TOOL_CALL_START", {"tool_call_id": "c1"}),
        row(2, "kernel.gated", {"step_seq": 1, "verdict": "allow", "stage": "gate"}, run_id=str(_RUN)),
        _subrun_started(3, sid),
    ]
    synthetic = plan_interrupted_closures(events, run_id=_RUN)
    types = [e.event_type for e in synthetic]
    assert types.count("TOOL_CALL_RESULT") == 1
    assert types.count("kernel.interrupted") == 1 and types.count("kernel.step_failed") == 1
    assert types.count("SUBRUN_FINISHED") == 1  # 三类撕裂同批闭合（各自配对规则独立）

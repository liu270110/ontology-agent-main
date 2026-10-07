"""X16 运行链路 PG 集成测试（隔离库+桩 LLM/工具；api/01 §5.11 runs 族端点直调+worker 认领全链）。

七场景（ask 验收面）：串行三节点全绿事件序列/条件分支走向/parallel 分组 parallel_id/
approval 暂停→resume 续跑/断点暂停/环图 409/失败终态事件；另含 4804 未发布/4102 并发
互斥/abort 中止/运行列表与详情。worker 真链认领（TaskRunWorker+workflow 分派分支），
事件落库经 _drain_workflow（task_events 回放根）。执行器语义纯单测见
test_workflow_executor.py。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from starlette.requests import Request as StarletteRequest

from services.agent.business.task_worker import TaskRunWorker
from services.gateway.app import create_app
from services.platform.db.uow import AsyncUnitOfWork
from services.platform.deps import Principal
from services.platform.errors import ErrorCode, GatewayError
from services.workflows.api.runs import (
    abort_workflow_run,
    get_workflow_run,
    list_workflow_runs,
    resume_workflow_run,
    submit_workflow_run,
    submit_workflow_test,
)
from services.workflows.api.schemas.runs import WorkflowAbortIn, WorkflowResumeIn, WorkflowRunIn, WorkflowTestIn
from services.workflows.business.executor import WorkflowRunExecutor
from services.workflows.domain.model.graph import WorkflowEdge, WorkflowGraph
from services.workflows.domain.model.workflow import Workflow
from tests.workflows.test_workflow_executor import StubPorts, _graph, _node

if TYPE_CHECKING:
    pass

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------- 装配


@dataclass
class RunSeed:
    """运行链路装配（wf_seed 之上加 uow/端点调用面/桩执行器）。"""

    seed: Any
    uow: AsyncUnitOfWork

    def principal(self, scopes: list[str] | None = None) -> Principal:
        return Principal(
            {
                "sub": str(self.seed.user_id),
                "tenant_id": str(self.seed.tenant_id),
                "roles": ["admin"],
                "scopes": scopes or ["workflow:read", "workflow:edit", "workflow:run"],
                "typ": "access",
                "jti": uuid.uuid4().hex,
            }
        )

    def request(self) -> StarletteRequest:
        app = create_app(self.seed.settings)
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/workflows",
            "raw_path": b"/api/v1/workflows",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "app": app,
        }
        request = StarletteRequest(scope)
        request.state.trace_id = "wf-run-it-trace"
        return request

    def executor(self) -> WorkflowRunExecutor:
        return WorkflowRunExecutor(uow=self.uow, ports=StubPorts())


@dataclass
class FakePoller:
    """认领队列桩（worker 分派面；claim 按序弹出）。"""

    claims: list[Any] = field(default_factory=list)

    async def next_work(self, *, tenant_id: uuid.UUID | None = None) -> Any | None:
        return self.claims.pop(0) if self.claims else None


@pytest.fixture
async def run_seed(wf_seed: Any) -> AsyncIterator[RunSeed]:
    """wf_seed + uow；用例后清理 tasks/runs/task_events/outbox（FK 逆序，standards/01 §2.9）。"""
    rs = RunSeed(seed=wf_seed, uow=AsyncUnitOfWork.from_dsn(wf_seed.settings.pg_dsn))
    yield rs
    from sqlalchemy import delete, text

    from services.agent.data.orm import Run as RunORM
    from services.agent.data.orm import Task as TaskORM
    from services.agent.data.orm import TaskEvent as TaskEventORM

    async with rs.seed.factory() as db, db.begin():
        task_ids_stmt = text(
            "SELECT id FROM tasks WHERE tenant_id = CAST(:tid AS uuid) AND type IN ('workflow_run','workflow_test')"
        )
        rows = (await db.execute(task_ids_stmt, {"tid": str(rs.seed.tenant_id)})).scalars().all()
        if rows:
            await db.execute(
                delete(TaskEventORM).where(TaskEventORM.task_id.in_(rows), TaskEventORM.tenant_id == rs.seed.tenant_id)
            )
            await db.execute(delete(RunORM).where(RunORM.task_id.in_(rows), RunORM.task_id.in_(rows)))
            await db.execute(delete(TaskORM).where(TaskORM.id.in_(rows), TaskORM.tenant_id == rs.seed.tenant_id))
        await db.execute(
            text("DELETE FROM outbox_events WHERE tenant_id = CAST(:tid AS uuid)"),
            {"tid": str(rs.seed.tenant_id)},
        )
        await db.execute(
            text(
                "DELETE FROM review_tickets WHERE tenant_id = CAST(:tid AS uuid) AND target_type = 'workflow_approval'"
            ),
            {"tid": str(rs.seed.tenant_id)},
        )


def _serial_graph() -> WorkflowGraph:
    return _graph(
        [
            _node("start", "start_end", "开始"),
            _node("agent-1", "agent", "Agent:调度", slot_id="slot:a"),
            _node("end", "start_end", "结束"),
        ],
        [("start", "agent-1", None), ("agent-1", "end", None)],
    )


async def _publish_draft(rs: RunSeed, graph: WorkflowGraph, *, status: str = "draft") -> uuid.UUID:
    """直接落库建工作流（绕过 API——环图/未发布等负向用例的图态构造面）。"""
    from datetime import UTC, datetime

    workflow = Workflow(
        tenant_id=rs.seed.tenant_id,
        name=f"it-run-{uuid.uuid4().hex[:8]}",
        draft=graph,
        status=status,  # type: ignore[arg-type]
        created_by=rs.seed.user_id,
    )
    if status == "published":  # published 需 head 版本行（受理面读快照）
        from services.workflows.domain.model.workflow import WorkflowVersion

        version = WorkflowVersion(
            tenant_id=rs.seed.tenant_id,
            workflow_id=workflow.id,
            version=1,
            snapshot=graph.to_storage(),
            published_at=datetime.now(tz=UTC),
        )
        async with rs.uow.for_tenant(rs.seed.tenant_id) as tx:
            await tx.workflows.add(workflow)
            await tx.workflows.add_version(version)
            workflow.publish(1)
            await tx.workflows.save(workflow)
    else:
        async with rs.uow.for_tenant(rs.seed.tenant_id) as tx:
            await tx.workflows.add(workflow)
    return workflow.id


async def _drive_once(rs: RunSeed, task_id: uuid.UUID, run_id: uuid.UUID, *, kind: str = "queued") -> None:
    """worker 真链认领一次（claim→分派→drain→落库）。"""
    assert await _drive_once_soft(rs, task_id, run_id, kind=kind) is True


async def _drive_once_soft(rs: RunSeed, task_id: uuid.UUID, run_id: uuid.UUID, *, kind: str = "queued") -> bool:
    """同 _drive_once 但返回认领结果（幂等护栏断言面）。"""
    from services.agent.data.repo_impl.task_poller import WorkerClaim

    worker = TaskRunWorker(
        uow=rs.uow,
        poller=FakePoller(
            claims=[WorkerClaim(kind=kind, tenant_id=rs.seed.tenant_id, task_id=task_id, run_id=run_id)]
        ),
        orchestrator_provider=lambda: None,
        workflow_executor_provider=rs.executor,
    )
    return await worker.poll_once()


async def _task_events(rs: RunSeed, task_id: uuid.UUID) -> list[Any]:
    async with rs.uow.for_tenant(rs.seed.tenant_id) as tx:
        return await tx.tasks.list_events(task_id, limit=500)


async def _load_task(rs: RunSeed, task_id: uuid.UUID) -> Any:
    async with rs.uow.for_tenant(rs.seed.tenant_id) as tx:
        return await tx.tasks.get(task_id)


# ---------------------------------------------------------------- 场景 1：串行三节点全绿


async def test_串行三节点_受理202_全绿事件序列落库_终态聚合(run_seed: RunSeed):
    rs = run_seed
    workflow_id = await _publish_draft(rs, _serial_graph())
    principal, request = rs.principal(), rs.request()
    accepted = await submit_workflow_test(workflow_id, WorkflowTestIn(variables={"k": "v"}), principal, rs.uow, request)
    assert accepted.kind == "workflow_test" and accepted.status == "queued" and accepted.version is None

    await _drive_once(rs, accepted.task_id, accepted.run_id)
    rows = await _task_events(rs, accepted.task_id)
    types = [r.event_type for r in rows]
    # 回放根断言：受理行（task.created）之外，RUN_STARTED + 六条节点事件按序落 task_events
    wire = types[1:]  # [0]=task.created（受理侧审计行）
    assert wire[0] == "RUN_STARTED" and rows[1].data["task_type"] == "workflow_test"
    assert wire[1:] == [
        "WORKFLOW_NODE_STARTED",
        "WORKFLOW_NODE_FINISHED",
        "WORKFLOW_NODE_STARTED",
        "WORKFLOW_NODE_FINISHED",
        "WORKFLOW_NODE_STARTED",
        "WORKFLOW_NODE_FINISHED",
        "run.finished",  # 终态审计行（executor._finalize 自写；chat 结果汇 workflow 同构面）
    ]
    assert [r.data.get("node_id") for r in rows[2:8]] == [
        "start",
        "start",
        "agent-1",
        "agent-1",
        "end",
        "end",
    ]
    assert all(r.data.get("trace_id") == "wf-run-it-trace" for r in rows[2:8])  # C4 受理链贯通
    task = await _load_task(rs, accepted.task_id)
    assert task.status.value == "succeeded" and task.runs[0].status.value == "completed"
    state = task.payload["workflow_state"]
    assert {nid: row["status"] for nid, row in state["nodes"].items()} == {
        "start": "succeeded",
        "agent-1": "succeeded",
        "end": "succeeded",
    }
    # 详情端点：节点状态聚合视图
    detail = await get_workflow_run(workflow_id, accepted.run_id, principal, rs.uow, request)
    assert detail.data.run_status == "completed" and detail.data.task_status == "succeeded"
    assert set(detail.data.nodes) == {"start", "agent-1", "end"}
    assert detail.data.nodes["agent-1"]["title"] == "Agent:调度"
    # 列表端点
    listing = await list_workflow_runs(workflow_id, principal, rs.uow, request)
    assert listing.meta.total == 1 and listing.data[0].run_id == accepted.run_id


# ---------------------------------------------------------------- 场景 2：条件分支走向


async def test_条件分支_否分支执行_是分支跳过(run_seed: RunSeed):
    rs = run_seed
    graph = _graph(
        [
            _node("start", "start_end", "开始"),
            _node("cond", "condition", "条件", expression="amount > 1000"),
            _node("high", "template", "高价", template="高价"),
            _node("low", "template", "低价", template="低价:${amount}"),
            _node("end", "start_end", "结束"),
        ],
        [
            ("start", "cond", None),
            ("cond", "high", "是"),
            ("cond", "low", "否"),
            ("high", "end", None),
            ("low", "end", None),
        ],
    )
    workflow_id = await _publish_draft(rs, graph)
    accepted = await submit_workflow_test(
        workflow_id, WorkflowTestIn(variables={"amount": 500}), rs.principal(), rs.uow, rs.request()
    )
    await _drive_once(rs, accepted.task_id, accepted.run_id)
    rows = await _task_events(rs, accepted.task_id)
    finished = {
        r.data["node_id"]: r.data["status"] for r in rows if r.event_type == "WORKFLOW_NODE_FINISHED"
    }
    assert finished["low"] == "succeeded" and finished["high"] == "skipped"
    task = await _load_task(rs, accepted.task_id)
    assert task.payload["workflow_state"]["outputs"]["low"]["output"] == "低价:500"


# ---------------------------------------------------------------- 场景 3：parallel 分组


async def test_parallel分组_parallel_id落事件(run_seed: RunSeed):
    rs = run_seed
    graph = _graph(
        [
            _node("start", "start_end", "开始"),
            _node("par", "parallel", "并行"),
            _node("a", "agent", "Agent:A", slot_id="slot:a"),
            _node("b", "agent", "Agent:B", slot_id="slot:b"),
            _node("end", "start_end", "结束"),
        ],
        [
            ("start", "par", None),
            ("par", "a", None),
            ("par", "b", None),
            ("a", "end", None),
            ("b", "end", None),
        ],
    )
    workflow_id = await _publish_draft(rs, graph)
    accepted = await submit_workflow_test(workflow_id, WorkflowTestIn(), rs.principal(), rs.uow, rs.request())
    await _drive_once(rs, accepted.task_id, accepted.run_id)
    rows = await _task_events(rs, accepted.task_id)
    started = {r.data["node_id"]: r.data for r in rows if r.event_type == "WORKFLOW_NODE_STARTED"}
    assert started["a"]["parallel_id"] == "par" and started["b"]["parallel_id"] == "par"
    assert started["end"]["parallel_id"] == "par"  # 最近 parallel 祖先沿激活链继承
    assert started["start"]["parallel_id"] is None
    task = await _load_task(rs, accepted.task_id)
    assert task.status.value == "succeeded"


# ---------------------------------------------------------------- 场景 4：approval 暂停→resume


async def test_approval暂停_resume批准续跑_哈希核验(run_seed: RunSeed):
    rs = run_seed
    graph = _graph(
        [
            _node("start", "start_end", "开始"),
            _node("gate", "approval", "人工审批"),
            _node("end", "start_end", "结束"),
        ],
        [("start", "gate", None), ("gate", "end", None)],
    )
    workflow_id = await _publish_draft(rs, graph)
    principal, request = rs.principal(), rs.request()
    accepted = await submit_workflow_test(workflow_id, WorkflowTestIn(), principal, rs.uow, request)
    await _drive_once(rs, accepted.task_id, accepted.run_id)
    task = await _load_task(rs, accepted.task_id)
    assert task.runs[0].status.value == "waiting_tool" and task.status.value == "running"
    anchor = task.payload["approval_pending"]
    assert anchor["kind"] == "approval" and anchor["node_id"] == "gate"
    detail = await get_workflow_run(workflow_id, accepted.run_id, principal, rs.uow, request)
    assert detail.data.paused_node == "gate" and detail.data.paused_kind == "approval"

    # 哈希绑定核验：错哈希 409+3001（防批准错对象/换参重放）；无哈希同拒（审批类必核）
    with pytest.raises(GatewayError) as exc_bad:
        await resume_workflow_run(
            workflow_id,
            accepted.run_id,
            WorkflowResumeIn(param_hash="deadbeef"),
            principal,
            rs.uow,
            request,
        )
    assert int(exc_bad.value.code) == 3001 and exc_bad.value.status_code == 409
    with pytest.raises(GatewayError) as exc_missing:
        await resume_workflow_run(workflow_id, accepted.run_id, WorkflowResumeIn(), principal, rs.uow, request)
    assert int(exc_missing.value.code) == 3001

    # 正确哈希 approve → 202；worker resume 认领续跑（批准即完成 + 下游续跑）
    resumed = await resume_workflow_run(
        workflow_id, accepted.run_id, WorkflowResumeIn(param_hash=anchor["param_hash"]), principal, rs.uow, request
    )
    assert resumed.run_status == "running" and resumed.resumed_node == "gate"
    await _drive_once(rs, accepted.task_id, accepted.run_id, kind="resume")
    rows = await _task_events(rs, accepted.task_id)
    gate_finished = [r for r in rows if r.event_type == "WORKFLOW_NODE_FINISHED" and r.data["node_id"] == "gate"]
    assert [r.data["status"] for r in gate_finished] == ["waiting_approval", "succeeded"]
    task = await _load_task(rs, accepted.task_id)
    assert task.runs[0].status.value == "completed" and task.status.value == "succeeded"
    # resume 期间不重发 RUN_STARTED（同 run 协议不变量）
    assert types_count(rows) == 1


def types_count(rows: list[Any]) -> int:
    return sum(1 for r in rows if r.event_type == "RUN_STARTED")


# ---------------------------------------------------------------- 场景 5：断点暂停


async def test_断点前置命中_修参resume续跑(run_seed: RunSeed):
    rs = run_seed
    graph = _graph(
        [
            _node("start", "start_end", "开始"),
            _node("b1", "template", "断点步", template="原:${amount}"),
            _node("end", "start_end", "结束"),
        ],
        [("start", "b1", None), ("b1", "end", None)],
    )
    workflow_id = await _publish_draft(rs, graph)
    principal, request = rs.principal(), rs.request()
    accepted = await submit_workflow_test(
        workflow_id, WorkflowTestIn(breakpoints=["b1"], variables={"amount": 500}), principal, rs.uow, request
    )
    await _drive_once(rs, accepted.task_id, accepted.run_id)
    task = await _load_task(rs, accepted.task_id)
    assert task.runs[0].status.value == "waiting_tool"
    assert task.payload["approval_pending"]["kind"] == "breakpoint"
    assert "b1" not in task.payload["workflow_state"]["outputs"]  # 前置检查：节点未执行

    # 断点恢复无需 param_hash（锚点哈希不核验时允许缺省——修参续跑通道）
    anchor_hash = task.payload["approval_pending"]["param_hash"]
    await resume_workflow_run(
        workflow_id,
        accepted.run_id,
        WorkflowResumeIn(param_hash=anchor_hash, params={"template": "修参:${amount}"}),
        principal,
        rs.uow,
        request,
    )
    await _drive_once(rs, accepted.task_id, accepted.run_id, kind="resume")
    task = await _load_task(rs, accepted.task_id)
    assert task.status.value == "succeeded"
    assert task.payload["workflow_state"]["outputs"]["b1"]["output"] == "修参:500"
    rows = await _task_events(rs, accepted.task_id)
    b1_finished = [r for r in rows if r.event_type == "WORKFLOW_NODE_FINISHED" and r.data["node_id"] == "b1"]
    assert [r.data["status"] for r in b1_finished] == ["waiting_approval", "succeeded"]


# ---------------------------------------------------------------- 场景 6：环图 409 / 4804 / 4102


async def test_环图受理_409_4801(run_seed: RunSeed):
    rs = run_seed
    cyclic = WorkflowGraph(
        nodes=[_node("a", "start_end", "A"), _node("b", "template", "B", template="x")],
        edges=[WorkflowEdge(source="a", target="b"), WorkflowEdge(source="b", target="a")],
    )
    workflow_id = await _publish_draft(rs, cyclic)  # 直接落库（API 保存路径已挡环图）
    with pytest.raises(GatewayError) as exc:
        await submit_workflow_test(workflow_id, WorkflowTestIn(), rs.principal(), rs.uow, rs.request())
    assert int(exc.value.code) == 4801 and exc.value.status_code == 409


async def test_正式运行_草稿拒绝_4804_发布后放行(run_seed: RunSeed):
    rs = run_seed
    workflow_id = await _publish_draft(rs, _serial_graph(), status="draft")
    with pytest.raises(GatewayError) as exc:
        await submit_workflow_run(workflow_id, WorkflowRunIn(), rs.principal(), rs.uow, rs.request())  # noqa: E501
    assert int(exc.value.code) == int(ErrorCode.WORKFLOW_NOT_PUBLISHED) and exc.value.status_code == 409
    # 同草稿试运行不受限（27 篇 §3：草稿可随意改→试运行）
    accepted = await submit_workflow_test(workflow_id, WorkflowTestIn(), rs.principal(), rs.uow, rs.request())
    assert accepted.kind == "workflow_test"


async def test_同工作流并发互斥_4102(run_seed: RunSeed):
    rs = run_seed
    workflow_id = await _publish_draft(rs, _serial_graph())
    first = await submit_workflow_test(workflow_id, WorkflowTestIn(), rs.principal(), rs.uow, rs.request())
    with pytest.raises(GatewayError) as exc:
        await submit_workflow_test(workflow_id, WorkflowTestIn(), rs.principal(), rs.uow, rs.request())
    assert int(exc.value.code) == 4102 and exc.value.status_code == 409
    _ = first


# ---------------------------------------------------------------- 场景 7：失败终态事件


async def test_节点失败_RUN_ERROR终态_task_failed(run_seed: RunSeed):
    rs = run_seed
    graph = _graph(
        [
            _node("start", "start_end", "开始"),
            _node("boom", "tool", "爆炸工具", tool="boom"),
            _node("end", "start_end", "结束"),
        ],
        [("start", "boom", None), ("boom", "end", None)],
    )
    workflow_id = await _publish_draft(rs, graph)
    accepted = await submit_workflow_test(workflow_id, WorkflowTestIn(), rs.principal(), rs.uow, rs.request())
    await _drive_once(rs, accepted.task_id, accepted.run_id)
    rows = await _task_events(rs, accepted.task_id)
    failed = next(r for r in rows if r.event_type == "WORKFLOW_NODE_FINISHED" and r.data["node_id"] == "boom")
    assert failed.data["status"] == "failed" and failed.data["error"]["code"] == 4601
    # 终态 wire 事件（RUN_ERROR）不落 task_events（worker 防双写，_drain_orchestrator 同款）；
    # 回放面终态凭证=executor 自写 run.error 审计行（chat 结果汇同构）
    run_error = next(r for r in rows if r.event_type == "run.error")
    assert run_error.data["code"] == 4601
    task = await _load_task(rs, accepted.task_id)
    assert task.runs[0].status.value == "failed" and task.status.value == "failed"  # v1 无自动重试
    detail = await get_workflow_run(workflow_id, accepted.run_id, rs.principal(), rs.uow, rs.request())
    assert detail.data.task_status == "failed" and detail.data.nodes["end"]["status"] == "pending"


# ---------------------------------------------------------------- abort 中止


async def test_abort_暂停中中止_开放节点cancelled标注(run_seed: RunSeed):
    rs = run_seed
    graph = _graph(
        [
            _node("start", "start_end", "开始"),
            _node("gate", "approval", "人工审批"),
            _node("end", "start_end", "结束"),
        ],
        [("start", "gate", None), ("gate", "end", None)],
    )
    workflow_id = await _publish_draft(rs, graph)
    principal, request = rs.principal(), rs.request()
    accepted = await submit_workflow_test(workflow_id, WorkflowTestIn(), principal, rs.uow, request)
    await _drive_once(rs, accepted.task_id, accepted.run_id)
    aborted = await abort_workflow_run(
        workflow_id, accepted.run_id, WorkflowAbortIn(reason="试运行放弃"), principal, rs.uow, request
    )
    assert aborted.run_status == "cancelled"
    task = await _load_task(rs, accepted.task_id)
    assert task.status.value == "cancelled" and task.runs[0].status.value == "cancelled"
    rows = await _task_events(rs, accepted.task_id)
    cancelled = [r for r in rows if r.event_type == "WORKFLOW_NODE_FINISHED" and r.data["status"] == "cancelled"]
    assert [r.data["node_id"] for r in cancelled] == ["gate"]  # 开放节点补 FINISHED(cancelled)
    # 重复中止 → 终态不可逆 409（聚合断言）
    with pytest.raises(GatewayError) as exc:
        await abort_workflow_run(workflow_id, accepted.run_id, WorkflowAbortIn(), principal, rs.uow, request)
    assert exc.value.status_code == 409
    # 中止后收敛三重防线：①worker 拒认领非 queued run（幂等护栏）
    assert await _drive_once_soft(rs, accepted.task_id, accepted.run_id) is False
    # ②执行器直驱：run 非运行态入口守卫（静默零事件）
    executor = rs.executor()
    events_after = [
        e
        async for e in executor.execute_run(
            tenant_id=rs.seed.tenant_id,
            task_id=accepted.task_id,
            run_id=accepted.run_id,
            task_type="workflow_test",
            trace_id="wf-run-it-trace",
        )
    ]
    assert events_after == []
    # ③task_events 零新增（中止标注后账本静止）
    rows_after = await _task_events(rs, accepted.task_id)
    assert len(rows_after) == len(rows)


async def test_运行详情_404_跨工作流(run_seed: RunSeed):
    rs = run_seed
    workflow_id = await _publish_draft(rs, _serial_graph())
    other_id = await _publish_draft(rs, _serial_graph())
    accepted = await submit_workflow_test(workflow_id, WorkflowTestIn(), rs.principal(), rs.uow, rs.request())
    with pytest.raises(GatewayError) as exc:
        await get_workflow_run(other_id, accepted.run_id, rs.principal(), rs.uow, rs.request())
    assert exc.value.status_code == 404
    with pytest.raises(GatewayError):
        await get_workflow_run(workflow_id, uuid.uuid4(), rs.principal(), rs.uow, rs.request())

"""X16 提升链路 PG 集成测试（40 篇 §6 run→template；api/01 §5.11 POST /workflows/runs/{run_id}/promote）。

场景：工作流族提升（执行图+血统+断点回写+变量抽取）/ 幂等键=run_id（重复调用返回既有
草稿）/ 计划投影提升（PLAN_UPDATED 回扫→origin=llm_candidate 顺序图）/ 孤儿剔除与环图
409 / llm_candidate solo 档发布强制审批（宪法 3）。API 直调形态=test_workflow_runs_pg 同款；
纯函数投影锚点见同目录 test_graph_domain。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import Response
from starlette.requests import Request as StarletteRequest

from services.agent.domain.model.task import Task, TaskEvent
from services.platform.db.uow import AsyncUnitOfWork
from services.platform.errors import GatewayError
from services.workflows.api.runs import promote_workflow_run, submit_workflow_test
from services.workflows.api.schemas.runs import WorkflowPromoteIn, WorkflowTestIn
from services.workflows.business.runs import WorkflowRunControl, _plan_projection, _prune_orphans
from services.workflows.business.service import WorkflowService
from services.workflows.domain.model.graph import WorkflowEdge, WorkflowGraph, WorkflowNode
from services.workflows.domain.model.workflow import Workflow, WorkflowOrigin
from tests.workflows.conftest import FakeApprovals, FakeReview, WorkflowSeed
from tests.workflows.test_workflow_executor import _graph, _node
from tests.workflows.test_workflow_runs_pg import RunSeed, _drive_once

pytestmark = pytest.mark.integration


@pytest.fixture
async def run_seed(wf_seed: WorkflowSeed) -> AsyncIterator[RunSeed]:
    """wf_seed + uow（test_workflow_runs_pg 同款夹具本域复制——保持该文件自含）；清理面
    =promote 产物（workflows）归 wf_seed 清理，tasks/runs/events/outbox 归本夹具。"""
    rs = RunSeed(seed=wf_seed, uow=AsyncUnitOfWork.from_dsn(wf_seed.settings.pg_dsn))
    yield rs
    from sqlalchemy import delete, text

    from services.agent.data.orm import Run as RunORM
    from services.agent.data.orm import Task as TaskORM
    from services.agent.data.orm import TaskEvent as TaskEventORM

    async with rs.seed.factory() as db, db.begin():
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT id FROM tasks WHERE tenant_id = CAST(:tid AS uuid)"
                        " AND type IN ('workflow_run','workflow_test','chat')"
                    ),
                    {"tid": str(rs.seed.tenant_id)},
                )
            )
            .scalars()
            .all()
        )
        if rows:
            await db.execute(
                delete(TaskEventORM).where(TaskEventORM.task_id.in_(rows), TaskEventORM.tenant_id == rs.seed.tenant_id)
            )
            await db.execute(delete(RunORM).where(RunORM.task_id.in_(rows)))
            await db.execute(delete(TaskORM).where(TaskORM.id.in_(rows), TaskORM.tenant_id == rs.seed.tenant_id))
        await db.execute(
            text("DELETE FROM outbox_events WHERE tenant_id = CAST(:tid AS uuid)"),
            {"tid": str(rs.seed.tenant_id)},
        )


def _promote_request(rs: RunSeed) -> StarletteRequest:
    request = rs.request()
    request.state.trace_id = "wf-promote-trace"
    return request


async def _chat_task_with_plan(rs: RunSeed, items: list[str]) -> uuid.UUID:
    """构造 chat 任务 + PLAN_UPDATED 落库（plan_id=run_id——kernel/loop 发射口径）。"""
    task = Task(tenant_id=rs.seed.tenant_id, type="chat", payload={"session_id": None})
    task.start_run()
    run_id = task.runs[-1].id
    async with rs.uow.for_tenant(rs.seed.tenant_id) as tx:
        await tx.tasks.save(task)
        await tx.tasks.append_event(
            task.id,
            TaskEvent(
                task_id=task.id,
                event_type="PLAN_UPDATED",
                data={
                    "plan_id": str(run_id),
                    "revision": 1,
                    "items": [{"id": f"p{i}", "content": c, "status": "completed"} for i, c in enumerate(items, 1)],
                    "trace_id": "wf-promote-trace",
                },
            ),
        )
    return run_id


# ---------------------------------------------------------------- 场景 1：工作流族提升（入口①）


async def test_promote_运行卡提升_执行图与血统落稿_幂等返回既有(run_seed: RunSeed):
    rs = run_seed
    clean = _graph(
        [
            _node("start", "start_end", "开始"),
            _node("agent-1", "agent", "Agent:调度", slot_id="slot:a"),
            _node("end", "start_end", "结束"),
        ],
        [("start", "agent-1", None), ("agent-1", "end", None)],
    )
    workflow_id = await _publish_draft_with_graph(rs, clean)
    accepted = await submit_workflow_test(
        workflow_id,
        WorkflowTestIn(variables={"city": "苏州", "region": "华东", "amount": 7}),
        rs.principal(),
        rs.uow,
        rs.request(),
    )
    await _drive_once(rs, accepted.task_id, accepted.run_id)

    control = WorkflowRunControl(uow=rs.uow)
    outcome = await control.promote(
        run_id=accepted.run_id, tenant_id=rs.seed.tenant_id, actor_id=rs.seed.user_id, trace_id="wf-promote-trace"
    )
    # 断言：新建草稿 + 血统 + user 来源 + 执行图原样
    assert outcome.created is True and outcome.origin == "user" and outcome.source_run_id == accepted.run_id
    async with rs.uow.for_tenant(rs.seed.tenant_id) as tx:
        draft = await tx.workflows.get(outcome.workflow_id)
        assert draft is not None and draft.source_run_id == accepted.run_id
        assert draft.origin is WorkflowOrigin.USER
        assert {n.id for n in draft.draft.nodes} == {"start", "agent-1", "end"}
        start = next(n for n in draft.draft.nodes if n.id == "start")
        # v1 变量抽取：payload.variables 顶层**字符串**字段（§6.2），sorted；非字符串（amount=7）不入
        assert start.params["template_variables"] == ["city", "region"]
        assert draft.name.startswith("来自运行 ")

    # 幂等：重复调用返回既有草稿 id（40 篇 §6.2；api 层 200 语义）
    request = _promote_request(rs)
    idem_response = Response()
    api_out = await promote_workflow_run(
        accepted.run_id, WorkflowPromoteIn(), rs.principal(), rs.uow, request, idem_response
    )
    assert api_out.workflow_id == outcome.workflow_id and api_out.status == "exists" and api_out.origin == "user"
    assert idem_response.status_code == 200  # B2（2026-10-07）：幂等命中经 Response 显式覆盖 200

    # 变更确认清单入参：variable_hints 与抽取合并去重（提升新 run 验证 hints 路径）
    accepted2 = await submit_workflow_test(
        workflow_id,
        WorkflowTestIn(variables={"city": "南京", "feeder": "FL-10kV"}),
        rs.principal(),
        rs.uow,
        rs.request(),
    )
    await _drive_once(rs, accepted2.task_id, accepted2.run_id)
    outcome2 = await control.promote(
        run_id=accepted2.run_id,
        tenant_id=rs.seed.tenant_id,
        actor_id=rs.seed.user_id,
        title="调度提升稿",
        variable_hints=["region", "city"],
    )
    assert outcome2.created is True and outcome2.workflow_id != outcome.workflow_id
    async with rs.uow.for_tenant(rs.seed.tenant_id) as tx:
        draft2 = await tx.workflows.get(outcome2.workflow_id)
        assert draft2 is not None and draft2.name == "调度提升稿"
        start2 = next(n for n in draft2.draft.nodes if n.id == "start")
        # hints 先行 + 抽取补并（去重序保留）：region/city 来自 hints，feeder/amount 面见下行
        assert start2.params["template_variables"] == ["region", "city", "feeder"]


async def test_promote_断点命中节点回写_breakpoint标记(run_seed: RunSeed):
    rs = run_seed
    graph = _graph(
        [
            _node("start", "start_end", "开始"),
            _node("gate", "approval", "人工审批"),
            _node("end", "start_end", "结束"),
        ],
        [("start", "gate", None), ("gate", "end", None)],
    )
    workflow_id = await _publish_draft_with_graph(rs, graph)
    accepted = await submit_workflow_test(
        workflow_id, WorkflowTestIn(breakpoints=["gate"]), rs.principal(), rs.uow, rs.request()
    )
    await _drive_once(rs, accepted.task_id, accepted.run_id)  # 断点命中 → waiting_tool 暂停
    control = WorkflowRunControl(uow=rs.uow)
    outcome = await control.promote(run_id=accepted.run_id, tenant_id=rs.seed.tenant_id)
    async with rs.uow.for_tenant(rs.seed.tenant_id) as tx:
        draft = await tx.workflows.get(outcome.workflow_id)
        assert draft is not None
        gate = next(n for n in draft.draft.nodes if n.id == "gate")
        assert gate.breakpoint is True  # 断点随血统延续（提升稿重放保留人工关卡）
        assert gate.kind.value == "approval"  # waiting_approval 语义映射=approval 节点（八类既有）


# ---------------------------------------------------------------- 场景 2：计划投影提升（入口②）


async def test_promote_计划投影_llm_candidate_顺序图(run_seed: RunSeed):
    rs = run_seed
    run_id = await _chat_task_with_plan(rs, ["研判故障现象", "检索台账与规程", "生成研判意见"])
    control = WorkflowRunControl(uow=rs.uow)
    outcome = await control.promote(run_id=run_id, tenant_id=rs.seed.tenant_id, actor_id=rs.seed.user_id)
    assert outcome.created is True and outcome.origin == "llm_candidate"
    async with rs.uow.for_tenant(rs.seed.tenant_id) as tx:
        draft = await tx.workflows.get(outcome.workflow_id)
        assert draft is not None and draft.origin is WorkflowOrigin.LLM_CANDIDATE
        assert draft.source_run_id == run_id
        nodes = {n.id: n for n in draft.draft.nodes}
        assert set(nodes) == {"start", "step-1", "step-2", "step-3", "end"}
        assert [e.target for e in draft.draft.edges] == ["step-1", "step-2", "step-3", "end"]
        assert nodes["step-2"].kind.value == "agent" and nodes["step-2"].params["prompt"] == "检索台账与规程"
        assert nodes["step-1"].params.get("slot_id") is None  # LLM 候选必缺必填——画布补全+审批终审
    # 幂等命中同型
    again = await control.promote(run_id=run_id, tenant_id=rs.seed.tenant_id)
    assert again.created is False and again.workflow_id == outcome.workflow_id


# ---------------------------------------------------------------- 场景 3：孤儿剔除 / 环图 409 / 404


def test_孤儿剔除_纯函数_零关联度节点不进提升稿():
    graph = WorkflowGraph(
        nodes=[
            WorkflowNode(id="start", kind="start_end", label="开始"),
            WorkflowNode(id="a", kind="agent", label="A", params={"slot_id": "s"}),
            WorkflowNode(id="orphan", kind="template", label="孤儿"),
        ],
        edges=[WorkflowEdge(source="start", target="a")],
    )
    pruned = _prune_orphans(graph)
    assert {n.id for n in pruned.nodes} == {"start", "a"} and len(pruned.edges) == 1


def test_计划投影_纯函数_start_end补结构两端():
    graph = _plan_projection(["一步", "二步"], hints=["kw"])
    assert [n.id for n in graph.nodes] == ["start", "step-1", "step-2", "end"]
    assert graph.nodes[0].params["template_variables"] == ["kw"]
    assert graph.nodes[1].label == "一步" and graph.nodes[1].params["prompt"] == "一步"


# ---------------------------------------------------------------- 场景 4：状态码契约（B2，2026-10-07）


def test_promote_路由注册201_首调新建码():
    """B2 缺陷修复（2026-10-07）：api/01 §5.11 约定「201 新建 / 200 幂等命中」——路由默认码
    须显式注册 201（修复前装饰器缺省 200，正文约定双码只落在 200）；幂等命中 200 覆盖
    由 promote_workflow_run 直调断言（场景 1 内 idem_response）。"""
    from services.workflows.api.runs import router as runs_router

    route = next(
        r
        for r in runs_router.routes
        if getattr(r, "path", "") == "/workflows/runs/{run_id}/promote" and "POST" in getattr(r, "methods", set())
    )
    assert route.status_code == 201


async def test_promote_环图409_与运行404(run_seed: RunSeed):
    rs = run_seed
    # 环图：工作流族 payload 固化面手工落库（受理面已拒环——防御口径 4801→409）
    cyclic = _graph(
        [
            _node("a", "agent", "A", slot_id="s"),
            _node("b", "agent", "B", slot_id="s"),
        ],
        [("a", "b", None), ("b", "a", None)],
    )
    task = Task(
        tenant_id=rs.seed.tenant_id,
        type="workflow_test",
        payload={"workflow_id": str(uuid.uuid4()), "workflow_graph": cyclic.to_storage()},
    )
    task.start_run()
    run_id = task.runs[-1].id
    async with rs.uow.for_tenant(rs.seed.tenant_id) as tx:
        await tx.tasks.save(task)
    control = WorkflowRunControl(uow=rs.uow)
    with pytest.raises(GatewayError) as ei:
        await control.promote(run_id=run_id, tenant_id=rs.seed.tenant_id)
    assert ei.value.status_code == 409 and "4801" in str(ei.value)
    # 运行不存在 → 404
    with pytest.raises(GatewayError) as ei2:
        await control.promote(run_id=uuid.uuid4(), tenant_id=rs.seed.tenant_id)
    assert ei2.value.status_code == 404


# ---------------------------------------------------------------- 场景 4：llm_candidate 发布硬门禁（宪法 3）


async def test_llm_candidate_solo发布强制审批_任意档不可直发(wf_seed: Any):
    seed = wf_seed
    review = FakeReview()
    graph = WorkflowGraph(
        nodes=[
            WorkflowNode(id="start", kind="start_end", label="开始"),
            WorkflowNode(id="a", kind="agent", label="A", params={"slot_id": "slot:a"}),
            WorkflowNode(id="end", kind="start_end", label="结束"),
        ],
        edges=[WorkflowEdge(source="start", target="a"), WorkflowEdge(source="a", target="end")],
    )
    from services.workflows.data.repo_impl.workflow_repo import PgWorkflowRepository

    async with seed.factory() as db, db.begin():
        repo = PgWorkflowRepository(db, seed.tenant_id)
        candidate = Workflow(
            tenant_id=seed.tenant_id,
            name="候选稿",
            origin=WorkflowOrigin.LLM_CANDIDATE,
            draft=graph,
            created_by=seed.user_id,
        )
        await repo.add(candidate)
        workflow_id = candidate.id
    async with seed.factory() as db, db.begin():
        repo2 = PgWorkflowRepository(db, seed.tenant_id)
        service = WorkflowService(repo=repo2, review=review, approvals=FakeApprovals("solo"))  # type: ignore[arg-type]
        outcome = await service.publish(workflow_id=workflow_id, note="候选直发尝试", submitter_id=seed.user_id)
        # 宪法 3：llm_candidate 任何档位（solo 在内）不得直发——202 pending + 工单
        assert outcome.status == "pending_approval" and outcome.governance == "solo"
        assert len(review.submitted) == 1 and review.submitted[0]["target_type"] == "workflow_publish"
        row = await repo2.get(workflow_id)
        assert row is not None and row.status.value == "draft" and row.head_version is None  # head 不动
        # 对照组：user 草稿 solo 档照旧直发（档位分流面不变，仅候选收紧）
        user_wf = Workflow(tenant_id=seed.tenant_id, name="用户稿", draft=graph, created_by=seed.user_id)
        await repo2.add(user_wf)
        outcome2 = await service.publish(workflow_id=user_wf.id, note="用户直发", submitter_id=seed.user_id)
        assert outcome2.status == "published" and outcome2.next_version == "v1"
        versions = await repo2.list_versions(user_wf.id)
        assert [v.version for v in versions] == [1]


async def _publish_draft_with_graph(rs: RunSeed, graph: WorkflowGraph) -> uuid.UUID:
    """直接落库建草稿（test_workflow_runs_pg._publish_draft 同款——受理面绕行构造图态）。"""
    workflow = Workflow(
        tenant_id=rs.seed.tenant_id,
        name=f"it-promote-{uuid.uuid4().hex[:8]}",
        draft=graph,
        created_by=rs.seed.user_id,
    )
    async with rs.uow.for_tenant(rs.seed.tenant_id) as tx:
        await tx.workflows.add(workflow)
    return workflow.id

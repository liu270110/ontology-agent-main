"""X16 执行引擎纯单测（零 IO：Fake UoW+桩端口；事件序列/条件/parallel/断点/审批/失败/超时）。

权威口径：27 篇 §3 八类节点语义 + 40 篇 §4.2 事件 schema（STARTED 先于 FINISHED 的
协议不变量，40 篇 §4.3 #2）；PG 全链集成（受理/恢复/中止端点+worker 认领）见
test_workflow_runs_pg.py。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from services.agent.business.chat_events import ChatEventName
from services.workflows.business.executor import (
    NodeExecutionError,
    NodeOutcome,
    WorkflowRunCommand,
    WorkflowRunExecutor,
    evaluate_condition,
    render_template,
)
from services.workflows.domain.model.graph import WorkflowEdge, WorkflowGraph, WorkflowNode

# ---------------------------------------------------------------- Fake UoW（内存事务面）


class FakeTasksRepo:
    """内存 task 仓储（get/save/append_event 最小面；聚合对象原样存取）。"""

    def __init__(self) -> None:
        self.tasks: dict[uuid.UUID, Any] = {}
        self.events: list[Any] = []

    async def get(self, task_id: uuid.UUID) -> Any:
        return self.tasks.get(task_id)

    async def save(self, task: Any) -> None:
        self.tasks[task.id] = task

    async def append_event(self, task_id: uuid.UUID, event: Any, *, replay_root: bool = False) -> int:
        self.events.append(event)
        return len(self.events)


class FakeTx:
    def __init__(self, repo: FakeTasksRepo) -> None:
        self.tasks = repo

    async def __aenter__(self) -> "FakeTx":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakeUow:
    def __init__(self) -> None:
        self.repo = FakeTasksRepo()

    def for_tenant(self, tenant_id: uuid.UUID) -> FakeTx:
        return FakeTx(self.repo)


class StubPorts:
    """桩端口：agent/tool/retrieval 罐头输出；tool=params.tool=="boom" 时结构化失败。"""

    async def agent_turn(self, node: Any, message: str, ctx: Any, *, run_id: uuid.UUID) -> NodeOutcome:
        return NodeOutcome(output={"answer": f"agent:{node.id}:{message}"}, usage={"input_tokens": 10, "output_tokens": 5})

    async def invoke_tool(self, node: Any, args: dict, ctx: Any, *, run_id: uuid.UUID) -> NodeOutcome:
        if node.params.get("tool") == "boom":
            raise NodeExecutionError(4601, "工具爆炸（桩）")
        return NodeOutcome(output={"tool": node.params.get("tool"), "args": args}, usage={})

    async def retrieve(self, node: Any, query: str, ctx: Any, *, run_id: uuid.UUID) -> NodeOutcome:
        return NodeOutcome(output={"chunks": [query]}, usage={})


def _node(nid: str, kind: str, label: str, **params: Any) -> WorkflowNode:
    return WorkflowNode(id=nid, kind=kind, label=label, params=dict(params))


def _graph(nodes: list[WorkflowNode], edges: list[tuple[str, str, str | None]]) -> WorkflowGraph:
    return WorkflowGraph(
        nodes=nodes, edges=[WorkflowEdge(source=s, target=t, label=label) for s, t, label in edges]
    )


def _make_task(graph: WorkflowGraph, *, kind: str = "workflow_test", breakpoints: list[str] | None = None) -> Any:
    from services.agent.domain.model.task import Task

    task = Task(
        tenant_id=TENANT,
        type=kind,
        payload={
            "workflow_id": str(WF_ID),
            "kind": kind,
            "version": None,
            "breakpoints": breakpoints or [],
            "variables": {"amount": 500, "mode": "fast"},
            "workflow_graph": graph.to_storage(),
        },
    )
    task.start_run()
    task.runs[0].start()  # 模拟 worker 已认领（running）
    return task


TENANT = uuid.uuid4()
WF_ID = uuid.uuid4()


async def _drive(executor: WorkflowRunExecutor, repo: FakeTasksRepo, task: Any, *, kind: str = "workflow_test") -> list[Any]:
    """消费事件流（worker._drain_workflow 同款：全部事件顺序收集）。"""
    command = WorkflowRunCommand(
        tenant_id=TENANT, task_id=task.id, run_id=task.runs[0].id, task_type=kind, trace_id="trace-x16"
    )
    events = [event async for event in executor.execute_run(tenant_id=command.tenant_id, task_id=command.task_id, run_id=command.run_id, task_type=command.task_type, trace_id=command.trace_id)]
    return events


def _names(events: list[Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for event in events:
        if event.name in (ChatEventName.WORKFLOW_NODE_STARTED, ChatEventName.WORKFLOW_NODE_FINISHED):
            out.append((event.name.value, event.data["node_id"]))
        else:
            out.append((event.name.value, ""))
    return out


# ---------------------------------------------------------------- 串行三节点全绿


async def test_串行三节点_全绿事件序列与终态回写():
    graph = _graph(
        [
            _node("start", "start_end", "开始"),
            _node("agent-1", "agent", "Agent:调度", slot_id="slot:a"),
            _node("end", "start_end", "结束"),
        ],
        [("start", "agent-1", None), ("agent-1", "end", None)],
    )
    task = _make_task(graph)
    uow = FakeUow()
    uow.repo.tasks[task.id] = task
    executor = WorkflowRunExecutor(uow=uow, ports=StubPorts())
    events = await _drive(executor, uow.repo, task)

    names = _names(events)
    assert names[0] == ("RUN_STARTED", "")
    assert events[0].data["task_type"] == "workflow_test"
    assert names[-1] == ("RUN_FINISHED", "")
    assert [n for n in names[1:-1]] == [
        ("WORKFLOW_NODE_STARTED", "start"),
        ("WORKFLOW_NODE_FINISHED", "start"),
        ("WORKFLOW_NODE_STARTED", "agent-1"),
        ("WORKFLOW_NODE_FINISHED", "agent-1"),
        ("WORKFLOW_NODE_STARTED", "end"),
        ("WORKFLOW_NODE_FINISHED", "end"),
    ]
    # 协议不变量：同 node STARTED 先于 FINISHED（40 篇 §4.3 #2）
    seq = {nid: [n for n, x in names if x == nid] for nid in ("start", "agent-1", "end")}
    for order in seq.values():
        assert order == sorted(order, key=lambda n: 0 if n.endswith("STARTED") else 1)
    # agent 节点吃到上游输出（start 直通入口变量）；终态回写
    assert task.runs[0].status.value == "completed"
    assert task.status.value == "succeeded"
    state = task.payload["workflow_state"]
    assert state["nodes"]["agent-1"]["status"] == "succeeded"
    assert state["outputs"]["agent-1"]["answer"].startswith("agent:agent-1:")
    finished = [e for e in uow.repo.events if e.event_type == "run.finished"]
    assert finished and finished[0].data["task_type"] == "workflow_test"
    # STARTED 事件 payload 面（40 篇 §4.2）：node_type/title/attempt/parallel_id
    started_agent = next(e for e in events if e.name is ChatEventName.WORKFLOW_NODE_STARTED and e.data["node_id"] == "agent-1")
    assert started_agent.data["node_type"] == "agent"
    assert started_agent.data["title"] == "Agent:调度"
    assert started_agent.data["attempt"] == 1
    assert started_agent.data["parallel_id"] is None
    assert started_agent.data["trace_id"] == "trace-x16"


# ---------------------------------------------------------------- 条件分支走向


async def test_条件分支_表达式真走是分支_否分支跳过传播():
    graph = _graph(
        [
            _node("start", "start_end", "开始"),
            _node("cond", "condition", "条件", expression="amount > 1000"),
            _node("high", "template", "高价", template="高价:${amount}"),
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
    task = _make_task(graph)
    uow = FakeUow()
    uow.repo.tasks[task.id] = task
    executor = WorkflowRunExecutor(uow=uow, ports=StubPorts())
    events = await _drive(executor, uow.repo, task)

    finished = {(e.data["node_id"]): e.data["status"] for e in events if e.name is ChatEventName.WORKFLOW_NODE_FINISHED}
    assert finished["low"] == "succeeded"
    assert finished["high"] == "skipped"  # 表达式假（500>1000=False）→ 否分支执行，是分支跳过
    assert finished["cond"] == "succeeded" and finished["end"] == "succeeded"
    assert task.runs[0].status.value == "completed"
    # 表达式假（500>1000=False）→ 否分支执行，其输出进 end
    assert task.payload["workflow_state"]["outputs"]["low"]["output"] == "低价:500"
    assert "high" not in task.payload["workflow_state"]["outputs"]


async def test_条件结构化when对象优先():
    node = _node("c1", "condition", "条件", when={"all": [{"path": "amount", "op": ">", "value": 100}]})
    assert evaluate_condition(node, {"amount": 500}) is True
    node_any = _node("c2", "condition", "条件", when={"any": [{"path": "amount", "op": "<", "value": 100}]})
    assert evaluate_condition(node_any, {"amount": 500}) is False


def test_受限表达式求值器_比较布尔组合_禁eval面():
    node = _node("c", "condition", "条件", expression="amount > 100 and mode == 'fast' or not flag")
    assert evaluate_condition(node, {"amount": 500, "mode": "fast", "flag": False}) is True
    assert evaluate_condition(node, {"amount": 50, "mode": "slow", "flag": True}) is False
    assert evaluate_condition(_node("d", "condition", "d", expression="nodes.a.score >= 0.5"), {"nodes": {"a": {"score": 0.7}}}) is True
    with pytest.raises(NodeExecutionError):
        evaluate_condition(_node("e", "condition", "e", expression="__import__('os')"), {})


def test_模板渲染_点路径与未知变量显性失败():
    assert render_template("A:${amount}-B:${nodes.x.name}", {"amount": 7, "nodes": {"x": {"name": "n"}}}) == "A:7-B:n"
    with pytest.raises(NodeExecutionError):
        render_template("${missing}", {})


# ---------------------------------------------------------------- parallel 分组


async def test_parallel分支组并行_parallel_id正确():
    graph = _graph(
        [
            _node("start", "start_end", "开始"),
            _node("par", "parallel", "并行"),
            _node("a", "agent", "Agent:A", slot_id="slot:a"),
            _node("b", "agent", "Agent:B", slot_id="slot:b"),
            _node("join", "template", "汇聚", template="${nodes.a.answer}|${nodes.b.answer}"),
            _node("end", "start_end", "结束"),
        ],
        [
            ("start", "par", None),
            ("par", "a", None),
            ("par", "b", None),
            ("a", "join", None),
            ("b", "join", None),
            ("join", "end", None),
        ],
    )
    task = _make_task(graph)
    uow = FakeUow()
    uow.repo.tasks[task.id] = task
    executor = WorkflowRunExecutor(uow=uow, ports=StubPorts())
    events = await _drive(executor, uow.repo, task)

    started = {e.data["node_id"]: e.data for e in events if e.name is ChatEventName.WORKFLOW_NODE_STARTED}
    assert started["a"]["parallel_id"] == "par" and started["b"]["parallel_id"] == "par"
    assert started["join"]["parallel_id"] == "par"  # 最近 parallel 祖先沿激活链继承
    assert started["start"]["parallel_id"] is None
    # 分支组同波并发：a/b 的 STARTED 相邻发射（波首发），join 等双亲到齐后单波执行
    ids = [nid for _, nid in _names(events)]
    assert ids.index("a") < ids.index("join") and ids.index("b") < ids.index("join")
    join_output = task.payload["workflow_state"]["outputs"]["join"]["output"]
    assert join_output.startswith("agent:a:") and "|agent:b:" in join_output  # 双亲出参都到齐
    assert task.runs[0].status.value == "completed"


# ---------------------------------------------------------------- 审批暂停→resume 续跑


async def test_approval节点_落锚点暂停_resume批准即完成续跑():
    graph = _graph(
        [
            _node("start", "start_end", "开始"),
            _node("gate", "approval", "人工审批"),
            _node("end", "start_end", "结束"),
        ],
        [("start", "gate", None), ("gate", "end", None)],
    )
    task = _make_task(graph)
    uow = FakeUow()
    uow.repo.tasks[task.id] = task
    executor = WorkflowRunExecutor(uow=uow, ports=StubPorts())
    events = await _drive(executor, uow.repo, task)

    # 暂停：FINISHED(waiting_approval)+锚点+waiting_tool 行态（合法长等，无终态事件）
    assert ("WORKFLOW_NODE_FINISHED", "gate") in _names(events)
    fin = next(e for e in events if e.name is ChatEventName.WORKFLOW_NODE_FINISHED and e.data["node_id"] == "gate")
    assert fin.data["status"] == "waiting_approval"
    assert task.runs[0].status.value == "waiting_tool"
    assert task.status.value == "running"
    assert events[-1].name is not ChatEventName.RUN_FINISHED
    anchor = task.payload["approval_pending"]
    assert anchor["kind"] == "approval" and anchor["node_id"] == "gate" and anchor["param_hash"]
    # 模拟 resume 端点+worker 取票：锚点消费、run.start()（waiting_tool→running）
    task.payload.pop("approval_pending")
    task.runs[0].start()

    events2 = await _drive(executor, uow.repo, task)
    fin2 = [e for e in events2 if e.name is ChatEventName.WORKFLOW_NODE_FINISHED and e.data["node_id"] == "gate"]
    assert fin2 and fin2[0].data["status"] == "succeeded"  # 批准即完成（不再重发 RUN_STARTED）
    assert all(e.name is not ChatEventName.RUN_STARTED for e in events2)
    assert events2[-1].name is ChatEventName.RUN_FINISHED
    assert task.runs[0].status.value == "completed" and task.status.value == "succeeded"


# ---------------------------------------------------------------- 断点暂停（workflow_test）


async def test_断点前置命中_暂停标注_resume修参续跑():
    graph = _graph(
        [
            _node("start", "start_end", "开始"),
            _node("b1", "template", "断点步", template="值:${amount}"),
            _node("end", "start_end", "结束"),
        ],
        [("start", "b1", None), ("b1", "end", None)],
    )
    task = _make_task(graph, breakpoints=["b1"])
    uow = FakeUow()
    uow.repo.tasks[task.id] = task
    executor = WorkflowRunExecutor(uow=uow, ports=StubPorts())
    events = await _drive(executor, uow.repo, task)

    fin = next(e for e in events if e.name is ChatEventName.WORKFLOW_NODE_FINISHED and e.data["node_id"] == "b1")
    assert fin.data["status"] == "waiting_approval"  # 断点暂停=waiting_approval 语义标注
    assert task.payload["approval_pending"]["kind"] == "breakpoint"
    assert task.runs[0].status.value == "waiting_tool"
    assert "b1" not in task.payload["workflow_state"]["outputs"]  # 前置检查：未执行
    # resume：锚点消费+修参合入（params_override）+run.start()
    task.payload.pop("approval_pending")
    task.payload["workflow_state"]["params_override"] = {"b1": {"template": "修参:${amount}"}}
    task.runs[0].start()

    events2 = await _drive(executor, uow.repo, task)
    assert events2[-1].name is ChatEventName.RUN_FINISHED
    assert task.payload["workflow_state"]["outputs"]["b1"]["output"] == "修参:500"  # 修参生效
    # 断点不重复命中（breakpoint_hit 护栏）
    assert all(not (e.name is ChatEventName.WORKFLOW_NODE_FINISHED and e.data.get("status") == "waiting_approval") for e in events2)


# ---------------------------------------------------------------- 失败终态/超时


async def test_节点失败_run_failed终态事件_无自动重试():
    graph = _graph(
        [
            _node("start", "start_end", "开始"),
            _node("boom", "tool", "爆炸工具", tool="boom"),
            _node("end", "start_end", "结束"),
        ],
        [("start", "boom", None), ("boom", "end", None)],
    )
    task = _make_task(graph, kind="workflow_run")
    uow = FakeUow()
    uow.repo.tasks[task.id] = task
    executor = WorkflowRunExecutor(uow=uow, ports=StubPorts())
    events = await _drive(executor, uow.repo, task, kind="workflow_run")

    fin = next(e for e in events if e.name is ChatEventName.WORKFLOW_NODE_FINISHED and e.data["node_id"] == "boom")
    assert fin.data["status"] == "failed"
    assert fin.data["error"]["code"] == 4601
    assert events[-1].name is ChatEventName.RUN_ERROR
    assert events[-1].data["code"] == 4601 and events[-1].data["retryable"] is False
    assert task.runs[0].status.value == "failed" and task.runs[0].error["code"] == 4601
    assert task.status.value == "failed"  # v1 无自动重试：task 直接终局（重试监督不接手）
    assert ("WORKFLOW_NODE_STARTED", "end") not in _names(events)  # 下游不再执行


async def test_节点超时_per_node配置钳取():
    class SlowPorts(StubPorts):
        async def agent_turn(self, node: Any, message: str, ctx: Any, *, run_id: uuid.UUID) -> NodeOutcome:
            await asyncio.sleep(0.2)
            return NodeOutcome(output={"answer": "late"})

    graph = _graph(
        [_node("start", "start_end", "开始"), _node("slow", "agent", "慢代理", slot_id="s", timeout_s=0.05)],
        [],
    )
    graph.edges.append(WorkflowEdge(source="start", target="slow"))
    task = _make_task(graph)
    uow = FakeUow()
    uow.repo.tasks[task.id] = task
    executor = WorkflowRunExecutor(uow=uow, ports=SlowPorts(), node_timeout_default_s=30.0)
    events = await _drive(executor, uow.repo, task)

    fin = next(e for e in events if e.name is ChatEventName.WORKFLOW_NODE_FINISHED and e.data["node_id"] == "slow")
    assert fin.data["status"] == "failed" and fin.data["error"]["code"] == 5003
    assert task.runs[0].status.value == "failed"

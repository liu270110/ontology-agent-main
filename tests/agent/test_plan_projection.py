# tests/agent/test_plan_projection.py
"""计划投影用例（40 篇 §4.2 PLAN_UPDATED / §8 R4 v1 最小实现；2026-10-04 步）。

断言目标（R4 验收口径）：
- 规划产出即发整表快照（revision=1，全 pending，plan_id=run_id，payload 过
  PlanUpdatedPayload 确定性校验——宪法 2）；
- 内核步推进时 items 状态推进：pending → in_progress（步开跑）→ completed（步
  validated/finished 终态），revision 严格递增（40 篇 §4.3-4）；
- 串行/并行两条调度路径推进一致；门禁拒绝与预算截断步保持 pending（没跑过不标完成）；
- 中断/取消终局扫描：已开跑未终态项推进 completed（防前端计划卡滞留旋转）；
- 端到端：内核发射 kernel.plan_updated 经 ExecEventTranslator（H-0a observer）产出
  PLAN_UPDATED ChatEvent（转译/落库纪律=exec_events.py）。
"""

from __future__ import annotations

import asyncio

from services.agent.business.chat_events import ChatEvent, ChatEventName
from services.agent.business.exec_events import ExecEventTranslator, PlanUpdatedPayload
from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_context import TaskRef
from tests.agent.conftest import (
    ACTION_IRI,
    FakePlanner,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)

_UUID_T = "00000000-0000-0000-0000-000000000000"


def _plan_events(kernel: AgentKernel) -> list:
    """账本内 kernel.plan_updated 锚点（发射序）。"""
    assert kernel.last_ledger is not None
    return [e for e in kernel.last_ledger.events if e.event_type == "kernel.plan_updated"]


def _statuses(event) -> list[str]:
    return [item["status"] for item in event.data["items"]]


def _item(event, seq: int) -> dict:
    return event.data["items"][seq - 1]  # items 按 seq 升序，id=p{seq}


def _wire_ok(event) -> bool:
    """锚点 data + trace_id（转译器注入，KernelEvent.trace_id）→ 过 wire 载荷确定性校验。

    内核锚点 data 不含 trace_id（exec_events.py 契约：trace_id 取 KernelEvent 顶层），
    此处以补缺形态验证「转译后即合法 PLAN_UPDATED 载荷」（宪法 2 校验点=转译侧）。
    """
    return PlanUpdatedPayload.model_validate({"trace_id": "trace-kernel-test", **event.data}) is not None


# ── 串行路径（单步/多步）─────────────────────────────────────────────────


async def test_规划产出即发整表快照_revision1_全pending():
    tool = FakeTool()
    kernel = AgentKernel(
        make_tool_dispatcher(tool, register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),)),
    )
    task: TaskRef = make_task()
    await kernel.run(task, make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    events = _plan_events(kernel)
    assert len(events) >= 1
    first = events[0]
    # 40 篇 §4.2：plan_id=本次 run 内计划标识（=run_id）；revision=1 起；items 整表
    assert first.data["plan_id"] == str(task.run_id)
    assert first.data["revision"] == 1
    assert first.data["items"] == [
        {"id": "p1", "content": f"步骤 1：{ACTION_IRI.rsplit('/', 1)[-1]}", "status": "pending"}
    ]
    assert _wire_ok(first)  # 转译后载荷过确定性校验（宪法 2）


async def test_步推进三快照_revision严格递增_pending到completed():
    tool = FakeTool()
    kernel = AgentKernel(
        make_tool_dispatcher(tool, register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),)),
    )
    await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    events = _plan_events(kernel)
    assert [e.data["revision"] for e in events] == [1, 2, 3]  # 从 1 严格递增（40 篇 §4.3-4）
    assert [_statuses(e) for e in events] == [["pending"], ["in_progress"], ["completed"]]
    assert _wire_ok(events[-1])


async def test_多步计划_各步独立推进_最终全completed():
    steps = (make_step(seq=1, params={"q": "a"}), make_step(seq=2, params={"q": "b"}))
    tool = FakeTool()
    kernel = AgentKernel(
        make_tool_dispatcher(tool, register_planning_strategy=(FakePlanner(make_candidate(steps)),)),
    )
    await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    events = _plan_events(kernel)
    # 每步两发（开跑/终态）+ 规划一发；两步串行推进序：p1 跑完才轮 p2
    assert [e.data["revision"] for e in events] == [1, 2, 3, 4, 5]
    assert [_statuses(e) for e in events] == [
        ["pending", "pending"],
        ["in_progress", "pending"],
        ["completed", "pending"],
        ["completed", "in_progress"],
        ["completed", "completed"],
    ]
    assert {i["id"] for i in events[-1].data["items"]} == {"p1", "p2"}  # 稳定 id（前端 React key）
    assert _wire_ok(events[-1])


async def test_描述优先_内容取计划步description():
    from services.agent.domain.model.kernel_planning import PlanStep

    step = PlanStep(seq=1, action_iri=ACTION_IRI, required_scopes=("tool.exec",), description="检索停电工单并摘要")
    kernel = AgentKernel(
        make_tool_dispatcher(FakeTool(), register_planning_strategy=(FakePlanner(make_candidate((step,))),)),
    )
    await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    events = _plan_events(kernel)
    assert all(e.data["items"][0]["content"] == "检索停电工单并摘要" for e in events)


# ── 不推进分支（门禁拒/预算截断）─────────────────────────────────────────


async def test_门禁拒绝步保持pending_零变化零发射():
    tool = FakeTool()
    kernel = AgentKernel(
        make_tool_dispatcher(tool, register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),)),
    )
    outcome = await kernel.run(make_task(), make_ctx(scopes=()), budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == "failed" and tool.calls == []  # 门禁拒绝：步未执行
    events = _plan_events(kernel)
    assert [e.data["revision"] for e in events] == [1]  # 仅规划快照；开跑/终态零发射
    assert _statuses(events[0]) == ["pending"]  # 没跑过不标完成（R4 推进纪律）


async def test_预算截断步保持pending_已开跑步推进completed():
    steps = (make_step(seq=1, params={"q": "a"}), make_step(seq=2, params={"q": "b"}))
    tool = FakeTool()
    kernel = AgentKernel(
        make_tool_dispatcher(tool, register_planning_strategy=(FakePlanner(make_candidate(steps)),)),
    )
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=1, duration_s=30))
    assert outcome.status == "failed" and len(tool.calls) == 1  # 步 2 预算截断未执行
    events = _plan_events(kernel)
    assert [e.data["revision"] for e in events] == [1, 2, 3]  # 终局扫描对 pending 步零变化零发射
    assert _statuses(events[-1]) == ["completed", "pending"]


# ── 并行路径（B-① 段执行器推进）─────────────────────────────────────────


def _parallel_step(seq: int):
    from services.agent.domain.model.kernel_planning import PlanStep

    return PlanStep(
        seq=seq,
        action_iri=ACTION_IRI,
        parameters={"q": f"并行{seq}"},
        required_scopes=("tool.exec",),
        parameter_schema={"required": ["q"], "properties": {"q": {"type": "string"}}},
        parallelizable=True,
    )


async def test_并行段_整批推进_一发开跑一发终态():
    steps = tuple(_parallel_step(i) for i in (1, 2, 3))
    tool = FakeTool()
    kernel = AgentKernel(
        make_tool_dispatcher(tool, register_planning_strategy=(FakePlanner(make_candidate(steps)),)),
    )
    await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    events = _plan_events(kernel)
    # 并行段批内合并：规划一发 + 入池整批一发 + 整批终态一发（低频整表快照，40 篇 §4.1）
    assert [e.data["revision"] for e in events] == [1, 2, 3]
    assert [_statuses(e) for e in events] == [
        ["pending", "pending", "pending"],
        ["in_progress", "in_progress", "in_progress"],
        ["completed", "completed", "completed"],
    ]
    assert PlanUpdatedPayload.model_validate({"trace_id": "x", **events[-1].data})

# ── 中断/取消终局扫描（R4 收敛面）───────────────────────────────────────


async def test_取消收敛_终局扫描补发快照_已开跑步推进completed():
    tool = FakeTool(sleep_s=10.0)  # 在途调用：取消传播后步落 cancelled 终态
    kernel = AgentKernel(
        make_tool_dispatcher(tool, register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),)),
    )
    run_task = asyncio.create_task(kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=60)))
    await asyncio.sleep(0.05)  # 让运行进入在途工具调用（步已开跑=in_progress）
    run_task.cancel()
    try:
        await run_task
    except asyncio.CancelledError:
        pass  # 取消语义保留（02 §2.4）：清理完毕后重抛
    events = _plan_events(kernel)
    assert [e.data["revision"] for e in events] == [1, 2, 3]  # 终局扫描补发第三发
    assert _statuses(events[-1]) == ["completed"]  # cancelled 步随终局收敛推进（三态枚举无 failed）


# ── 端到端：内核发射 → ExecEventTranslator → PLAN_UPDATED ChatEvent ────


async def test_端到端_内核发射经observer产出PLAN_UPDATED事件():
    received: list[ChatEvent] = []
    tool = FakeTool()
    task: TaskRef = make_task()
    dispatcher = make_tool_dispatcher(
        tool, register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),)
    )
    dispatcher.register_hook(
        "on_kernel_event",
        ExecEventTranslator(
            task_id=task.task_id,
            session_id=_UUID_T,
            trace_id="trace-plan-e2e",
            on_event=received.append,
        ),
    )
    kernel = AgentKernel(dispatcher)
    await kernel.run(task, make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    plan_updates = [e for e in received if e.name is ChatEventName.PLAN_UPDATED]
    assert [e.data["revision"] for e in plan_updates] == [1, 2, 3]
    assert plan_updates[0].data["plan_id"] == str(task.run_id)
    # §4.2 公共字段：内核 data 无 trace_id → 转译器取 KernelEvent.trace_id（make_ctx 缺省值）优先
    assert plan_updates[0].data["trace_id"] == "trace-kernel-test"
    assert plan_updates[0].trace_id == "trace-kernel-test"
    assert plan_updates[-1].data["items"][0]["status"] == "completed"
    for event in plan_updates:  # 每发载荷均过确定性校验
        assert PlanUpdatedPayload.model_validate(event.data)

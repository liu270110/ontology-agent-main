# tests/agent/test_subrun_events.py
"""子 Run 执行结构事件发射测试（40 篇 §8 R2/R6/R10，2026-10-04 批）。

断言目标（R2 发射点验收口径）：
- spawn→finish 事件序列与 payload 字段（40 篇 §4.2 / api/02 §3 契约形状）；
- 并行批次 index/total 正确（能力层 spawn 透传 + 内核 STARTED 载荷）；
- 终态五值映射（40 篇 §3.2）：运行失败 failed / 产物被拒 rejected_artifact 禁误报
  completed / 超时 timeout / 级联取消 cancelled（R6 补发）；
- 深度超限拒绝（R10）：STARTED 之前结构化拒绝，零事件；血统深度随 TaskRef 下传；
- 内核 run 内派生端到端：发射经父 Run 账本 + H-0a 广播 → ExecEventTranslator 转译
  ChatEvent（session_id 注入）。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

import pytest

from services.agent.business.capabilities.subagent.guards import DerivationWhitelist
from services.agent.business.capabilities.subagent.tools import (
    SUBAGENT_SPAWN_ACTION_IRI,
    BudgetSharePolicy,
    SpawnGroupRegistry,
    SubagentSpawnTool,
)
from services.agent.business.chat_events import ChatEventName
from services.agent.business.exec_events import ExecEventTranslator
from services.agent.business.kernel.budget import Budget, BudgetTracker
from services.agent.business.kernel.cancellation import CancellationCoordinator
from services.agent.business.kernel.ledger import KernelLedger
from services.agent.business.kernel.loop import AgentKernel
from services.agent.business.kernel.subagent import (
    BuiltinAgentSlot,
    SubRunHandle,
    SubRunReceipt,
    SubRunResult,
    subrun_finished_status,
)
from services.agent.domain.model.kernel_actions import ToolCall, ToolResult
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.agent.domain.model.kernel_gates import RunOutcome
from tests.agent.conftest import (
    ACTION_IRI,
    FakePlanner,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)

_OBJECT_SCHEMA = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}

PARENT_RUN = uuid.UUID(int=0)  # 单父用例约定：bind_parent 与 spawn 都用该 run_id


def make_result(
    *,
    outcome_status: str = "completed",
    artifact: dict[str, Any] | None = None,
    tokens_used: int = 120,
) -> SubRunResult:
    return SubRunResult(
        outcome=RunOutcome(run_id=uuid.uuid4(), status=outcome_status, reason="子 Run 终态"),
        artifact=artifact if artifact is not None else {"summary": "停电原因：线路过载"},
        tokens_used=tokens_used,
    )


class EmitCollector:
    """发射通道桩：按序记录 (event_type, data)（事件序列断言口）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event_type: str, data: dict[str, Any]) -> None:
        self.events.append((event_type, data))

    @property
    def types(self) -> list[str]:
        return [t for t, _ in self.events]


class FakeRunner:
    """SubRunRunner 桩：可配置产物/耗时/挂起；记录子 TaskRef（血统深度断言口）。"""

    def __init__(self, result: SubRunResult | None = None, *, sleep_s: float = 0.0, hang: bool = False) -> None:
        self.result = result or make_result()
        self.sleep_s = sleep_s
        self.hang = hang
        self.calls: list[tuple[Any, TenantContext, Budget]] = []

    async def __call__(self, task: Any, ctx: TenantContext, *, budget: Budget) -> SubRunResult:
        self.calls.append((task, ctx, budget))
        if self.hang:
            await asyncio.Event().wait()
        if self.sleep_s:
            await asyncio.sleep(self.sleep_s)
        return self.result


def make_bound_slot(
    runner: FakeRunner,
    collector: EmitCollector,
    *,
    max_tokens: int | None = 1_000,
) -> tuple[BuiltinAgentSlot, BudgetTracker, CancellationCoordinator]:
    slot = BuiltinAgentSlot(runner)
    tracker = BudgetTracker(Budget(max_tokens=max_tokens, max_steps=None, duration_s=None))
    coordinator = CancellationCoordinator(KernelLedger(tenant_id=uuid.uuid4(), trace_id="trace-subrun-events"))
    slot.bind_parent(PARENT_RUN, tracker, coordinator, emit=collector)
    return slot, tracker, coordinator


# ── R2：spawn→finish 事件序列与 payload ──────────────────────────────────


async def test_派生成功_事件序列与started载荷字段齐全():
    collector = EmitCollector()
    slot, _, _ = make_bound_slot(FakeRunner(make_result(tokens_used=120)), collector)
    task = make_task(run_id=PARENT_RUN)
    handle_id = await slot.spawn_sub(
        task, make_ctx(), context_budget=500, artifact_schema=_OBJECT_SCHEMA, label="数据抽取员", index=2, total=5
    )
    assert collector.types == ["kernel.subrun_started", "kernel.subrun_finished"]  # STARTED 先于 FINISHED
    started = collector.events[0][1]
    assert started["sub_run_id"] == handle_id  # = 子 run 行 id（R1 落库主键同源）
    assert started["parent_run_id"] == str(task.run_id)
    assert started["task_id"] == str(task.task_id)
    assert started["depth"] == 1  # 根（depth 0）派发 → 子深度 1
    assert started["label"] == "数据抽取员"
    assert started["goal"] == task.objective
    assert started["index"] == 2 and started["total"] == 5  # 批次序号（Hermes task_index/task_count 同构）
    assert started["context_budget"] == 500
    assert isinstance(started["started_at"], str) and datetime.fromisoformat(started["started_at"])
    finished = collector.events[1][1]
    assert finished["sub_run_id"] == handle_id
    assert finished["status"] == "completed"
    assert isinstance(finished["duration_ms"], int) and finished["duration_ms"] >= 0
    assert finished["usage"] == {"total_tokens": 120}
    assert "error" not in finished  # 正常终态无 error


async def test_并行批次_逐成员index_total与label独立正确():
    collector = EmitCollector()
    slot, _, _ = make_bound_slot(FakeRunner(), collector)
    for i in range(3):
        await slot.spawn_sub(
            make_task(run_id=PARENT_RUN),
            make_ctx(),
            context_budget=100,
            artifact_schema={"type": "object"},
            label=f"worker-{i}",
            index=i,
            total=3,
        )
    starts = [data for t, data in collector.events if t == "kernel.subrun_started"]
    assert [s["index"] for s in starts] == [0, 1, 2]
    assert all(s["total"] == 3 for s in starts)
    assert [s["label"] for s in starts] == ["worker-0", "worker-1", "worker-2"]
    assert len({s["sub_run_id"] for s in starts}) == 3  # 逐成员独立 sub_run_id


async def test_缺省发射面元数据_可选项保持None():
    collector = EmitCollector()
    slot, _, _ = make_bound_slot(FakeRunner(), collector)
    await slot.spawn_sub(
        make_task(run_id=PARENT_RUN), make_ctx(), context_budget=100, artifact_schema={"type": "object"}
    )
    started = collector.events[0][1]
    assert started["label"] is None and started["index"] is None and started["total"] is None


# ── R2 §3.2：终态五值映射 ─────────────────────────────────────────────────


async def test_运行失败_outcome_failed_事件映射failed而非completed():
    collector = EmitCollector()
    slot, _, _ = make_bound_slot(FakeRunner(make_result(outcome_status="failed")), collector)
    handle_id = await slot.spawn_sub(
        make_task(run_id=PARENT_RUN), make_ctx(), context_budget=500, artifact_schema=_OBJECT_SCHEMA
    )
    receipt = slot.receipt(handle_id)
    assert receipt is not None and receipt.status == "completed"  # 内核回执枚举无 failed：产物过契约即 completed
    assert collector.events[1][1]["status"] == "failed"  # 事件面映射 §3.2：运行失败 ≠ completed


async def test_产物校验被拒_事件恒rejected_artifact_禁误报completed():
    collector = EmitCollector()
    slot, _, _ = make_bound_slot(FakeRunner(make_result(artifact={"wrong": 1})), collector)
    handle_id = await slot.spawn_sub(
        make_task(run_id=PARENT_RUN), make_ctx(), context_budget=500, artifact_schema=_OBJECT_SCHEMA
    )
    receipt = slot.receipt(handle_id)
    assert receipt is not None and receipt.status == "rejected_artifact"
    finished = collector.events[1][1]
    assert finished["status"] == "rejected_artifact"  # 宪法 2：执行成功但产物被拒，不得误报 completed
    assert "message" in finished["error"]  # 拒绝原因随 error 面向可审计


def test_五值映射纯函数_全分支对照():
    def status(receipt_status: str, outcome_status: str | None = None) -> str:
        handle = SubRunHandle(run_id=uuid.uuid4(), parent_run_id=uuid.uuid4(), context_budget=1, artifact_schema={})
        receipt = SubRunReceipt(handle=handle, status=receipt_status, outcome_status=outcome_status)
        return subrun_finished_status(receipt)

    assert status("rejected_artifact", "failed") == "rejected_artifact"  # 优先级最高：产物被拒不吞
    assert status("timeout") == "timeout"
    assert status("cancelled") == "cancelled"
    assert status("completed", "failed") == "failed"  # 回执 completed × 运行失败 → failed
    assert status("completed", "cancelled") == "cancelled"  # 子内核自行取消收敛
    assert status("completed", "timeout") == "timeout"
    assert status("completed", "completed") == "completed"
    assert status("completed", "waiting_tool") == "completed"  # B2 等待回执收敛≠失败


# ── R10：深度护栏（kernel 通道）───────────────────────────────────────────


async def test_深度超限_派发被拒_零事件():
    collector = EmitCollector()
    slot, _, _ = make_bound_slot(FakeRunner(), collector)
    with pytest.raises(Exception, match="派发深度超限"):
        await slot.spawn_sub(
            make_task(run_id=PARENT_RUN, depth=2),  # 默认上限 2：子深度 3 > 2
            make_ctx(),
            context_budget=100,
            artifact_schema={"type": "object"},
        )
    assert collector.events == []  # 拒绝发生在 STARTED 之前：不产生事件（40 篇 R10）
    assert not slot._receipts  # 无回执（未派生）


async def test_深度临界_派发放行_血统深度随TaskRef下传():
    collector = EmitCollector()
    runner = FakeRunner()
    slot, _, _ = make_bound_slot(runner, collector)
    await slot.spawn_sub(
        make_task(run_id=PARENT_RUN, depth=1), make_ctx(), context_budget=100, artifact_schema={"type": "object"}
    )
    child_task = runner.calls[0][0]
    assert child_task.depth == 2  # 父深度 1 + 1（嵌套 kernel.run 的 R10 护栏同源）
    assert collector.events[0][1]["depth"] == 2


async def test_嵌套内核_深度2的run内再派发_被拒收敛failed():
    """嵌套派生防线（R10）：depth=2 的子内核内 spawn（子深度 3 > 上限）→ 步失败收敛。"""
    dispatcher = make_tool_dispatcher(None)
    slot = BuiltinAgentSlot(FakeRunner())
    dispatcher.register_agent_slot(slot)
    dispatcher.register_planning_strategy(FakePlanner(make_candidate((make_step(),))))
    deep_task = make_task(depth=2)
    dispatcher.register_tool(SpawningTool(dispatcher, deep_task))
    outcome = await AgentKernel(dispatcher).run(
        deep_task, make_ctx(), budget=Budget(max_tokens=10_000, max_steps=5, duration_s=30.0)
    )
    assert outcome.status == "failed"  # spawn 被拒 → 步失败 → 运行收敛 failed
    assert not slot._receipts  # 未派生（拒绝发生在 STARTED 之前）


# ── R6：级联取消/超时补发终态 ─────────────────────────────────────────────


async def test_级联取消_补发FINISHED_cancelled():
    collector = EmitCollector()
    slot, _, coordinator = make_bound_slot(FakeRunner(hang=True), collector, max_tokens=None)

    async def parent() -> None:
        await slot.spawn_sub(
            make_task(run_id=PARENT_RUN), make_ctx(), context_budget=500, artifact_schema={"type": "object"}
        )

    spawn_task = asyncio.ensure_future(parent())
    await asyncio.sleep(0.05)  # 让子 Run 进入在途
    await coordinator.execute(reason="父取消测试")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(spawn_task, timeout=6.0)
    assert collector.types == ["kernel.subrun_started", "kernel.subrun_finished"]  # STARTED→补发 FINISHED
    finished = collector.events[1][1]
    assert finished["status"] == "cancelled"
    assert "message" in finished["error"]  # 级联取消原因面向可审计


async def test_派发超时_补发FINISHED_timeout():
    collector = EmitCollector()
    slot, _, _ = make_bound_slot(FakeRunner(sleep_s=30.0), collector, max_tokens=None)
    with pytest.raises(TimeoutError):
        await slot.spawn_sub(
            make_task(run_id=PARENT_RUN),
            make_ctx(),
            context_budget=500,
            artifact_schema={"type": "object"},
            timeout_ms=50,
        )
    assert collector.types == ["kernel.subrun_started", "kernel.subrun_finished"]
    assert collector.events[1][1]["status"] == "timeout"


async def test_裸插槽无父绑定_不发射_不报错():
    slot = BuiltinAgentSlot(FakeRunner(make_result(tokens_used=33)))
    handle_id = await slot.spawn_sub(make_task(), make_ctx(), context_budget=100, artifact_schema={"type": "object"})
    assert slot.receipt(handle_id) is not None  # 派生主流程不受发射通道缺失影响


# ── 端到端：kernel.run 内派生 → 账本广播 → ExecEventTranslator ─────────────


class SpawningTool:
    """计划步内派生子 Run 的工具（M4 消费形态的最小演示）：经分发器取 agent.slots。"""

    def __init__(self, dispatcher: Any, task: Any) -> None:
        self.meta = ExtensionMeta(
            name="fixture.spawn_tool", version="1.0.0", semantic_annotation={"action_iri": ACTION_IRI}
        )
        self._dispatcher = dispatcher
        self._task = task

    async def invoke(
        self, call: Any, ctx: TenantContext, *, approval: Any = None, timeout_ms: int = 30_000
    ) -> ToolResult:
        slot = self._dispatcher.agent_slot()
        assert slot is not None
        handle_id = await slot.spawn_sub(
            self._task, ctx, context_budget=500, artifact_schema={"type": "object"}, label="分析员", index=0, total=1
        )
        receipt = slot.receipt(handle_id)
        assert receipt is not None and receipt.status == "completed"
        return ToolResult(ok=True, output={"sub_run": handle_id})


async def test_内核run内派生_SUBRUN锚点经账本转译为ChatEvent():
    task = make_task()
    chat_events: list[Any] = []
    dispatcher = make_tool_dispatcher(None)
    slot = BuiltinAgentSlot(FakeRunner(make_result(tokens_used=120)))
    dispatcher.register_agent_slot(slot)
    dispatcher.register_planning_strategy(FakePlanner(make_candidate((make_step(),))))
    dispatcher.register_tool(SpawningTool(dispatcher, task))
    dispatcher.register_hook(
        "on_kernel_event",
        ExecEventTranslator(
            task_id=uuid.UUID(int=7), session_id=uuid.UUID(int=8), trace_id="trace-e2e", on_event=chat_events.append
        ),
    )
    outcome = await AgentKernel(dispatcher).run(
        task, make_ctx(trace_id="trace-e2e"), budget=Budget(max_tokens=10_000, max_steps=5, duration_s=30.0)
    )
    assert outcome.status == "completed"
    names = [e.name for e in chat_events]
    assert names.count(ChatEventName.SUBRUN_STARTED) == 1
    assert names.count(ChatEventName.SUBRUN_FINISHED) == 1
    # STARTED 必须先于 FINISHED（40 篇 §4.3-2），且发射随内核运行（先于 run 收敛完成）
    assert names.index(ChatEventName.SUBRUN_STARTED) < names.index(ChatEventName.SUBRUN_FINISHED)
    started = next(e for e in chat_events if e.name is ChatEventName.SUBRUN_STARTED)
    assert started.data["session_id"] == str(uuid.UUID(int=8))  # 转译器上下文补齐（内核无会话概念）
    assert started.data["trace_id"] == "trace-e2e"  # trace 取 KernelEvent（C2：内核账本拒收外源 trace）
    assert started.data["label"] == "分析员" and started.data["index"] == 0 and started.data["total"] == 1
    finished = next(e for e in chat_events if e.name is ChatEventName.SUBRUN_FINISHED)
    assert finished.data["status"] == "completed" and finished.data["usage"]["total_tokens"] == 120


# ── 能力层：spawn 工具透传 index/total/label（R2 批次序号来源）──────────────


class RecordingSlot:
    """SubagentSlotPort 桩：记录 spawn_sub 全部 kwargs（透传断言口）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def spawn_sub(
        self,
        task: Any,
        ctx: Any,
        *,
        context_budget: int,
        artifact_schema: dict[str, Any],
        timeout_ms: int = 600_000,
        label: str | None = None,
        index: int | None = None,
        total: int | None = None,
    ) -> str:
        self.calls.append({"context_budget": context_budget, "label": label, "index": index, "total": total})
        return str(uuid.uuid4())

    def receipt(self, handle_id: str) -> SubRunReceipt | None:
        return None


async def test_spawn工具_批次序号与label透传内核插槽():
    slot = RecordingSlot()
    spawn = SubagentSpawnTool(
        slot,
        lambda _ctx: make_task(run_id=PARENT_RUN),
        DerivationWhitelist(("analyst", "researcher")),
        BudgetSharePolicy(0.5),
        lambda: None,
        SpawnGroupRegistry(),
    )
    call = ToolCall(
        action_iri=SUBAGENT_SPAWN_ACTION_IRI,
        parameters={
            "tasks": [
                {
                    "agent_type": "analyst",
                    "objective": "子目标A",
                    "context_budget": 500,
                    "artifact_schema": {"type": "object"},
                },
                {
                    "agent_type": "researcher",
                    "objective": "子目标B",
                    "context_budget": 500,
                    "artifact_schema": {"type": "object"},
                },
            ]
        },
        param_hash="test-hash",
    )
    result = await spawn.invoke(call, make_ctx())
    assert result.ok
    await asyncio.sleep(0.05)  # 等批次成员的派生调用任务启动（注册即返回语义）
    assert [c["index"] for c in slot.calls] == [0, 1]  # 本批并行批次序号（0 基）
    assert all(c["total"] == 2 for c in slot.calls)  # 批次总量
    assert {c["label"] for c in slot.calls} == {"analyst", "researcher"}  # label=agent_type

# tests/agent/test_kernel_subagent.py
"""AgentSlot 内置实现测试（02 §4.2：分账 / Artifact 契约 / 窗口隔离 / 级联取消 / 系统化接线）。"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pydantic
import pytest
from conftest import (
    ACTION_IRI,
    FakePlanner,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)

from services.agent.business.kernel.budget import Budget, BudgetTracker
from services.agent.business.kernel.cancellation import CancellationCoordinator
from services.agent.business.kernel.errors import KernelContractError
from services.agent.business.kernel.ledger import KernelLedger
from services.agent.business.kernel.loop import AgentKernel
from services.agent.business.kernel.subagent import (
    BuiltinAgentSlot,
    KernelSubRunRunner,
    SubRunHandle,
    SubRunReceipt,
    SubRunResult,
    validate_artifact,
)
from services.agent.domain.model.kernel_actions import ToolResult
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext, TrustLevel
from services.agent.domain.model.kernel_gates import RunOutcome

_OBJECT_SCHEMA = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}

PARENT_RUN = uuid.UUID(int=0)  # 单父用例约定：bind_parent 与 spawn 都用该 run_id


def make_result(
    *,
    artifact: dict[str, Any] | None = None,
    tokens_used: int = 120,
) -> SubRunResult:
    return SubRunResult(
        outcome=RunOutcome(run_id=uuid.uuid4(), status="completed", reason="子 Run 正常终态"),
        artifact=artifact if artifact is not None else {"summary": "停电原因：线路过载"},
        artifact_pointer=None,
        tokens_used=tokens_used,
    )


class FakeRunner:
    """SubRunRunner 桩：可配置产物/耗时/挂起；记录调用参数供窗口隔离断言。"""

    meta = ExtensionMeta(
        name="fixture.subrunner",
        version="1.0.0",
        semantic_annotation={"concept_iri": "http://ontology.example/concept/子代理"},
    )

    def __init__(self, result: SubRunResult | None = None, *, sleep_s: float = 0.0, hang: bool = False) -> None:
        self.result = result or make_result()
        self.sleep_s = sleep_s
        self.hang = hang
        self.calls: list[tuple[Any, TenantContext, Budget]] = []

    async def __call__(self, task: Any, ctx: TenantContext, *, budget: Budget) -> SubRunResult:
        self.calls.append((task, ctx, budget))
        if self.hang:
            await asyncio.Event().wait()  # 挂起直到被取消（级联取消用例）
        if self.sleep_s:
            await asyncio.sleep(self.sleep_s)
        return self.result


def make_bound_slot(
    runner: FakeRunner, *, max_tokens: int | None = 1_000
) -> tuple[BuiltinAgentSlot, BudgetTracker, CancellationCoordinator]:
    slot = BuiltinAgentSlot(runner)
    tracker = BudgetTracker(Budget(max_tokens=max_tokens, max_steps=None, duration_s=None))
    coordinator = CancellationCoordinator(KernelLedger(tenant_id=uuid.uuid4(), trace_id="trace-subagent"))
    slot.bind_parent(PARENT_RUN, tracker, coordinator)
    return slot, tracker, coordinator


# ── 正向：同步派生 + Artifact 回传 + 分账 ─────────────────────────────────


async def test_派生成功_artifact过schema_子消耗记回父预算():
    slot, tracker, _ = make_bound_slot(FakeRunner(make_result(tokens_used=120)))
    handle_id = await slot.spawn_sub(
        make_task(run_id=PARENT_RUN), make_ctx(), context_budget=500, artifact_schema=_OBJECT_SCHEMA
    )
    receipt = slot.receipt(handle_id)
    assert receipt is not None and receipt.status == "completed"
    assert receipt.artifact == {"summary": "停电原因：线路过载"}
    assert receipt.outcome_status == "completed"
    assert tracker.tokens_used == 120  # 分账回写：子消耗计入父（A4 不新增总额）


async def test_子Run窗口隔离_独立句柄与预算_共享trace():
    runner = FakeRunner()
    slot, _, _ = make_bound_slot(runner)
    task = make_task(run_id=PARENT_RUN)
    ctx = make_ctx()
    handle_id = await slot.spawn_sub(task, ctx, context_budget=500, artifact_schema={"type": "object"})
    child_task, child_ctx, child_budget = runner.calls[0]
    handle = slot.receipt(handle_id) is not None
    assert handle
    assert child_task.run_id != task.run_id  # 窗口隔离：子 Run 独立于父 Run
    assert child_task.task_id == task.task_id  # 隶属不变式：同一 Task（03 §4.1）
    assert child_task.goal_action_iris == task.goal_action_iris  # 判据引用随派生收窄继承
    assert child_budget.max_tokens == 500  # 预算=分账额度（绝对上限）
    assert child_ctx.trace_id == ctx.trace_id  # 共享 trace（C2）
    assert str(child_task.run_id) == handle_id  # 返回句柄即子 Run id


async def test_无父绑定_裸派生可用():
    slot = BuiltinAgentSlot(FakeRunner(make_result(tokens_used=33)))
    handle_id = await slot.spawn_sub(make_task(), make_ctx(), context_budget=100, artifact_schema={"type": "object"})
    assert slot.receipt(handle_id) is not None


# ── 负向：契约拒绝与失败分支 ─────────────────────────────────────────────


async def test_分账超父剩余_拒绝派生():
    slot, tracker, _ = make_bound_slot(FakeRunner(), max_tokens=100)
    tracker.add_tokens(50)  # 剩余 50
    with pytest.raises(KernelContractError, match="分账超父剩余"):
        await slot.spawn_sub(
            make_task(run_id=PARENT_RUN), make_ctx(), context_budget=80, artifact_schema={"type": "object"}
        )


async def test_artifact_schema非法_拒绝派生():
    slot, _, _ = make_bound_slot(FakeRunner())
    with pytest.raises(KernelContractError, match="artifact_schema"):
        await slot.spawn_sub(
            make_task(run_id=PARENT_RUN), make_ctx(), context_budget=100, artifact_schema={"type": "string"}
        )


async def test_context_budget非正数_拒绝派生():
    slot, _, _ = make_bound_slot(FakeRunner())
    with pytest.raises(KernelContractError, match="正整数"):
        await slot.spawn_sub(
            make_task(run_id=PARENT_RUN), make_ctx(), context_budget=0, artifact_schema={"type": "object"}
        )


async def test_artifact违例_schema_按失败分支处理_分账仍生效():
    slot, tracker, _ = make_bound_slot(FakeRunner(make_result(artifact={"wrong": 1}, tokens_used=77)))
    handle_id = await slot.spawn_sub(
        make_task(run_id=PARENT_RUN), make_ctx(), context_budget=500, artifact_schema=_OBJECT_SCHEMA
    )
    receipt = slot.receipt(handle_id)
    assert receipt is not None and receipt.status == "rejected_artifact"
    assert receipt.artifact is None  # 违例产物不回传（禁自由文本回灌父上下文）
    assert tracker.tokens_used == 77  # 消耗照记：失败分支不免除分账


# ── 取消与超时（§2.4）────────────────────────────────────────────────────


async def test_父取消_级联取消在途子Run_receipt留痕():
    slot, _, coordinator = make_bound_slot(FakeRunner(hang=True), max_tokens=None)

    async def parent() -> None:
        await slot.spawn_sub(
            make_task(run_id=PARENT_RUN), make_ctx(), context_budget=500, artifact_schema={"type": "object"}
        )

    spawn_task = asyncio.ensure_future(parent())
    await asyncio.sleep(0.05)  # 让子 Run 进入在途
    report = await coordinator.execute(reason="父取消测试")  # 取消清单第 1 步：子 Run 级联
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(spawn_task, timeout=6.0)
    assert report.finished  # 清单零残留（子 Run 钩子完成）
    receipts = list(slot._receipts.values())  # 测试断言口：直接读内部回执表
    assert len(receipts) == 1 and receipts[0].status == "cancelled"


async def test_子Run超时_按超时分支收敛():
    slot, tracker, _ = make_bound_slot(FakeRunner(sleep_s=30.0), max_tokens=None)
    with pytest.raises(TimeoutError):
        await slot.spawn_sub(
            make_task(run_id=PARENT_RUN),
            make_ctx(),
            context_budget=500,
            artifact_schema={"type": "object"},
            timeout_ms=50,
        )
    receipt = next(iter(slot._receipts.values()))  # 测试断言口
    assert receipt.status == "timeout"
    assert tracker.tokens_used == 0  # 未结算：无分账


# ── 系统化接线：AgentKernel.run 自动挂父作用域（02 §4.2 同注册表）──────────


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
        handle_id = await slot.spawn_sub(self._task, ctx, context_budget=500, artifact_schema={"type": "object"})
        receipt = slot.receipt(handle_id)
        assert receipt is not None and receipt.status == "completed"
        return ToolResult(ok=True, output={"sub_run": handle_id}, trust_level=TrustLevel.AGENT_ATTESTED)


async def test_内核run内派生_父作用域自动接线_分账落水印():
    task = make_task()
    slot = BuiltinAgentSlot(FakeRunner(make_result(tokens_used=120)))
    kernel = AgentKernel(_build_dispatcher(slot, task))
    outcome = await kernel.run(task, make_ctx(), budget=Budget(max_tokens=10_000, max_steps=5, duration_s=30.0))
    assert outcome.status == "completed"
    assert len(slot._receipts) == 1  # 测试断言口：派生恰好一次
    assert next(iter(slot._receipts.values())).status == "completed"
    assert outcome.terminal_states[0].budget_watermark.tokens_used >= 120  # 观察阶段水印含子 Run 分账
    assert slot._parents  # 父作用域已由 kernel.run 系统化接线（bind_parent）


def _build_dispatcher(slot: BuiltinAgentSlot, task: Any) -> Any:
    dispatcher = make_tool_dispatcher(None)
    dispatcher.register_agent_slot(slot)
    dispatcher.register_planning_strategy(FakePlanner(make_candidate((make_step(),))))
    dispatcher.register_tool(SpawningTool(dispatcher, task))
    return dispatcher


async def test_KernelSubRunRunner_适配kernel_run为子执行器():
    dispatcher = make_tool_dispatcher(FakeTool())
    dispatcher.register_planning_strategy(FakePlanner(make_candidate((make_step(),))))
    runner = KernelSubRunRunner(AgentKernel(dispatcher))
    result = await runner(make_task(), make_ctx(), budget=Budget(max_tokens=1_000, max_steps=5, duration_s=10.0))
    assert result.outcome.status == "completed"
    assert result.tokens_used == 0  # v1 产物映射：消耗由组合根抽取（显式口径）


# ── Artifact 确定性校验 ──────────────────────────────────────────────────


def test_artifact校验_嵌套必填与数组元素():
    schema = {
        "type": "object",
        "required": ["findings"],
        "properties": {
            "findings": {
                "type": "array",
                "items": {"type": "object", "required": ["iri"], "properties": {"iri": {"type": "string"}}},
            }
        },
    }
    assert validate_artifact({"findings": [{"iri": "http://x"}]}, schema) == []
    errors = validate_artifact({"findings": [{"nope": 1}]}, schema)
    assert any("iri" in e for e in errors)


def test_artifact校验_类型与整数布尔边界():
    assert validate_artifact({"n": True}, {"type": "object", "properties": {"n": {"type": "integer"}}}) != []
    assert validate_artifact({"n": 3}, {"type": "object", "properties": {"n": {"type": "integer"}}}) == []
    assert validate_artifact([1], {"type": "object"}) != []  # 顶层类型不符
    assert (
        validate_artifact(
            {"a": [1, "x"]}, {"type": "object", "properties": {"a": {"type": "array", "items": {"type": "integer"}}}}
        )
        != []
    )


def test_receipt取用_非法句柄返回None():
    slot = BuiltinAgentSlot(FakeRunner())
    assert slot.receipt("not-a-uuid") is None


def test_SubRunReceipt与Handle冻结值语义():
    handle = SubRunHandle(run_id=uuid.uuid4(), parent_run_id=uuid.uuid4(), context_budget=1, artifact_schema={})
    receipt = SubRunReceipt(handle=handle, status="completed")
    with pytest.raises(pydantic.ValidationError):
        receipt.status = "completed"
    with pytest.raises(pydantic.ValidationError):
        handle.context_budget = 2

# tests/agent/test_cap_subagent.py
"""subagent 能力三工具测试（docs/Agent/06 路线 #4）：spawn/wait/interrupt 正例 + 四护栏负向。

零外部依赖：内核 AgentSlot 用真实 BuiltinAgentSlot + Fake 子代理执行器桩（研究整理/08
C4 形状）；AAA + 中文命名。覆盖：批量 spawn→wait→Artifact 取回、一次性消费语义、
递归深度上限（ContextVar 随内核派生任务下传）、并发上限（整批 all-or-nothing）、
派生白名单（fail-closed）、预算继承（份额上限 + 内核 A4 兜底）、interrupt 传播取消、
审计留痕（父/子 run_id + 预算份额，正文红线）、分发器注册寻址。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from services.agent.business.capabilities.subagent import (
    BUDGET_SHARE_DEFAULT,
    DERIVATION_DEPTH,
    SUBAGENT_INTERRUPT_ACTION_IRI,
    SUBAGENT_SPAWN_ACTION_IRI,
    SUBAGENT_WAIT_ACTION_IRI,
    BudgetSharePolicy,
    build_subagent_bindings,
    default_audit_sink,
)
from services.agent.business.kernel.budget import Budget, BudgetTracker
from services.agent.business.kernel.cancellation import CancellationCoordinator
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.ledger import KernelLedger
from services.agent.business.kernel.subagent import BuiltinAgentSlot, SubRunResult
from services.agent.domain.model.kernel_actions import ToolCall, ToolResult
from services.agent.domain.model.kernel_context import TaskRef, TenantContext
from services.agent.domain.model.kernel_gates import RunOutcome
from services.platform.errors import ErrorCode
from tests.agent.conftest import make_ctx

_OBJECT_SCHEMA = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}
PARENT_RUN = uuid.UUID(int=0)  # 单父用例约定：bind_parent 与派生归因同用该 run_id
_TASK = TaskRef(
    task_id=uuid.uuid4(),
    run_id=PARENT_RUN,
    task_iri="http://ontology.example/task/停电分析",
    objective="分析线路停电原因",
)


def make_result(*, artifact: dict[str, Any] | None = None, tokens_used: int = 0) -> SubRunResult:
    return SubRunResult(
        outcome=RunOutcome(run_id=uuid.uuid4(), status="completed", reason="子 Run 正常终态"),
        artifact=artifact if artifact is not None else {"summary": "停电原因：线路过载"},
        tokens_used=tokens_used,
    )


class FakeRunner:
    """SubRunRunner 桩：可配置产物/挂起；记录调用参数与取消次数（派生路径断言口）。"""

    def __init__(self, result: SubRunResult | None = None, *, sleep_s: float = 0.0, hang: bool = False) -> None:
        self.result = result or make_result()
        self.sleep_s = sleep_s
        self.hang = hang
        self.calls: list[tuple[TaskRef, TenantContext, Budget]] = []
        self.cancelled = 0

    async def __call__(self, task: TaskRef, ctx: TenantContext, *, budget: Budget) -> SubRunResult:
        self.calls.append((task, ctx, budget))
        try:
            if self.hang:
                await asyncio.Event().wait()  # 挂起直到被取消（interrupt/级联用例）
            if self.sleep_s:
                await asyncio.sleep(self.sleep_s)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return self.result


class ScriptedRunner:
    """按脚本逐次响应的桩：("hang",) 挂起 / ("sleep", 秒, 产物) 延时返回；末项可复用。"""

    def __init__(self, script: list[tuple[Any, ...]]) -> None:
        self.script = list(script)
        self.calls: list[tuple[TaskRef, TenantContext, Budget]] = []
        self.cancelled = 0

    async def __call__(self, task: TaskRef, ctx: TenantContext, *, budget: Budget) -> SubRunResult:
        self.calls.append((task, ctx, budget))
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        try:
            if step[0] == "hang":
                await asyncio.Event().wait()
            elif step[0] == "sleep":
                await asyncio.sleep(step[1])
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return step[-1]


class RecursingRunner:
    """子 Run 执行体内再次调用 spawn 工具的桩（模拟子代理自我派生，深度护栏用）。

    ``tool`` 公开可回填：spawn 工具依赖以本桩为执行器的内核插槽，装配后回填引用。
    """

    def __init__(self, tool: Any, call: ToolCall, ctx: TenantContext) -> None:
        self.tool = tool
        self._call = call
        self._ctx = ctx
        self.calls = 0
        self.depths: list[int] = []
        self.tool_results: list[ToolResult] = []

    async def __call__(self, task: TaskRef, ctx: TenantContext, *, budget: Budget) -> SubRunResult:
        self.calls += 1
        self.depths.append(DERIVATION_DEPTH.get())  # 子执行子树应读到自身派生深度
        self.tool_results.append(await self.tool.invoke(self._call, self._ctx))
        return make_result()


def make_bindings(
    runner: Any,
    *,
    allowlist: tuple[str, ...] | Any = ("analyst",),
    probe: Callable[[], int | None] | None = None,
    share: float = BUDGET_SHARE_DEFAULT,
    max_depth: int = 2,
    max_concurrent: int = 4,
    audit_sink: Callable[[dict[str, Any]], None] = default_audit_sink,
    bind: bool = False,
    max_tokens: int | None = None,
) -> tuple[Any, ...]:
    """装配三工具 + 内核插槽（bind=True 时挂父作用域：分账 tracker + 级联协调器）。"""
    slot = BuiltinAgentSlot(runner)
    tracker: BudgetTracker | None = None
    coordinator: CancellationCoordinator | None = None
    if bind:
        tracker = BudgetTracker(Budget(max_tokens=max_tokens, max_steps=None, duration_s=None))
        coordinator = CancellationCoordinator(KernelLedger(tenant_id=uuid.uuid4(), trace_id="trace-cap-subagent"))
        slot.bind_parent(PARENT_RUN, tracker, coordinator)
    spawn, wait, interrupt = build_subagent_bindings(
        slot,
        lambda _ctx: _TASK,
        allowlist,
        budget_probe=probe,
        budget_share=share,
        max_depth=max_depth,
        max_concurrent=max_concurrent,
        audit_sink=audit_sink,
    )
    return slot, spawn, wait, interrupt, tracker, coordinator


def _spawn_call(*tasks: dict[str, Any]) -> ToolCall:
    return ToolCall(
        action_iri=SUBAGENT_SPAWN_ACTION_IRI,
        parameters={"tasks": list(tasks)},
        param_hash="test-hash",
    )


def _task_entry(
    *,
    agent_type: str = "analyst",
    objective: str = "子目标：核查支线 B",
    context_budget: int = 500,
    artifact_schema: dict[str, Any] | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "agent_type": agent_type,
        "objective": objective,
        "context_budget": context_budget,
        "artifact_schema": artifact_schema if artifact_schema is not None else {"type": "object"},
    }
    if timeout_ms is not None:
        entry["timeout_ms"] = timeout_ms
    return entry


def _wait_call(group_id: str, *, index: int | None = None, timeout_ms: int | None = None) -> ToolCall:
    parameters: dict[str, Any] = {"group_id": group_id}
    if index is not None:
        parameters["index"] = index
    if timeout_ms is not None:
        parameters["timeout_ms"] = timeout_ms
    return ToolCall(action_iri=SUBAGENT_WAIT_ACTION_IRI, parameters=parameters, param_hash="test-hash")


def _interrupt_call(group_id: str, *, index: int | None = None) -> ToolCall:
    parameters: dict[str, Any] = {"group_id": group_id}
    if index is not None:
        parameters["index"] = index
    return ToolCall(action_iri=SUBAGENT_INTERRUPT_ACTION_IRI, parameters=parameters, param_hash="test-hash")


async def _flush() -> None:
    """让 done 回调（在途计数/落定审计）在本循环内收敛。"""
    for _ in range(4):
        await asyncio.sleep(0)


async def _eventually(predicate: Callable[[], bool], *, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return predicate()


# ── 正向：批量 spawn → wait → Artifact 取回 ──────────────────────────────────


async def test_spawn_批量注册返回句柄组_子Run经内核插槽派生():
    # Arrange：内核插槽 + 快速完成的 Fake 子代理（白名单含两种目标类型）
    runner = FakeRunner(make_result(tokens_used=10))
    _, spawn, _, _, _, _ = make_bindings(runner, allowlist=("analyst", "researcher"))
    # Act：一批两个子任务
    result = await spawn.invoke(_spawn_call(_task_entry(), _task_entry(agent_type="researcher")), make_ctx())
    await _flush()
    # Assert：句柄组返回，且派生走了内核插槽（Fake 执行器被调用两次）
    assert result.ok
    output = result.output
    assert output["spawned"] == 2 and len(output["children"]) == 2
    assert output["children"][0]["agent_type"] == "analyst"
    assert output["children"][1]["budget_allocated"] == 500
    assert isinstance(output["group_id"], str) and output["group_id"]
    assert len(runner.calls) == 2  # 子 Run 由内核派生（执行器被内核调用）


async def test_spawn后wait_取回过契约的Artifact与内核回执():
    # Arrange：子代理产出过契约的 Artifact
    runner = FakeRunner(make_result(artifact={"summary": "停电原因：线路过载"}, tokens_used=120))
    slot, spawn, wait, _, _, _ = make_bindings(runner)
    spawned = await spawn.invoke(_spawn_call(_task_entry(artifact_schema=_OBJECT_SCHEMA)), make_ctx())
    group_id = spawned.output["group_id"]
    # Act
    result = await wait.invoke(_wait_call(group_id), make_ctx())
    # Assert：成员回执含结构化 Artifact，内核回执可按子 run_id 取用
    assert result.ok
    member = result.output["members"][0]
    assert member["status"] == "completed"
    assert member["artifact"] == {"summary": "停电原因：线路过载"}
    assert member["tokens_used"] == 120
    child_run_id = member["child_run_id"]
    assert slot.receipt(child_run_id) is not None  # 结果取用走内核回执表（02 §4.2）
    assert result.output["group_consumed"] is True  # 全员取回后组注销（一次性消费）


async def test_wait_按index取单个成员_组保留至全员取回():
    # Arrange：两个成员不同时长的脚本桩
    runner = ScriptedRunner(
        [
            ("sleep", 0.02, make_result(artifact={"summary": "甲"})),
            ("sleep", 0.06, make_result(artifact={"summary": "乙"})),
        ]
    )
    _, spawn, wait, _, _, _ = make_bindings(runner)
    spawned = await spawn.invoke(_spawn_call(_task_entry(), _task_entry()), make_ctx())
    group_id = spawned.output["group_id"]
    # Act：先只等成员 0
    first = await wait.invoke(_wait_call(group_id, index=0, timeout_ms=2_000), make_ctx())
    second = await wait.invoke(_wait_call(group_id), make_ctx())
    # Assert：首取只含成员 0 且组保留；再取整组只回未取回的成员 1 并注销组
    assert first.ok and [m["index"] for m in first.output["members"]] == [0]
    assert first.output["members"][0]["artifact"] == {"summary": "甲"}
    assert first.output["group_consumed"] is False
    assert second.ok and [m["index"] for m in second.output["members"]] == [1]
    assert second.output["members"][0]["artifact"] == {"summary": "乙"}
    assert second.output["group_consumed"] is True


async def test_wait_未知组与已消费组_结构化拒绝():
    # Arrange：一份已消费的组
    _, spawn, wait, _, _, _ = make_bindings(FakeRunner())
    spawned = await spawn.invoke(_spawn_call(_task_entry()), make_ctx())
    group_id = spawned.output["group_id"]
    await wait.invoke(_wait_call(group_id), make_ctx())
    # Act / Assert：未知组与已消费组同形拒绝（3001）
    unknown = await wait.invoke(_wait_call("no-such-group"), make_ctx())
    consumed = await wait.invoke(_wait_call(group_id), make_ctx())
    assert not unknown.ok and unknown.error_code == int(ErrorCode.PARAM_INVALID)
    assert not consumed.ok and consumed.error_code == int(ErrorCode.PARAM_INVALID)
    assert "已消费" in consumed.error_message


async def test_wait_超时_结构化拒绝且组保留可重试():
    # Arrange：挂起的子代理 + 很短的等待上限
    _, spawn, wait, interrupt, _, _ = make_bindings(FakeRunner(hang=True))
    spawned = await spawn.invoke(_spawn_call(_task_entry()), make_ctx())
    group_id = spawned.output["group_id"]
    # Act：等待 50ms 超时后改为取消
    timed = await wait.invoke(_wait_call(group_id, timeout_ms=50), make_ctx())
    cancelled = await interrupt.invoke(_interrupt_call(group_id), make_ctx())
    # Assert：超时=5001 结构化失败；组未被消费（interrupt 仍可寻址）
    assert not timed.ok and timed.error_code == int(ErrorCode.LLM_TIMEOUT)
    assert "超时" in timed.error_message and "重试" in timed.error_message
    assert cancelled.ok and cancelled.output["interrupted"] == [0]


# ── 护栏 1：递归深度上限（默认 2 层）──────────────────────────────────────────


async def test_spawn_递归深度两层内放行_第三层拒绝():
    # Arrange：子代理执行体内自我派生（深度 1 → 2 → 拒），深度经 ContextVar 随内核派生任务下传
    runner = RecursingRunner(None, _spawn_call(_task_entry(context_budget=100)), make_ctx())
    _, spawn, _, _, _, _ = make_bindings(runner, max_depth=2)
    runner.tool = spawn  # 回填：子执行体引用同一 spawn 工具
    # Act：根调用（深度 0）发起派生
    root = await spawn.invoke(_spawn_call(_task_entry(context_budget=100)), make_ctx())
    assert root.ok
    assert await _eventually(lambda: len(runner.tool_results) >= 2)
    # Assert：两层放行、第三层结构化拒绝；子执行子树读到的深度逐层 +1
    assert runner.depths == [1, 2]
    assert runner.tool_results[0].ok is True  # 深度 1 的子代理可再派生（第 2 层）
    assert runner.tool_results[1].ok is False  # 深度 2 的孙代理被拒（超出 2 层上限）
    assert "深度" in (runner.tool_results[1].error_message or "")
    assert runner.calls == 2  # 拒绝为结构化 ToolResult，未再触发第 3 次内核派生


# ── 护栏 2：并发子代理上限（整批 all-or-nothing）──────────────────────────────


async def test_spawn_并发超限拒绝_回收在途后放行():
    # Arrange：上限 2，子代理全部挂起
    runner = FakeRunner(hang=True)
    _, spawn, _, interrupt, _, _ = make_bindings(runner, max_concurrent=2)
    ctx = make_ctx()
    first = await spawn.invoke(_spawn_call(_task_entry(), _task_entry()), ctx)
    assert first.ok
    await _flush()
    # Act / Assert：第 3 个在途会被整批拒绝（TOOL_BUSY），零新增派生
    denied = await spawn.invoke(_spawn_call(_task_entry()), ctx)
    assert not denied.ok and denied.error_code == int(ErrorCode.TOOL_BUSY)
    assert "并发" in denied.error_message
    assert len(runner.calls) == 2
    # 回收在途后同形状调用放行
    done = await interrupt.invoke(_interrupt_call(first.output["group_id"]), ctx)
    assert done.ok
    await _flush()
    retried = await spawn.invoke(_spawn_call(_task_entry()), ctx)
    assert retried.ok
    await _flush()
    assert len(runner.calls) == 3
    await interrupt.invoke(_interrupt_call(retried.output["group_id"]), ctx)  # 清场


async def test_spawn_批量容量不足_整批零派生():
    # Arrange：上限 4，在途 3，申请批量为 2（3+2 > 4）
    runner = FakeRunner(hang=True)
    _, spawn, _, interrupt, _, _ = make_bindings(runner, max_concurrent=4)
    ctx = make_ctx()
    first = await spawn.invoke(_spawn_call(_task_entry(), _task_entry(), _task_entry()), ctx)
    assert first.ok
    await _flush()
    assert len(runner.calls) == 3
    # Act：批量 2 触发整批拒绝
    denied = await spawn.invoke(_spawn_call(_task_entry(), _task_entry()), ctx)
    await interrupt.invoke(_interrupt_call(first.output["group_id"]), ctx)
    # Assert：零部分派生（fail-closed）
    assert not denied.ok and denied.error_code == int(ErrorCode.TOOL_BUSY)
    assert "整批拒绝" in denied.error_message
    assert len(runner.calls) == 3


# ── 护栏 3：派生白名单（deny-by-default）──────────────────────────────────────


async def test_spawn_白名单外类型拒绝_零派生():
    # Arrange：白名单只含 analyst
    runner = FakeRunner()
    _, spawn, _, _, _, _ = make_bindings(runner, allowlist=("analyst",))
    # Act：白名单外的目标类型
    result = await spawn.invoke(_spawn_call(_task_entry(agent_type="hacker")), make_ctx())
    # Assert：结构化拒绝 + 零派生
    assert not result.ok and result.error_code == int(ErrorCode.SCOPE_INSUFFICIENT)
    assert "白名单" in result.error_message and "hacker" in result.error_message
    assert len(runner.calls) == 0


async def test_spawn_空白名单_fail_closed全拒():
    # Arrange：未注册任何可派生类型（安全边界而非功能开关）
    runner = FakeRunner()
    _, spawn, _, _, _, _ = make_bindings(runner, allowlist=())
    # Act / Assert
    result = await spawn.invoke(_spawn_call(_task_entry()), make_ctx())
    assert not result.ok and result.error_code == int(ErrorCode.SCOPE_INSUFFICIENT)
    assert len(runner.calls) == 0


# ── 护栏 4：预算继承（份额上限，参数可调；内核 A4 兜底）─────────────────────────


async def test_预算继承_实配为min申请额与父剩余份额():
    # Arrange：父剩余 1000、份额 0.5 → 单成员上限 500
    runner = FakeRunner()
    probe = lambda: 1_000  # noqa: E731 —— 组合根探针桩（真实接线=父 tracker.remaining_tokens）
    _, spawn, _, _, _, _ = make_bindings(runner, probe=probe, share=0.5)
    # Act：申请 800（>份额上限）与 300（<上限）各一
    result = await spawn.invoke(
        _spawn_call(_task_entry(context_budget=800), _task_entry(context_budget=300)), make_ctx()
    )
    await _flush()
    # Assert：实配 = min(申请额, ⌊父剩余×份额⌋)，并随内核预算对象落地
    assert result.ok
    assert [c["budget_allocated"] for c in result.output["children"]] == [500, 300]
    assert result.output["budget_share"] == 0.5
    assert [call[2].max_tokens for call in runner.calls] == [500, 300]


async def test_预算继承_份额塌缩为0_拒绝派生且份额参数受校验():
    # Arrange：父剩余 1 × 份额 0.5 → 上限份额 0
    runner = FakeRunner()
    _, spawn, _, _, _, _ = make_bindings(runner, probe=lambda: 1, share=0.5)
    # Act / Assert：结构化拒绝（5005 预算段），零派生
    result = await spawn.invoke(_spawn_call(_task_entry(context_budget=500)), make_ctx())
    assert not result.ok and result.error_code == int(ErrorCode.RETRY_BUDGET_EXHAUSTED)
    assert "预算" in result.error_message
    assert len(runner.calls) == 0
    # 份额参数形状：非 (0, 1] 一律拒绝
    with pytest.raises(ValueError, match="budget_share"):
        BudgetSharePolicy(0.0)
    with pytest.raises(ValueError, match="budget_share"):
        BudgetSharePolicy(1.5)


async def test_内核A4分账断言兜底_超父剩余的派生落定为rejected():
    # Arrange：能力层份额上限被探针放大（10_000×0.5=5000），但父 Run 实际只剩 100
    runner = FakeRunner(make_result(tokens_used=0))
    slot, spawn, wait, _, tracker, _ = make_bindings(runner, probe=lambda: 10_000, share=0.5, bind=True, max_tokens=100)
    assert tracker is not None
    # Act：申请 5000 → 能力层放行，内核 A4 断言拒派生
    spawned = await spawn.invoke(_spawn_call(_task_entry(context_budget=5_000)), make_ctx())
    result = await wait.invoke(_wait_call(spawned.output["group_id"]), make_ctx())
    # Assert：成员落定为 rejected（内核契约拒绝），父预算零消耗
    member = result.output["members"][0]
    assert member["status"] == "rejected"
    assert "分账超父剩余" in (member["reason"] or "")
    assert tracker.tokens_used == 0


# ── interrupt：取消传播与留痕 ────────────────────────────────────────────────


async def test_interrupt_传播取消_子Run收敛_cancelled留痕():
    # Arrange：挂起的子代理（经内核插槽 + 级联协调器）
    runner = FakeRunner(hang=True)
    slot, spawn, _, interrupt, _, _ = make_bindings(runner, bind=True, max_tokens=None)
    spawned = await spawn.invoke(_spawn_call(_task_entry()), make_ctx())
    group_id = spawned.output["group_id"]
    await asyncio.sleep(0.05)  # 让子 Run 进入在途（内核已派生子 Run，取消才有级联对象）
    # Act
    result = await interrupt.invoke(_interrupt_call(group_id), make_ctx())
    # Assert：取消经内核取消分支传导——子执行器真实收到取消，回执留痕 cancelled
    assert result.ok and result.output["interrupted"] == [0]
    assert result.output["already_settled"] == []
    assert runner.cancelled == 1  # 传播取消：子 Run 被内核级联取消（§2.4 清单第 1 步）
    receipts = [r for r in _all_receipts(slot) if r.status == "cancelled"]
    assert len(receipts) == 1 and receipts[0].handle.parent_run_id == PARENT_RUN


async def test_interrupt_按index取消单个成员_其余正常完成():
    # Arrange：成员 0 挂起、成员 1 快速完成
    runner = ScriptedRunner([("hang",), ("sleep", 0.01, make_result(artifact={"summary": "乙"}))])
    slot, spawn, wait, interrupt, _, _ = make_bindings(runner, bind=True, max_tokens=None)
    spawned = await spawn.invoke(_spawn_call(_task_entry(), _task_entry()), make_ctx())
    group_id = spawned.output["group_id"]
    await asyncio.sleep(0.05)  # 让成员 0 进入在途、成员 1 自然完成（脚本按派生次序消费）
    # Act：只取消成员 0，再取整组未取回成员
    interrupted = await interrupt.invoke(_interrupt_call(group_id, index=0), make_ctx())
    result = await wait.invoke(_wait_call(group_id), make_ctx())
    # Assert：单成员取消、兄弟成员照常回传；内核回执两态齐备
    assert interrupted.ok and interrupted.output["interrupted"] == [0]
    assert result.ok and [m["index"] for m in result.output["members"]] == [1]
    assert result.output["members"][0]["status"] == "completed"
    statuses = sorted(r.status for r in _all_receipts(slot))
    assert statuses == ["cancelled", "completed"]


# ── 审计与记账口径 ───────────────────────────────────────────────────────────


async def test_spawn审计留痕_注册与落定两段_含父run子run与预算份额():
    # Arrange：可捕获的审计汇
    records: list[dict[str, Any]] = []
    runner = FakeRunner(make_result(artifact={"summary": "结论"}, tokens_used=120))
    _, spawn, wait, _, _, _ = make_bindings(runner, probe=lambda: 1_000, share=0.5, audit_sink=records.append)
    # Act：注册（1 成员，申请 800）→ 等取 → 让回调收敛
    spawned = await spawn.invoke(_spawn_call(_task_entry(context_budget=800, objective="机密目标文本")), make_ctx())
    await wait.invoke(_wait_call(spawned.output["group_id"]), make_ctx())
    await _flush()
    # Assert：注册段（父 run/组/份额/实配）+ 落定段（子 run/状态/消耗）
    registered = next(r for r in records if r["outcome"] == "registered")
    settled = next(r for r in records if r["outcome"] == "settled")
    assert registered["parent_run_id"] == str(PARENT_RUN)
    assert registered["budget_share"] == 0.5 and registered["budgets"] == (500,)
    assert registered["group_id"] == spawned.output["group_id"]
    assert settled["child_run_id"] and settled["status"] == "completed" and settled["tokens_used"] == 120
    # 红线：objective 与 Artifact 正文不落审计（不可信内容只走 ToolResult/B3）
    dump = json.dumps(records, ensure_ascii=False, default=str)
    assert "机密目标文本" not in dump and "线路过载" not in dump


async def test_子消耗分账一次_内核结算_wait不重复记账():
    # Arrange：绑定父 tracker；子代理消耗 120
    runner = FakeRunner(make_result(tokens_used=120))
    _, spawn, wait, _, tracker, _ = make_bindings(runner, bind=True, max_tokens=1_000)
    # Act
    spawned = await spawn.invoke(_spawn_call(_task_entry()), make_ctx())
    result = await wait.invoke(_wait_call(spawned.output["group_id"]), make_ctx())
    # Assert：消耗只由内核 spawn_sub 结算记回一次（wait 结果不带 usage，防二次记账）
    assert result.ok and result.usage.get("total_tokens") is None
    assert tracker is not None and tracker.tokens_used == 120


async def test_三工具经分发器注册_行动类可寻址():
    # Arrange / Act：三绑定注册进内核分发器
    _, spawn, wait, interrupt, _, _ = make_bindings(FakeRunner())
    dispatcher = ExtensionDispatcher()
    for tool in (spawn, wait, interrupt):
        dispatcher.register_tool(tool)
    # Assert：行动类 IRI 一一寻址（B1 门禁按计划步行动类路由的前提）
    assert dispatcher.tool_for(SUBAGENT_SPAWN_ACTION_IRI) is spawn
    assert dispatcher.tool_for(SUBAGENT_WAIT_ACTION_IRI) is wait
    assert dispatcher.tool_for(SUBAGENT_INTERRUPT_ACTION_IRI) is interrupt


# ── 断言辅助（内核回执表为测试断言口，同 test_kernel_subagent 约定）──────────────


def _all_receipts(slot: Any) -> list[Any]:
    return list(slot._receipts.values())

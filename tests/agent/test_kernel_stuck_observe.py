# tests/agent/test_kernel_stuck_observe.py
"""K12 STUCK 观测态验收测试（docs/Agent/13 §18；上游对标 OpenHands@06 STUCK+nudge）。

覆盖面：
- K12-a 心跳记账：loop_guard 记账点扩展为心跳源——串行步循环逐步 + 并行段池前按段
  一次，刷新 Run 级 last_progress（时间戳+步号+进展指纹 token/新工具结果数）；
- K12-b STUCK 观测：连续 N 次记账无实质进展（进展指纹零增长，N=threshold 可配、
  0=关闭）或相邻记账点边界间隔 ≥ step_timeout_s 置 Run 级 stuck 标记并发
  kernel.run_stuck 观测事件（payload：停滞计数/阈值/触发面/当前步号/最近进展指纹）；
  **只观测不迁移**：Run/Step 状态机枚举与迁移表零改动（task.py 状态集合不变断言）；
  事件去重=同一 stuck 期至多一次（照 recheck_capped_emitted 先例），有实质进展解除
  后可再次置位；
- K12-c nudge 联动：置位时注入卡死引导（标界三重：source=kernel.stuck_watch、
  agent_attested、tier=3），同一 stuck 期至多一次；
- 端到端接线：串行步路径/并行段路径（B-①）、健康运行零事件、Run 终态语义不变。
"""

from __future__ import annotations

import uuid
from typing import Any

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.loop import AgentKernel
from services.agent.business.kernel.loop_guard import STUCK_EVENT, STUCK_NUDGE_SOURCE, StuckWatch
from services.agent.business.kernel.run_context import RunContext
from services.agent.domain.model.kernel_actions import StepResult
from services.agent.domain.model.kernel_context import ExtensionMeta, TrustLevel
from services.agent.domain.model.kernel_gates import GateFinding, GateReport, GateVerdict
from services.agent.domain.model.task import _VALID_RUN_TRANSITIONS, _VALID_TASK_TRANSITIONS, RunStatus, TaskStatus
from tests.agent.conftest import FakePlanner, FakeTool, make_candidate, make_ctx, make_step, make_task

_IRI = "http://ontology.example/action/read_data"


# ── 单元级桩与构造器 ────────────────────────────────────────────────────────
class _FakeClock:
    """可控单调时钟：beat 读 rc.tracker.elapsed_s（同源），测试显式推进 now。"""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _make_rc(clock: _FakeClock | None = None) -> RunContext:
    return RunContext(
        make_task(),
        make_ctx(),
        Budget(),
        clock=clock or _FakeClock(),
        approvals=(),
    )


def _collector() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    events: list[tuple[str, dict[str, Any]]] = []

    def emit(ledger: Any, ctx: Any, run_id: Any, event_type: str, data: dict[str, Any]) -> None:
        events.append((event_type, data))

    return events, emit


def _stuck_events(events: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [data for etype, data in events if etype == STUCK_EVENT]


def _add_result(rc: RunContext, seq: int) -> None:
    """模拟一步执行完成产出结果（进展指纹 tool_results +1）。"""
    rc.results[seq] = StepResult(
        step_id=uuid.uuid4(), run_id=rc.task.run_id, step_seq=seq, action_iri=f"{_IRI}_{seq}", ok=True
    )


class _SelectiveGate:
    """按 action_iri 黑名单拒绝的包 gate（停滞/进展混合计划的驱动源）。"""

    def __init__(self, reject_iris: set[str]) -> None:
        self.meta = ExtensionMeta(
            name="fixture.stuck_gate",
            version="1.0.0",
            semantic_annotation={"rule_iri": "http://ontology.example/rule/stuck观测"},
        )
        self.reject_iris = reject_iris

    async def check(self, decision: Any, ctx: Any, *, timeout_ms: int = 100) -> GateReport:
        if decision.action_iri in self.reject_iris:
            return GateReport(
                verdict=GateVerdict.REJECT,
                is_baseline=False,
                reporter=self.meta.name,
                findings=(
                    GateFinding(
                        focus=decision.action_iri,
                        rule_iri="pack.stuck.test",
                        severity="error",
                        code=3001,
                        message="测试用拒绝",
                    ),
                ),
            )
        return GateReport(verdict=GateVerdict.ALLOW, is_baseline=False, reporter=self.meta.name)


def _kernel(
    count: int,
    *,
    stuck_threshold: int | None = None,
    stuck_step_timeout_s: float | None = None,
    reject_iris: set[str] | None = None,
    parallelizable: bool = False,
    tool_sleep_s: float = 0.0,
    tool_parallelism: int | None = None,
) -> AgentKernel:
    """装配 kernel：n 个不同 action_iri 的串行 READ 步（避让 K1 循环检测）+ 可选停滞驱动。"""
    dispatcher = ExtensionDispatcher()
    for n in range(1, count + 1):
        dispatcher.register_tool(FakeTool(action_iri=f"{_IRI}_{n}", sleep_s=tool_sleep_s))
    steps = tuple(
        make_step(seq=n, action_iri=f"{_IRI}_{n}").model_copy(update={"parallelizable": True})
        if parallelizable
        else make_step(seq=n, action_iri=f"{_IRI}_{n}")
        for n in range(1, count + 1)
    )
    dispatcher.register_planning_strategy(FakePlanner(make_candidate(steps)))
    if reject_iris is not None:
        dispatcher.register_pre_gate(_SelectiveGate(reject_iris))
    kwargs: dict[str, Any] = {}
    if stuck_threshold is not None:
        kwargs["stuck_threshold"] = stuck_threshold
    if stuck_step_timeout_s is not None:
        kwargs["stuck_step_timeout_s"] = stuck_step_timeout_s
    if tool_parallelism is not None:
        kwargs["tool_parallelism"] = tool_parallelism
    return AgentKernel(dispatcher, **kwargs)


# ── K12-a/b：心跳记账与停滞置位（单元级）──────────────────────────────────
# 计数口径：首次记账=基线（无前序指纹可比，不计停滞）；此后每次记账较前序零增长
# =停滞 1 次。N=3 ⇒ 第 2~4 次记账累计 3 次停滞、第 4 次记账置位。
async def test_连续无进展达阈值_置stuck标记_事件payload完整() -> None:
    rc = _make_rc()
    events, emit = _collector()
    watch = StuckWatch(threshold=3, emit=emit)
    for seq in (1, 2, 3, 4):  # 首次=基线，其后 3 次记账零增长
        watch.beat(rc, seq)

    assert rc.is_stuck is True  # Run 级 stuck 观测标记已置位
    assert rc.stall_count == 3
    assert rc.last_progress is not None  # K12-a：心跳快照已记录
    assert rc.last_progress.step_seq == 4
    payload = _stuck_events(events)
    assert len(payload) == 1
    assert payload[0]["stall_count"] == 3
    assert payload[0]["threshold"] == 3
    assert payload[0]["trigger"] == "stall"
    assert payload[0]["step_seq"] == 4
    assert payload[0]["last_progress"]["tokens"] == rc.last_progress.tokens
    assert payload[0]["last_progress"]["tool_results"] == 0
    assert payload[0]["source"] == STUCK_NUDGE_SOURCE
    assert payload[0]["stage"] == "execution"


async def test_有实质进展_不置位_停滞计数清零() -> None:
    rc = _make_rc()
    events, emit = _collector()
    watch = StuckWatch(threshold=3, emit=emit)
    watch.beat(rc, 1)
    _add_result(rc, 1)  # 步 1 完成：新工具结果 → 进展指纹增长
    rc.tracker.add_estimated(50)  # token 增长
    watch.beat(rc, 2)
    watch.beat(rc, 3)
    _add_result(rc, 3)
    watch.beat(rc, 4)

    assert _stuck_events(events) == []
    assert rc.is_stuck is False
    assert rc.stall_count == 0
    assert rc.last_progress is not None and rc.last_progress.tool_results == 2


async def test_同一stuck期事件去重_至多一次() -> None:
    rc = _make_rc()
    events, emit = _collector()
    watch = StuckWatch(threshold=3, emit=emit)
    for seq in range(1, 8):  # 基线+连续 6 次停滞：仅首达阈值发一次
        watch.beat(rc, seq)

    payload = _stuck_events(events)
    assert len(payload) == 1  # 照 recheck_capped_emitted 每期一次先例
    assert payload[0]["stall_count"] == 3  # payload 记首达阈值时点
    assert rc.stall_count == 6
    assert rc.is_stuck is True


async def test_进展解除stuck期后_可再次置位_第二次事件() -> None:
    rc = _make_rc()
    events, emit = _collector()
    watch = StuckWatch(threshold=2, emit=emit)
    watch.beat(rc, 1)  # 基线
    watch.beat(rc, 2)  # 停滞 1
    watch.beat(rc, 3)  # 停滞 2 → 达阈值 → 第 1 次 stuck
    assert len(_stuck_events(events)) == 1
    _add_result(rc, 3)  # 实质进展 → 解除标记
    watch.beat(rc, 4)
    assert rc.is_stuck is False
    watch.beat(rc, 5)  # 停滞 1
    watch.beat(rc, 6)  # 停滞 2 → 再次达阈值 → 第 2 次 stuck

    payload = _stuck_events(events)
    assert len(payload) == 2
    assert [p["step_seq"] for p in payload] == [3, 6]
    assert rc.is_stuck is True


async def test_单步执行超时判据_边界间隔超X_置位_step_timeout() -> None:
    clock = _FakeClock()
    rc = _make_rc(clock)
    events, emit = _collector()
    watch = StuckWatch(threshold=0, step_timeout_s=5.0, emit=emit)  # 停滞判据关、仅超时判据
    watch.beat(rc, 1)
    clock.now += 6.0  # 上一步/段执行区间 6s ≥ 5s 阈值
    watch.beat(rc, 2)
    clock.now += 1.0  # 区间 1s < 5s：不触发
    watch.beat(rc, 3)

    payload = _stuck_events(events)
    assert len(payload) == 1
    assert payload[0]["trigger"] == "step_timeout"
    assert payload[0]["threshold"] == 0
    assert payload[0]["step_seq"] == 2
    assert rc.is_stuck is True
    # K12-c：卡死引导注入含超时语义文本
    nudges = [b for b in rc.context_blocks if b.source == STUCK_NUDGE_SOURCE]
    assert len(nudges) == 1
    assert nudges[0].trust_level is TrustLevel.AGENT_ATTESTED
    assert nudges[0].tier == 3
    assert "超时阈值" in nudges[0].content and "非用户指令" in nudges[0].content


async def test_阈值与超时全零_观测关闭_零事件零注入() -> None:
    clock = _FakeClock()
    rc = _make_rc(clock)
    events, emit = _collector()
    watch = StuckWatch(threshold=0, step_timeout_s=0.0, emit=emit)  # 0=关闭（watermark_recheck_max 同款语义）
    for seq in range(1, 6):
        clock.now += 100.0
        watch.beat(rc, seq)

    assert _stuck_events(events) == []
    assert rc.is_stuck is False
    assert rc.last_progress is None  # 零开销直通：连心跳快照都不记
    assert not [b for b in rc.context_blocks if b.source == STUCK_NUDGE_SOURCE]


def test_状态机零迁移_task_py状态集合不变() -> None:
    """K12 红线断言：只观测不迁移——task.py 状态机枚举与迁移表零改动（04 §3 状态主权）。"""
    assert {s.value for s in RunStatus} == {
        "queued",
        "running",
        "waiting_tool",
        "completed",
        "failed",
        "timeout",
        "cancelled",
    }
    assert not any("stuck" in s.value for s in RunStatus)  # 无 stuck 面
    assert {s.value for s in TaskStatus} == {"pending", "running", "succeeded", "failed", "cancelled"}
    assert {k: {v.value for v in vs} for k, vs in _VALID_RUN_TRANSITIONS.items()} == {
        "queued": {"running", "cancelled"},
        "running": {"waiting_tool", "completed", "failed", "timeout", "cancelled"},
        "waiting_tool": {"running", "cancelled"},
        "completed": set(),
        "failed": set(),
        "timeout": set(),
        "cancelled": set(),
    }
    assert {k: {v.value for v in vs} for k, vs in _VALID_TASK_TRANSITIONS.items()} == {
        "pending": {"running"},
        "running": {"succeeded", "failed", "cancelled"},
        "succeeded": set(),
        "failed": set(),
        "cancelled": set(),
    }


# ── 端到端接线（AgentKernel.run）──────────────────────────────────────────
async def test_端到端串行停滞_置位一次_nudge注入_Run终态语义不变() -> None:
    # 4 步全被包 gate 拒绝（零结果零 token 增长）：记账点=每步边界、步前——首次记账=基线，
    # threshold=2 → 第 3 步记账（第 2 次停滞）置位，第 4 步同 stuck 期去重；
    # 运行终态仍是正常 failed（无步通过），无 stuck 状态面
    kernel = _kernel(4, stuck_threshold=2, reject_iris={f"{_IRI}_{n}" for n in range(1, 5)})
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))

    assert outcome.status == str(RunStatus.FAILED)  # 只观测不迁移：终态仍是既有七态之一
    rc = kernel.last_run_context
    assert rc is not None and rc.is_stuck is True
    stuck = [
        e for e in (kernel.last_ledger.events if kernel.last_ledger else []) if e.event_type == STUCK_EVENT
    ]
    assert len(stuck) == 1
    assert stuck[0].data["stall_count"] == 2
    assert stuck[0].data["trigger"] == "stall"
    assert stuck[0].data["step_seq"] == 3
    # K12-c：卡死引导注入一次，标界三重（source/agent_attested/tier=3）+ 零成本留痕
    nudges = [b for b in rc.context_blocks if b.source == STUCK_NUDGE_SOURCE]
    assert len(nudges) == 1
    assert nudges[0].trust_level is TrustLevel.AGENT_ATTESTED
    assert nudges[0].tier == 3
    assert nudges[0].tokens == 0
    assert "卡死防护" in nudges[0].content and "非用户指令" in nudges[0].content


async def test_端到端_进展解除后再置位_两次事件() -> None:
    # 6 步：1/2/4/5/6 被拒（零进展），3 放行且成功（新结果=进展）——记账点=步前，故
    # 步 N 的产出在步 N+1 记账可见。threshold=2：第 3 步记账置位 → 第 4 步记账见步 3
    # 结果（进展）解除 → 第 6 步记账再置位（共 2 次事件）
    reject = {f"{_IRI}_{n}" for n in (1, 2, 4, 5, 6)}
    kernel = _kernel(6, stuck_threshold=2, reject_iris=reject)
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))

    assert outcome.status == str(RunStatus.FAILED)
    stuck = [
        e for e in (kernel.last_ledger.events if kernel.last_ledger else []) if e.event_type == STUCK_EVENT
    ]
    assert [e.data["step_seq"] for e in stuck] == [3, 6]  # 解除后可再次置位
    assert all(e.data["trigger"] == "stall" for e in stuck)
    rc = kernel.last_run_context
    assert rc is not None
    nudges = [b for b in rc.context_blocks if b.source == STUCK_NUDGE_SOURCE]
    assert len(nudges) == 2  # 每个 stuck 期各注入一次


async def test_端到端_并行段_段级心跳一次_不误计段内步() -> None:
    # 并行段路径：段=调度单元，每段池前记账一次（非段内逐步）。6 个 parallelizable
    # READ 步、parallelism=2 → segment_steps 切为 4 段 (1,2),(3,),(4,5),(6,)（满段后
    # 次步单步成段的既有分段行为），全被拒（零结果）→ 记账点=段首步 4 次：基线+3 停滞。
    # - threshold=2 → 第 3 次记账（段 (4,5) 段首步 4）置位；
    # - threshold=4 → 最多 3 次停滞不置位（若误按段内逐步记账，6 步=5 次停滞必置位）。
    common = dict(
        reject_iris={f"{_IRI}_{n}" for n in range(1, 7)},
        parallelizable=True,
        tool_parallelism=2,
    )
    kernel_fire = _kernel(6, stuck_threshold=2, **common)
    outcome_fire = await kernel_fire.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))
    kernel_quiet = _kernel(6, stuck_threshold=4, **common)
    outcome_quiet = await kernel_quiet.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))

    for kernel, outcome in ((kernel_fire, outcome_fire), (kernel_quiet, outcome_quiet)):
        event_types = {e.event_type for e in kernel.last_ledger.events}  # type: ignore[union-attr]
        assert "kernel.group_started" in event_types  # 并行段路径确被走到
        assert outcome.status == str(RunStatus.FAILED)
    stuck_fire = [
        e
        for e in (kernel_fire.last_ledger.events if kernel_fire.last_ledger else [])
        if e.event_type == STUCK_EVENT
    ]
    assert len(stuck_fire) == 1
    assert stuck_fire[0].data["stall_count"] == 2
    assert stuck_fire[0].data["step_seq"] == 4  # 第 3 个记账点=段 (4,5) 段首步
    stuck_quiet = [
        e
        for e in (kernel_quiet.last_ledger.events if kernel_quiet.last_ledger else [])
        if e.event_type == STUCK_EVENT
    ]
    assert stuck_quiet == []  # 段级心跳：4 段=3 次停滞 < 4，不置位（逐步记账则 5 次必置位）


async def test_端到端_健康运行_默认配置_零事件() -> None:
    # 4 步全部成功：每步边界都有新结果 → 停滞计数恒清零；默认阈值（Settings=3）下零事件
    kernel = _kernel(4)
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))

    assert outcome.status == str(RunStatus.COMPLETED)
    stuck = [
        e for e in (kernel.last_ledger.events if kernel.last_ledger else []) if e.event_type == STUCK_EVENT
    ]
    assert stuck == []
    rc = kernel.last_run_context
    assert rc is not None and rc.is_stuck is False and rc.stall_count == 0
    assert not [b for b in rc.context_blocks if b.source == STUCK_NUDGE_SOURCE]


async def test_端到端_单步超时_真实时钟触发_step_timeout() -> None:
    # 停滞判据关（threshold=0）、单步超时 10ms：每步真实执行 ≥50ms → 后续记账命中超时判据
    kernel = _kernel(3, stuck_threshold=0, stuck_step_timeout_s=0.01, tool_sleep_s=0.05)
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=10, duration_s=30))

    assert outcome.status == str(RunStatus.COMPLETED)  # 观测不迁移：超时判据不改变终态
    stuck = [
        e for e in (kernel.last_ledger.events if kernel.last_ledger else []) if e.event_type == STUCK_EVENT
    ]
    assert len(stuck) >= 1
    assert all(e.data["trigger"] == "step_timeout" for e in stuck)

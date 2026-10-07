# tests/agent/conftest.py
"""内核测试夹具：八个扩展点的 Fake 实现与构造器（02 §4.1 各通道最小实现桩）。

所有 Fake 均实现对应 Protocol（runtime_checkable，meta 带「命名空间.名称」+ semver +
非空语义标注），供分发器注册与内核 loop 消费。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.domain.model.kernel_actions import (
    ActionDecision,
    ApprovalTicket,
    CodeAction,
    ExecutionMode,
    SandboxLease,
    SandboxSpec,
    StepResult,
    ToolCall,
    ToolResult,
)
from services.agent.domain.model.kernel_context import (
    ContextBlock,
    ExtensionMeta,
    KernelEvent,
    SinkAck,
    StepRef,
    TaskRef,
    TenantContext,
    TrustLevel,
)
from services.agent.domain.model.kernel_gates import GateFinding, GateReport, GateVerdict
from services.agent.domain.model.kernel_planning import PlanCandidate, PlanMode, PlanStep

ACTION_IRI = "http://ontology.example/action/read_data"
WRITE_ACTION_IRI = "http://ontology.example/action/external_write"
CODE_ACTION_IRI = "http://ontology.example/action/run_code"
SCOPE_TOOL = "tool.exec"


def make_ctx(**kw: Any) -> TenantContext:
    base: dict[str, Any] = {
        "tenant_id": uuid.uuid4(),
        "roles": ("operator",),
        "scopes": ("tool.exec",),
        "trace_id": "trace-kernel-test",
    }
    base.update(kw)
    return TenantContext(**base)


def make_task(**kw: Any) -> TaskRef:
    base: dict[str, Any] = {
        "task_id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "task_iri": "http://ontology.example/task/停电分析",
        "objective": "分析线路停电原因并输出结论",
    }
    base.update(kw)
    return TaskRef(**base)


def make_step(
    seq: int = 1,
    action_iri: str = ACTION_IRI,
    mode: ExecutionMode = ExecutionMode.READ,
    params: dict[str, Any] | None = None,
    scopes: tuple[str, ...] = (SCOPE_TOOL,),
    schema: dict[str, Any] | None = None,
) -> PlanStep:
    return PlanStep(
        seq=seq,
        action_iri=action_iri,
        execution_mode=mode,
        parameters=params if params is not None else {"q": "线路A"},
        required_scopes=scopes,
        parameter_schema=schema
        if schema is not None
        else {
            "required": ["q"],
            "properties": {"q": {"type": "string"}, "network": {"type": "boolean"}},
        },
    )


def make_candidate(steps: tuple[PlanStep, ...], criteria: tuple[Any, ...] = ()) -> PlanCandidate:
    return PlanCandidate(strategy_name="fixture.planner", steps=steps, success_criteria=criteria)


def make_tool_dispatcher(tool: Any, **extra_registrations: Any) -> ExtensionDispatcher:
    """注册一个工具绑定的分发器；extra 以关键字传入其他注册函数名→参数元组。"""
    dispatcher = ExtensionDispatcher()
    if tool is not None:
        dispatcher.register_tool(tool)
    for register_name, args in extra_registrations.items():
        getattr(dispatcher, register_name)(*args)
    return dispatcher


# ── ① context.providers ─────────────────────────────────────────────────
class FakeProvider:
    def __init__(
        self,
        *,
        content: str = "图谱检索结果",
        tokens: int = 100,
        trust_level: TrustLevel = TrustLevel.AGENT_ATTESTED,
    ) -> None:
        self.meta = ExtensionMeta(
            name="fixture.graph_provider",
            version="1.0.0",
            semantic_annotation={"concept_iri": "http://ontology.example/concept/电网"},
        )
        self.content = content
        self.tokens = tokens
        self.trust_level = trust_level
        self.received_ctx: TenantContext | None = None

    async def provide(
        self,
        task: TaskRef,
        step: StepRef | None,
        ctx: TenantContext,
        *,
        budget_tokens: int,
        timeout_ms: int = 3_000,
    ) -> ContextBlock:
        self.received_ctx = ctx
        return ContextBlock(
            source=self.meta.name, content=self.content, tokens=self.tokens, trust_level=self.trust_level
        )


# ── ② planning.strategies ───────────────────────────────────────────────
class FakePlanner:
    def __init__(self, candidate: PlanCandidate) -> None:
        self.meta = ExtensionMeta(
            name="fixture.planner",
            version="1.0.0",
            semantic_annotation={"rule_iri": "http://ontology.example/rule/停电规划"},
        )
        self.candidate = candidate

    async def plan(
        self, task: TaskRef, ctx: TenantContext, *, mode: PlanMode = PlanMode.TEMPLATE, timeout_ms: int = 10_000
    ) -> PlanCandidate:
        return self.candidate


# ── ③ gates.pre / gates.post ────────────────────────────────────────────
class FakePackGate:
    """包 gate（可配置其报告：放行/拒绝/自称基线）。"""

    def __init__(self, *, report: GateReport | None = None, sleep_s: float = 0.0) -> None:
        self.meta = ExtensionMeta(
            name="fixture.pack_gate",
            version="1.0.0",
            semantic_annotation={"rule_iri": "http://ontology.example/rule/行业规则包"},
        )
        self.report = report or GateReport(verdict=GateVerdict.ALLOW, is_baseline=False, reporter=self.meta.name)
        self.sleep_s = sleep_s

    async def check(self, decision: ActionDecision, ctx: TenantContext, *, timeout_ms: int = 100) -> GateReport:
        if self.sleep_s:
            await asyncio.sleep(self.sleep_s)
        return self.report


class FakeTamperingGate(FakePackGate):
    """篡改者：返回自称基线的报告（铁律 1 负向测试用）。"""

    def __init__(self) -> None:
        super().__init__()
        self.report = GateReport(verdict=GateVerdict.ALLOW, is_baseline=True, reporter=self.meta.name)


class FakePostGate:
    def __init__(self, *, ok: bool = True) -> None:
        self.meta = ExtensionMeta(
            name="fixture.post_gate",
            version="1.0.0",
            semantic_annotation={"rule_iri": "http://ontology.example/rule/后验shape包"},
        )
        self.ok = ok

    async def validate(self, result: StepResult, ctx: TenantContext, *, timeout_ms: int = 1_000) -> Any:
        from services.agent.domain.model.kernel_gates import ValidationReport

        return ValidationReport(
            ok=self.ok,
            validator=self.meta.name,
            findings=()
            if self.ok
            else (
                GateFinding(
                    focus=result.action_iri,
                    rule_iri="pack.post.shape",
                    severity="error",
                    code=3001,
                    message="产物不符合 shape 包",
                ),
            ),
        )


# ── ④ tools.bindings（ToolPort）─────────────────────────────────────────
class FakeTool:
    """最小工具实现：可配置成功/失败/usage/自报信任级/产出租户声明/延时。"""

    def __init__(
        self,
        action_iri: str = ACTION_IRI,
        *,
        ok: bool = True,
        output: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        trust_level: TrustLevel | None = None,
        sleep_s: float = 0.0,
        zombie: bool = False,
    ) -> None:
        self.meta = ExtensionMeta(name="fixture.tool", version="1.0.0", semantic_annotation={"action_iri": action_iri})
        self.ok = ok
        self.output = output if output is not None else {"rows": 3}
        self.usage = usage or {}
        self.trust_level = trust_level
        self.sleep_s = sleep_s
        self.zombie = zombie  # 吞取消的「卡死」实现（§2.4 5s 强制分支）
        self.calls: list[ToolCall] = []

    async def invoke(
        self,
        call: ToolCall,
        ctx: TenantContext,
        *,
        approval: ApprovalTicket | None = None,
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        self.calls.append(call)
        if self.zombie:
            try:
                return await asyncio.shield(asyncio.sleep(30.0)) or ToolResult(ok=True)
            except asyncio.CancelledError:
                # 模拟不可中止实现：吞掉取消再自行收尾（收尾耗时超过清单 5s 兜底窗口可被强制）
                await asyncio.sleep(1.2)
                return ToolResult(ok=True)
        if self.sleep_s:
            await asyncio.sleep(self.sleep_s)
        return ToolResult(
            ok=self.ok,
            output=self.output,
            error_code=None if self.ok else 5003,
            error_message=None if self.ok else "目标不可用",
            usage=self.usage,
            trust_level=self.trust_level or TrustLevel.AGENT_ATTESTED,
        )


# ── ⑤ reasoning.engines / ⑥ memory.policies（注册面演示桩）──────────────
class FakeReasoningEngine:
    def __init__(self) -> None:
        self.meta = ExtensionMeta(
            name="fixture.reasoner",
            version="1.0.0",
            semantic_annotation={"rule_iri": "http://ontology.example/rule/一致性"},
        )

    async def run(self, request: Any, ctx: TenantContext, *, timeout_ms: int = 60_000) -> Any:
        from services.agent.domain.model.kernel_planning import ReasoningResult

        return ReasoningResult(ok=True)


class FakeMemoryPolicy:
    def __init__(self) -> None:
        self.meta = ExtensionMeta(
            name="fixture.memory",
            version="1.0.0",
            semantic_annotation={"concept_iri": "http://ontology.example/concept/记忆"},
        )

    async def judge(self, candidates: list[Any], ctx: TenantContext, *, timeout_ms: int = 5_000) -> list[Any]:
        return []

    def weight(self, item: Any, ctx: TenantContext) -> float:
        return 0.5


# ── ⑦ event.sinks ───────────────────────────────────────────────────────
class FakeSink:
    def __init__(self) -> None:
        self.meta = ExtensionMeta(
            name="fixture.sink",
            version="1.0.0",
            semantic_annotation={"concept_iri": "http://ontology.example/concept/通知"},
        )
        self.handled: list[KernelEvent] = []

    async def handle(self, events: list[KernelEvent], ctx: TenantContext, *, timeout_ms: int = 5_000) -> SinkAck:
        self.handled.extend(events)
        return SinkAck(accepted_event_ids=tuple(e.event_id for e in events))


# ── ⑧ execution.backends ────────────────────────────────────────────────
class FakeBackend:
    """沙箱后端桩：登记活跃租约（取消完整性「零残留」验收断言口）。"""

    def __init__(self, *, run_sleep_s: float = 0.0, hang_release: bool = False) -> None:
        self.meta = ExtensionMeta(
            name="fixture.backend",
            version="1.0.0",
            semantic_annotation={"action_iri": CODE_ACTION_IRI},
        )
        self.run_sleep_s = run_sleep_s
        self.hang_release = hang_release
        self.active_leases: list[SandboxLease] = []
        self.acquired_specs: list[SandboxSpec] = []
        self.released: list[SandboxLease] = []
        self._release_event = asyncio.Event()

    async def acquire(self, spec: SandboxSpec, ctx: TenantContext, *, timeout_ms: int = 60_000) -> SandboxLease:
        self.acquired_specs.append(spec)
        lease = SandboxLease(backend_name=self.meta.name, sandbox_ref="container-1")
        self.active_leases.append(lease)
        return lease

    async def run(
        self, lease: SandboxLease, action: CodeAction, ctx: TenantContext, *, timeout_ms: int = 120_000
    ) -> Any:
        from services.agent.domain.model.kernel_actions import ExecutionResult

        if self.run_sleep_s:
            await asyncio.sleep(self.run_sleep_s)
        return ExecutionResult(ok=True, exit_code=0, output={"stdout": "ok"})

    async def release(self, lease: SandboxLease, ctx: TenantContext, *, timeout_ms: int = 5_000) -> None:
        if self.hang_release:
            await self._release_event.wait()  # 模拟持有方不肯优雅释放（清单 5s 强制分支）
        self.active_leases = [x for x in self.active_leases if x.lease_id != lease.lease_id]
        self.released.append(lease)


# ── L7 ModelPort（平台既有端口）─────────────────────────────────────────
class FakeModel:
    """ModelPort 桩：返回确定性计划 JSON（推理分级：输出过 Schema 才可用）。"""

    def __init__(self, plan: dict[str, Any] | None = None) -> None:
        self.plan = plan or {"steps": [{"seq": 1, "action_iri": ACTION_IRI}]}
        self.calls = 0

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any],
        timeout_s: float = 60.0,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        return self.plan


@pytest.fixture
def read_step() -> PlanStep:
    return make_step()


@pytest.fixture
def dispatcher_with_tool() -> ExtensionDispatcher:
    return make_tool_dispatcher(FakeTool())

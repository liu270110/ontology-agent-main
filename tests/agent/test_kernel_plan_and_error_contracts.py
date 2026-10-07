# tests/agent/test_kernel_plan_and_error_contracts.py
"""内核计划验收与错误契约测试（02 §11.2 源码勘察吸收裁决的实现验证）。

- 细节 1（hermes）：工具 schema **计划验收期**校验——坏 parameter_schema 在三层校验
  结构层报错（计划被拒），不拖到运行期门禁才炸；
- 细节 2（hermes）：工具错误结构化 + **2048 字符硬截断**——「错误即反馈」回流防灌爆。
"""

from __future__ import annotations

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.execution import ExecutionStage
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_actions import StepResult, ToolResult
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.agent.domain.model.kernel_gates import ValidationReport
from tests.agent.conftest import (
    FakePlanner,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)

_BUDGET = Budget(max_tokens=10_000, max_steps=5, duration_s=30.0)


def _kernel_with_plan(steps, *, tool=None, post_gate=None) -> AgentKernel:
    dispatcher = make_tool_dispatcher(tool if tool is not None else FakeTool())
    dispatcher.register_planning_strategy(FakePlanner(make_candidate(steps)))
    if post_gate is not None:
        dispatcher.register_post_gate(post_gate)
    return AgentKernel(dispatcher)


# ── 细节 1：parameter_schema 计划验收期校验（结构层）────────────────────────


async def test_坏schema_required引用未声明属性_计划验收期拒绝():
    from services.agent.domain.model.kernel_planning import PlanStep

    bad = PlanStep(
        seq=1,
        action_iri="http://ontology.example/action/read_data",
        parameters={"q": "线路A"},
        parameter_schema={"type": "object", "required": ["ghost"], "properties": {"q": {"type": "string"}}},
        required_scopes=("tool.exec",),
    )
    outcome = await _kernel_with_plan((bad,)).run(make_task(), make_ctx(), budget=_BUDGET)
    assert outcome.status == "failed"
    assert "parameter_schema" in outcome.reason and "结构层" in outcome.reason


async def test_坏schema_非object类型_计划验收期拒绝():
    from services.agent.domain.model.kernel_planning import PlanStep

    bad = PlanStep(
        seq=1,
        action_iri="http://ontology.example/action/read_data",
        parameter_schema={"type": "string"},
        required_scopes=("tool.exec",),
    )
    outcome = await _kernel_with_plan((bad,)).run(make_task(), make_ctx(), budget=_BUDGET)
    assert outcome.status == "failed"
    assert "object Schema" in outcome.reason


async def test_空schema与合法schema_放行():
    """空 Schema（chat 模板档形态）与 well-formed Schema 均验收通过。"""
    outcome = await _kernel_with_plan((make_step(),)).run(make_task(), make_ctx(), budget=_BUDGET)
    assert outcome.status == "completed"


# ── 细节 2：工具错误正文 2048 硬截断 ────────────────────────────────────────


def test_truncate_error_纯函数契约():
    long_message = "x" * 5000
    result = ToolResult(ok=False, error_code=5003, error_message=long_message)
    truncated = ExecutionStage.truncate_error(result)
    assert len(truncated.error_message) == 2048  # 硬截断
    assert len(result.error_message) == 5000  # 原结果不可变（frozen 值语义，copy 生效）
    ok_result = ToolResult(ok=True, output={"rows": 1})
    assert ExecutionStage.truncate_error(ok_result) is ok_result  # 成功结果不受影响
    short = ToolResult(ok=False, error_code=5003, error_message="短错误")
    assert ExecutionStage.truncate_error(short) is short  # 未超限原样返回


class _CapturingPostGate:
    """捕获后验入参的空转门禁：观测执行断点产出的 StepResult（截断发生时点验证）。"""

    meta = ExtensionMeta(
        name="fixture.capture_post_gate",
        version="1.0.0",
        semantic_annotation={"rule_iri": "http://ontology.example/rule/捕获"},
    )

    def __init__(self) -> None:
        self.seen: list[StepResult] = []

    async def validate(self, result: StepResult, ctx: TenantContext, *, timeout_ms: int = 1_000) -> ValidationReport:
        self.seen.append(result)
        return ValidationReport(ok=True, validator=self.meta.name)


async def test_内核执行路径_超长错误在后验前已截断():
    class NoisyTool(FakeTool):
        def __init__(self) -> None:
            super().__init__(ok=False)

        async def invoke(self, call, ctx, *, approval=None, timeout_ms=30_000):
            self.calls.append(call)
            return ToolResult(ok=False, error_code=5003, error_message="e" * 9999)

    gate = _CapturingPostGate()
    outcome = await _kernel_with_plan((make_step(),), tool=NoisyTool(), post_gate=gate).run(
        make_task(), make_ctx(), budget=_BUDGET
    )
    assert outcome.status == "failed"  # 步失败（工具报错），但产物仍走后验通道
    assert len(gate.seen) == 1
    seen = gate.seen[0].tool_result
    assert seen is not None and seen.error_message is not None
    assert len(seen.error_message) == 2048  # 截断发生在后验（回流）之前

# tests/agent/test_kernel_b1_gate_baseline.py
"""B1 门禁基线负向测试（02 §2 B1、§7.4 铁律 1）：基线四规则 + 包 gate 只增不替 + 篡改基线被拒。"""

from __future__ import annotations

import pytest

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.errors import KernelContractError
from services.agent.business.kernel.gate_baseline import BaselineGate, canonical_param_hash
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_actions import ActionDecision, ApprovalTicket
from services.agent.domain.model.kernel_context import TenantContext
from services.agent.domain.model.kernel_gates import GateReport, GateVerdict
from services.agent.domain.model.step_state import LoopStage, StepStatus
from services.agent.domain.model.task import RunStatus
from services.platform.errors import ErrorCode
from tests.agent.conftest import (
    ACTION_IRI,
    FakePackGate,
    FakePlanner,
    FakeTamperingGate,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)


def _decision(params: dict | None = None) -> ActionDecision:
    return ActionDecision(
        action_iri=ACTION_IRI,
        parameters=params if params is not None else {"q": "线路A"},
        step_seq=1,
    )


def _ctx(scopes: tuple[str, ...] = ("tool.exec",)) -> TenantContext:
    return make_ctx(scopes=scopes)


def _step(schema: dict | None = None, scopes: tuple[str, ...] = ("tool.exec",)):
    return make_step(schema=schema, scopes=scopes)


def test_行动类未注册工具绑定_基线拒绝():
    report = BaselineGate().check(_decision(), _step(), _ctx(), tool_bound=False, approval=None, param_hash="x")
    assert report.verdict is GateVerdict.REJECT
    assert report.is_baseline
    assert report.findings[0].code == int(ErrorCode.PARAM_INVALID)


def test_缺必填参数_基线拒绝():
    report = BaselineGate().check(_decision(params={}), _step(), _ctx(), tool_bound=True, approval=None, param_hash="x")
    assert report.verdict is GateVerdict.REJECT
    assert any("缺少必填参数" in f.message for f in report.findings)


def test_参数类型错误与未知参数_基线拒绝():
    schema = {
        "required": ["q"],
        "properties": {"q": {"type": "string"}, "limit": {"type": "integer"}},
        "additionalProperties": False,
    }
    decision = _decision(params={"q": 123, "limit": "many", "extra": 1})
    report = BaselineGate().check(
        decision, _step(schema=schema), _ctx(), tool_bound=True, approval=None, param_hash="x"
    )
    assert report.verdict is GateVerdict.REJECT
    messages = [f.message for f in report.findings]
    assert any("类型须为 string" in m for m in messages)
    assert any("类型须为 integer" in m for m in messages)
    assert any("未知参数: extra" in m for m in messages)


def test_参数不在枚举域_基线拒绝():
    schema = {"required": ["mode"], "properties": {"mode": {"type": "string", "enum": ["fast", "deep"]}}}
    report = BaselineGate().check(
        _decision(params={"mode": "yolo"}),
        _step(schema=schema),
        _ctx(),
        tool_bound=True,
        approval=None,
        param_hash="x",
    )
    assert report.verdict is GateVerdict.REJECT
    assert any("枚举域" in f.message for f in report.findings)


def test_scope不足_基线拒绝():
    report = BaselineGate().check(_decision(), _step(), _ctx(scopes=()), tool_bound=True, approval=None, param_hash="x")
    assert report.verdict is GateVerdict.REJECT
    assert report.findings[0].code == int(ErrorCode.SCOPE_INSUFFICIENT)


def test_高风险行动缺审批回执_门禁放行_路由至B5执行阶段默认拒绝():
    """缺回执不是门禁违例：交由执行阶段 waiting_approval 支「超时默认拒绝」（04 §3 状态机）。"""
    from services.agent.domain.model.kernel_actions import ExecutionMode

    decision = ActionDecision(
        action_iri=ACTION_IRI,
        execution_mode=ExecutionMode.EXTERNAL_WRITE,
        parameters={"q": "x"},
        step_seq=1,
    )
    report = BaselineGate().check(decision, _step(), _ctx(), tool_bound=True, approval=None, param_hash="x")
    assert report.verdict is GateVerdict.ALLOW  # 门禁放行 → B5 路由在执行阶段拒绝


def test_审批回执参数哈希不匹配_基线拒绝_防换参重放():
    from services.agent.domain.model.kernel_actions import ExecutionMode

    decision = ActionDecision(
        action_iri=ACTION_IRI,
        execution_mode=ExecutionMode.EXTERNAL_WRITE,  # 高级行动才触发审批校验
        parameters={"q": "线路A"},
        step_seq=1,
    )
    report = BaselineGate().check(
        decision,
        _step(),
        _ctx(),
        tool_bound=True,
        approval=ApprovalTicket(param_hash="stale-hash"),
        param_hash="fresh-hash",
    )
    assert report.verdict is GateVerdict.REJECT
    assert any("哈希" in f.message for f in report.findings)


def test_read行动误携审批回执_不受审批规则约束():
    report = BaselineGate().check(
        _decision(),
        _step(),
        _ctx(),
        tool_bound=True,
        approval=ApprovalTicket(param_hash="stale-hash"),
        param_hash="fresh-hash",
    )
    assert report.verdict is GateVerdict.ALLOW  # read 行动不需要审批，误携回执不影响基线


def test_参数哈希_同参同哈希_异参异哈希_值不经采样():
    assert canonical_param_hash({"a": 1, "b": 2}) == canonical_param_hash({"b": 2, "a": 1})
    assert canonical_param_hash({"a": 1}) != canonical_param_hash({"a": 2})


async def test_包gate只增不替_基线拒绝不被包gate翻案():
    # 包 gate 返回放行，但基线因 scope 不足拒绝 → 合成结论必须仍是拒绝
    kernel = AgentKernel(
        make_tool_dispatcher(
            FakeTool(),
            register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),),
            register_pre_gate=(FakePackGate(report=GateReport(verdict=GateVerdict.ALLOW, is_baseline=False)),),
        )
    )
    outcome = await kernel.run(make_task(), make_ctx(scopes=()), budget=Budget(max_steps=5, duration_s=10))
    state = outcome.terminal_states[0]
    assert state.status is StepStatus.FAILED
    assert state.gate_verdict == "reject"
    assert state.stage == LoopStage.GATE


async def test_包gate自称基线_篡改基线权威_整步拒绝():
    kernel = AgentKernel(
        make_tool_dispatcher(
            FakeTool(),
            register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),),
            register_pre_gate=(FakeTamperingGate(),),
        )
    )
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == str(RunStatus.FAILED)
    assert "自称基线" in outcome.reason  # compose 拒绝篡改（铁律 1）


async def test_包gate超时_按拒绝合成_基线放行结论被包gate否决():
    kernel = AgentKernel(
        make_tool_dispatcher(
            FakeTool(),
            register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),),
            register_pre_gate=(FakePackGate(sleep_s=1.5),),  # 超过 _GATE_TIMEOUT_S=1s
        )
    )
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=30))
    state = outcome.terminal_states[0]
    assert state.status is StepStatus.FAILED
    assert state.stage == LoopStage.GATE  # 包 gate 超时按拒绝合成（基线不受影响）


def test_合成顺序固定_基线先包后_包违例仅追加():
    # 只增不替语义：包 gate 的结论字段不作数，唯一货币是「新增违例」（findings）
    baseline = GateReport(verdict=GateVerdict.ALLOW, is_baseline=True)
    pack = GateReport(verdict=GateVerdict.REJECT, is_baseline=False, reporter="pack.x", findings=())
    merged = BaselineGate.compose(baseline, (pack,))
    assert merged.passed
    with pytest.raises(KernelContractError, match="自称基线"):
        BaselineGate.compose(
            baseline,
            (GateReport(verdict=GateVerdict.REJECT, is_baseline=True, reporter="rogue"),),
        )

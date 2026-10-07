# tests/agent/test_kernel_b4_egress.py
"""B4 出口控制负向测试（02 §2 B4；DSec §6.5 v1 降级承诺）：默认无网，network 请求一律拒绝。"""

from __future__ import annotations

import pytest

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.errors import KernelContractError
from services.agent.business.kernel.execution import ExecutionStage
from services.agent.business.kernel.gate_baseline import canonical_param_hash
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_actions import ApprovalTicket, ExecutionMode, SandboxSpec
from services.agent.domain.model.kernel_planning import PlanStep
from services.agent.domain.model.task import RunStatus
from tests.agent.conftest import (
    CODE_ACTION_IRI,
    FakeBackend,
    FakePlanner,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)


def test_沙箱规格恒无网_内核构造不允许能力侧开启网络():
    spec = ExecutionStage.sandbox_spec(
        PlanStep(
            seq=1,
            action_iri=CODE_ACTION_IRI,
            execution_mode=ExecutionMode.CODE,
            parameters={"image": "platform/sandbox:9", "code": "print(1)"},
            parameter_schema={},
            required_scopes=(),
        )
    )
    assert spec.network_enabled is False  # B4 硬编码项，后端实现也不得覆盖


def test_code行动请求network_内核拒绝构造规格():
    step = PlanStep(
        seq=1,
        action_iri=CODE_ACTION_IRI,
        execution_mode=ExecutionMode.CODE,
        parameters={"code": "curl http://evil.example", "network": True},
        parameter_schema={},
        required_scopes=(),
    )
    with pytest.raises(KernelContractError, match="network 请求被拒"):
        ExecutionStage.sandbox_spec(step)


async def test_code行动network请求_运行期被拒并产终态():
    tool = FakeTool()
    planner = FakePlanner(
        make_candidate(
            (
                make_step(
                    seq=1,
                    action_iri=CODE_ACTION_IRI,
                    mode=ExecutionMode.CODE,
                    params={"code": "curl http://evil.example", "network": True},
                    scopes=(),
                    schema={
                        "properties": {
                            "code": {"type": "string"},
                            "network": {"type": "boolean"},
                            "image": {"type": "string"},
                        }
                    },
                ),
            )
        )
    )
    kernel = AgentKernel(
        make_tool_dispatcher(
            tool,
            register_planning_strategy=(planner,),
            register_execution_backend=(FakeBackend(),),
        )
    )
    ticket = ApprovalTicket(param_hash=canonical_param_hash({"code": "curl http://evil.example", "network": True}))
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10), approvals=(ticket,))
    assert outcome.status == str(RunStatus.FAILED)
    assert "network 请求被拒" in outcome.reason


async def test_code行动无执行后端_拒绝():
    planner = FakePlanner(
        make_candidate(
            (
                make_step(
                    seq=1,
                    action_iri=CODE_ACTION_IRI,
                    mode=ExecutionMode.CODE,
                    params={"code": "1+1"},
                    scopes=(),
                    schema={"properties": {"code": {"type": "string"}}},
                ),
            )
        )
    )
    kernel = AgentKernel(make_tool_dispatcher(FakeTool(), register_planning_strategy=(planner,)))
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == str(RunStatus.FAILED)
    assert "ExecutionBackend" in outcome.reason


async def test_白名单规格正常路径_沙箱执行成功且租约释放():
    backend = FakeBackend()
    planner = FakePlanner(
        make_candidate(
            (
                make_step(
                    seq=1,
                    action_iri=CODE_ACTION_IRI,
                    mode=ExecutionMode.CODE,
                    params={"code": "print('停电分析')"},
                    scopes=(),
                    schema={
                        "properties": {
                            "code": {"type": "string"},
                            "network": {"type": "boolean"},
                            "image": {"type": "string"},
                        }
                    },
                ),
            )
        )
    )
    kernel = AgentKernel(
        make_tool_dispatcher(FakeTool(), register_planning_strategy=(planner,), register_execution_backend=(backend,))
    )
    ticket = ApprovalTicket(param_hash=canonical_param_hash({"code": "print('停电分析')"}))
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10), approvals=(ticket,))
    assert outcome.status == str(RunStatus.COMPLETED)
    assert backend.acquired_specs[0].network_enabled is False
    assert backend.active_leases == []  # 正常路径租约释放、钩子摘除
    assert outcome.terminal_states[0].status.value == "validated"


# ── K18-a 门 3 通道（方案=docs/Agent/13 §24）：SandboxSpec.env 受控供给通道 ──────────


def test_沙箱规格env缺省None_序列化向后兼容():
    """K18-a：缺省构造 env=None；model_dump 既有字段形状不变，env 以 null 落盘可回读。"""
    spec = SandboxSpec(image="platform/sandbox:default")
    assert spec.env is None
    dumped = spec.model_dump()
    # 既有字段零变化（向后兼容面）：仅新增 env=null 键
    assert dumped == {
        "image": "platform/sandbox:default",
        "network_enabled": False,
        "egress_whitelist": (),
        "cpu_limit": "1.0",
        "memory_limit": "512m",
        "env": None,
    }
    # null 载荷可原样回读（序列化往返）
    assert SandboxSpec.model_validate(dumped).env is None


def test_规格构造_env参数映射进规格通道():
    """K18-a：parameters.env 非 None → 映射入 SandboxSpec.env（值字符串化，通道值原样保留）。"""
    spec = ExecutionStage.sandbox_spec(
        PlanStep(
            seq=1,
            action_iri=CODE_ACTION_IRI,
            execution_mode=ExecutionMode.CODE,
            parameters={
                "image": "platform/sandbox:9",
                "code": "print(1)",
                "env": {"SKILL_TOKEN": "tok-123", "RETRIES": 3},
            },
            parameter_schema={},
            required_scopes=(),
        )
    )
    assert spec.env == {"SKILL_TOKEN": "tok-123", "RETRIES": "3"}


def test_规格构造_无env参数_不注env保持缺省None():
    """K18-a：parameters.env 缺省 → 不注 env，保持缺省 None（既有路径零差）。"""
    spec = ExecutionStage.sandbox_spec(
        PlanStep(
            seq=1,
            action_iri=CODE_ACTION_IRI,
            execution_mode=ExecutionMode.CODE,
            parameters={"image": "platform/sandbox:9", "code": "print(1)"},
            parameter_schema={},
            required_scopes=(),
        )
    )
    assert spec.env is None


def test_规格构造_env非映射_契约违规拒绝():
    """K18-a 负向：env 通道只收字符串映射，非映射入参走内核结构化契约错误。"""
    step = PlanStep(
        seq=1,
        action_iri=CODE_ACTION_IRI,
        execution_mode=ExecutionMode.CODE,
        parameters={"code": "1+1", "env": "API_KEY=sk-leak"},
        parameter_schema={},
        required_scopes=(),
    )
    with pytest.raises(KernelContractError, match="env 通道契约违规"):
        ExecutionStage.sandbox_spec(step)


async def test_code行动带env参数_通道值随规格透传至执行后端():
    """K18-a 端到端（内核侧一跳）：env 通道值经 sandbox_spec 随规格到达后端 acquire；宿主 env 不涉入。"""
    params = {"code": "print('停电分析')", "env": {"SKILL_TOKEN": "tok-k18"}}
    backend = FakeBackend()
    planner = FakePlanner(
        make_candidate(
            (
                make_step(
                    seq=1,
                    action_iri=CODE_ACTION_IRI,
                    mode=ExecutionMode.CODE,
                    params=params,
                    scopes=(),
                    schema={
                        "properties": {
                            "code": {"type": "string"},
                            "env": {"type": "object"},
                        }
                    },
                ),
            )
        )
    )
    kernel = AgentKernel(
        make_tool_dispatcher(FakeTool(), register_planning_strategy=(planner,), register_execution_backend=(backend,))
    )
    ticket = ApprovalTicket(param_hash=canonical_param_hash(params))
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10), approvals=(ticket,))
    assert outcome.status == str(RunStatus.COMPLETED)
    assert backend.acquired_specs[0].env == {"SKILL_TOKEN": "tok-k18"}  # 通道值受控透传
    assert backend.active_leases == []


def test_env通道_嵌套值fail_closed_标量白名单外拒():
    """ocr 2026-10-07：dict/list 值经 str() 会把 Python repr 垃圾静默送进容器——标量外 fail-closed。"""
    step = PlanStep(
        seq=1,
        action_iri=CODE_ACTION_IRI,
        execution_mode=ExecutionMode.CODE,
        parameters={"code": "x", "env": {"CFG": {"retries": 3}}},
        parameter_schema={},
        required_scopes=(),
    )
    with pytest.raises(KernelContractError, match="env 值必须为标量"):
        ExecutionStage.sandbox_spec(step)

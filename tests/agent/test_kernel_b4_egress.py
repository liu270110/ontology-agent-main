# tests/agent/test_kernel_b4_egress.py
"""B4 出口控制负向测试（02 §2 B4；DSec §6.5 v1 降级承诺）：默认无网，network 请求一律拒绝。"""

from __future__ import annotations

import pytest
from conftest import (
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

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.errors import KernelContractError
from services.agent.business.kernel.execution import ExecutionStage
from services.agent.business.kernel.gate_baseline import canonical_param_hash
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_actions import ApprovalTicket, ExecutionMode
from services.agent.domain.model.kernel_planning import PlanStep
from services.agent.domain.model.task import RunStatus


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

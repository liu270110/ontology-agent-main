# tests/agent/test_kernel_h5_usage_floor.py
"""H5 预算伪造防线测试（红队审查 docs/评审/红队攻击性审查-2026-10-06 §5，2026-10-07 修复批）。

覆盖（红队 H5：工具谎报 usage=1 即逃逸预算）：
- 谎报正数低报（usage=1 + 大产出）→ kernel.usage_adjusted 调整事件（usage_estimated=true）
  + 按启发式估算值入账（booked > reported）；
- 估算入账「咬」预算：调整后的入账值（非谎报值）触发 token 维预算耗尽 → 5005 优雅终止；
- usage=0/负数维持既有拒收不变（不入账、不调整、零事件）；
- 诚实回传零行为变化（回传值 ≥ 下界 → 无调整事件、原值入账）。
桩：conftest FakeTool/FakePlanner + 内核直跑（零真网零真库，test_kernel_a4_budget 同款）。
"""

from __future__ import annotations

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.loop import AgentKernel
from services.agent.business.kernel.spill import serialize_output
from services.agent.domain.model.task import RunStatus
from services.platform.config import get_settings
from tests.agent.conftest import (
    FakePlanner,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)

_BIG_OUTPUT = {"blob": "x" * 400}  # 序列化后 ~412 字符 → 估算 ~103 tokens


def _expected_booked(output: dict) -> int:
    """与 _floor_usage 同式复算期望入账值（ratio 读 Settings，估算只吃字符维——零时长路径）。"""
    est = len(serialize_output(output)) / 4
    return int(est * get_settings().kernel_usage_floor_ratio) + 1


def _ledger_event(kernel: AgentKernel, event_type: str) -> dict | None:
    ledger = kernel.last_ledger
    assert ledger is not None
    for event in ledger.events:
        if event.event_type == event_type:
            return event.data
    return None


async def test_谎报usage低于启发式下界_触发调整事件并按估算入账():
    """红队 H5 主断言：谎报 usage=1 逃逸预算被识破——调整事件留痕 + 估算值入账。"""
    # Arrange：谎报工具（usage=1，产出 400+ 字符）+ 单步计划 + 宽预算
    tool = FakeTool(usage={"total_tokens": 1}, output=dict(_BIG_OUTPUT))
    planner = FakePlanner(make_candidate((make_step(seq=1),)))
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))
    # Act：跑一轮
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_tokens=100_000, max_steps=9, duration_s=30))
    # Assert ①：调整不改变执行结果（completed），但账本留痕 kernel.usage_adjusted
    assert outcome.status == str(RunStatus.COMPLETED)
    data = _ledger_event(kernel, "kernel.usage_adjusted")
    assert data is not None, "低报未触发 kernel.usage_adjusted 事件"
    assert data["usage_estimated"] is True
    assert data["reported_tokens"] == 1
    expected = _expected_booked(_BIG_OUTPUT)
    assert data["booked_tokens"] == expected > 1  # 按估算入账（远高于谎报值）
    # Assert ②：入账口径=估算值——tracker 实际累计的是 booked 而非谎报的 1
    assert kernel.last_run_context is not None
    assert kernel.last_run_context.tracker.tokens_used == expected


async def test_估算入账咬预算_谎报值无法逃逸token维终止():
    """按估算入账的语义咬合 A4：预算上限卡在 booked 之下即耗尽——谎报 usage=1 不再绕过。"""
    # Arrange：两步计划 + token 预算=booked-1（谎报值 1 远达不到）
    tool = FakeTool(usage={"total_tokens": 1}, output=dict(_BIG_OUTPUT))
    booked = _expected_booked(_BIG_OUTPUT)
    planner = FakePlanner(make_candidate((make_step(seq=1), make_step(seq=2))))
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))
    # Act：预算上限=booked-1
    outcome = await kernel.run(
        make_task(), make_ctx(), budget=Budget(max_tokens=booked - 1, max_steps=99, duration_s=30)
    )
    # Assert：token 维预算耗尽优雅终止（若按谎报值入账则预算永不耗尽、两步全跑完）
    assert outcome.status == str(RunStatus.FAILED)
    assert outcome.reason_code == 5005  # RETRY_BUDGET_EXHAUSTED（预算登记码）
    assert len(tool.calls) == 1  # 第二步从未执行（预算咬合生效）


async def test_usage为零_维持拒收不变_零调整事件():
    # Arrange：0 值回传（既有拒收口径：不入账不检测）
    tool = FakeTool(usage={"total_tokens": 0}, output=dict(_BIG_OUTPUT))
    planner = FakePlanner(make_candidate((make_step(seq=1),)))
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))
    # Act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_tokens=100_000, max_steps=9, duration_s=30))
    # Assert：正常完成、零调整事件、零入账
    assert outcome.status == str(RunStatus.COMPLETED)
    assert _ledger_event(kernel, "kernel.usage_adjusted") is None
    assert kernel.last_run_context is not None
    assert kernel.last_run_context.tracker.tokens_used == 0


async def test_诚实回传_零行为变化_无调整事件():
    # Arrange：诚实大值回传（≥ 下界）——既有口径原值入账
    tool = FakeTool(usage={"total_tokens": 1_000}, output={"rows": 3})
    planner = FakePlanner(make_candidate((make_step(seq=1),)))
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))
    # Act
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_tokens=100_000, max_steps=9, duration_s=30))
    # Assert：completed + 无调整事件 + 按原值入账
    assert outcome.status == str(RunStatus.COMPLETED)
    assert _ledger_event(kernel, "kernel.usage_adjusted") is None
    assert kernel.last_run_context is not None
    assert kernel.last_run_context.tracker.tokens_used == 1_000

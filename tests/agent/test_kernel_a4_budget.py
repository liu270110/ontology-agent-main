# tests/agent/test_kernel_a4_budget.py
"""A4 预算与终止判定负向测试（02 §2 A4）：token/步数/时长三维；终止不认模型自述、不可卡死。"""

from __future__ import annotations

import time

import pytest

from services.agent.business.kernel.budget import Budget, BudgetTracker
from services.agent.business.kernel.errors import BudgetExhaustedError
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.step_state import StepStatus
from services.agent.domain.model.task import RunStatus
from tests.agent.conftest import (
    FakePlanner,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)


def test_预算构造_非正数被拒():
    with pytest.raises(ValueError):
        Budget(max_tokens=0)
    with pytest.raises(ValueError):
        Budget(max_steps=-1)
    with pytest.raises(ValueError):
        Budget(duration_s=0)


def test_预算检查点_任一维耗尽即抛结构化预算错误():
    tracker = BudgetTracker(Budget(max_tokens=10, max_steps=2), clock=lambda: 0.0)
    tracker.check()  # 未耗尽：不抛（对照）
    tracker.add_tokens(10)
    with pytest.raises(BudgetExhaustedError, match="token"):
        tracker.check()
    tracker2 = BudgetTracker(Budget(max_tokens=None, max_steps=1), clock=lambda: 0.0)
    tracker2.add_step()
    with pytest.raises(BudgetExhaustedError, match="step"):
        tracker2.check()


async def test_步数预算耗尽_优雅终止产终态_剩余步不执行():
    tool = FakeTool()
    planner = FakePlanner(make_candidate((make_step(seq=1), make_step(seq=2))))
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=1, duration_s=10))
    assert outcome.status == str(RunStatus.FAILED)
    assert outcome.reason_code == 5005  # RETRY_BUDGET_EXHAUSTED（登记段内唯一预算码）
    assert "步数" in outcome.reason or "预算" in outcome.reason
    assert len(tool.calls) == 1  # 第二步从未执行
    statuses = {s.seq: s.status for s in outcome.terminal_states}
    assert statuses[1] is StepStatus.VALIDATED  # 已开始步骤完成
    assert statuses[2] is StepStatus.CANCELLED  # 未开始步骤落取消终态
    assert all(s.is_terminal for s in outcome.terminal_states)  # 不可卡死：全部终态


async def test_token预算耗尽_工具记账后终止_模型自述完成不被采信():
    tool = FakeTool(usage={"total_tokens": 1_000})
    planner = FakePlanner(make_candidate((make_step(seq=1), make_step(seq=2))))
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_tokens=1_000, max_steps=99, duration_s=30))
    assert outcome.status == str(RunStatus.FAILED)
    assert outcome.reason_code == 5005
    # 工具回执自报「成功」不构成终止豁免——终止只认客观预算水位（A4：策略永不交给模型）
    ledger = kernel.last_ledger
    assert ledger is not None
    assert any("interrupted" in e.event_type for e in ledger.events)


async def test_时长预算硬兜底_卡死工具被asyncio_timeout收敛():
    tool = FakeTool(sleep_s=10.0)  # 可被取消的慢工具
    planner = FakePlanner(make_candidate((make_step(),)))
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))
    started = time.monotonic()
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_tokens=None, max_steps=None, duration_s=0.3))
    elapsed = time.monotonic() - started
    assert outcome.status == str(RunStatus.TIMEOUT)
    assert outcome.reason_code == 5005
    assert elapsed < 5.0  # 不可卡死：远小于工具自身 10s 时长
    assert all(s.is_terminal for s in outcome.terminal_states)


def test_水位快照累计单调():
    tracker = BudgetTracker(Budget(max_tokens=100), clock=lambda: 0.0)
    tracker.add_tokens(30)
    tracker.add_step()
    wm = tracker.watermark()
    assert (wm.tokens_used, wm.steps_done) == (30, 1)

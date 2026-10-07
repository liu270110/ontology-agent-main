# tests/agent/test_kernel_timeout_settings.py
"""内核超时参数化入 Settings（B-③ 批，docs/Agent/10 §8.2）。

覆盖面：
- 构造器显式参数覆盖被尊重（tool_timeout_s 显式注入优先，测试注入通道）；
- 未传参数时 Settings 回退生效（工具/规划/门禁/排水四维，monkeypatch 消费模块
  命名空间的 get_settings——模块级 from-import 持引用，须就地替换）；
- 四字段默认值与原常量逐位一致（30/10/1/5）+ gt=0 正数约束。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from annotated_types import Gt
from pydantic import ValidationError

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.ledger import KernelLedger
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_context import ExtensionMeta, TaskRef, TenantContext
from services.agent.domain.model.kernel_planning import PlanCandidate, PlanMode
from services.agent.domain.model.step_state import StepStatus
from services.agent.domain.model.task import RunStatus
from services.platform.config import Settings
from tests.agent.conftest import (
    FakePackGate,
    FakePlanner,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)


class SlowPlanner:
    """慢规划器桩（kernel_planning_timeout_s 注入验证用）：plan 前睡眠固定时长。"""

    def __init__(self, candidate: PlanCandidate, *, sleep_s: float) -> None:
        self.meta = ExtensionMeta(
            name="fixture.slow_planner",
            version="1.0.0",
            semantic_annotation={"rule_iri": "http://ontology.example/rule/慢规划"},
        )
        self.candidate = candidate
        self.sleep_s = sleep_s

    async def plan(
        self, task: TaskRef, ctx: TenantContext, *, mode: PlanMode = PlanMode.TEMPLATE, timeout_ms: int = 10_000
    ) -> PlanCandidate:
        await asyncio.sleep(self.sleep_s)
        return self.candidate


def _patch_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Settings:
    """打桩配置层：loop 与 execution 两处消费命名空间同源替换（同一 Settings 实例）。"""
    fake = Settings(**overrides)
    monkeypatch.setattr("services.agent.business.kernel.loop.get_settings", lambda: fake)
    monkeypatch.setattr("services.agent.business.kernel.execution.get_settings", lambda: fake)
    return fake


async def test_构造器显式超时参数优先于Settings_工具超时结构化失败5003(monkeypatch: pytest.MonkeyPatch) -> None:
    # 准备：配置层故意给宽超时 5.0（若显式参数未覆盖，0.3s 工具会跑完并 COMPLETED）
    _patch_settings(monkeypatch, kernel_tool_timeout_s=5.0)
    tool = FakeTool(sleep_s=0.3)
    planner = FakePlanner(make_candidate((make_step(),)))
    kernel = AgentKernel(
        make_tool_dispatcher(tool, register_planning_strategy=(planner,)),
        tool_timeout_s=0.2,
    )

    # 执行：0.3s 工具 vs 0.2s 显式超时
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(duration_s=10))

    # 断言：步失败且调用按单调用超时结构化闭合（MCP_TARGET_UNAVAILABLE=5003）
    assert outcome.status == str(RunStatus.FAILED)
    ledger = kernel.last_ledger
    assert ledger is not None
    assert ledger.tool_calls[0].error_code == 5003


async def test_未传参数时Settings回退_工具超时生效(monkeypatch: pytest.MonkeyPatch) -> None:
    # 准备：不传 tool_timeout_s，配置层注入 0.2；工具 0.3s 未完成
    _patch_settings(monkeypatch, kernel_tool_timeout_s=0.2)
    tool = FakeTool(sleep_s=0.3)
    planner = FakePlanner(make_candidate((make_step(),)))
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))

    # 执行
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(duration_s=10))

    # 断言：Settings 的 0.2s 注入到执行断点 → 结构化超时 5003
    assert outcome.status == str(RunStatus.FAILED)
    ledger = kernel.last_ledger
    assert ledger is not None
    assert ledger.tool_calls[0].error_code == 5003


async def test_未传参数时Settings回退_门禁超时按拒绝合成(monkeypatch: pytest.MonkeyPatch) -> None:
    # 准备：门禁超时注入 0.05s；包 gate 睡 0.5s（默认 1s 窗口下本会放行）
    _patch_settings(monkeypatch, kernel_gate_timeout_s=0.05)
    tool = FakeTool()
    gate = FakePackGate(sleep_s=0.5)
    planner = FakePlanner(make_candidate((make_step(),)))
    kernel = AgentKernel(make_tool_dispatcher(tool, register_pre_gate=(gate,), register_planning_strategy=(planner,)))

    # 执行
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(duration_s=10))

    # 断言：包 gate 超时按拒绝合成（降级矩阵），工具从未执行
    assert outcome.status == str(RunStatus.FAILED)
    failed = [s for s in outcome.terminal_states if s.status is StepStatus.FAILED]
    assert failed and failed[0].error is not None
    assert "包 gate 超时" in failed[0].error
    assert tool.calls == []


async def test_未传参数时Settings回退_规划超时硬兜底收敛(monkeypatch: pytest.MonkeyPatch) -> None:
    # 准备：规划超时注入 0.05s；慢规划器睡 1.0s（默认 10s 窗口下本会正常产出计划）
    _patch_settings(monkeypatch, kernel_planning_timeout_s=0.05)
    planner = SlowPlanner(make_candidate((make_step(),)), sleep_s=1.0)
    kernel = AgentKernel(make_tool_dispatcher(FakeTool(), register_planning_strategy=(planner,)))

    # 执行：wait_for 超时沿 run 的 TimeoutError 收敛通道落 TIMEOUT 终态
    started = time.monotonic()
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(duration_s=10))
    elapsed = time.monotonic() - started

    # 断言：0.05s 即收敛（未吃默认 10s 规划窗口，也未等慢规划器 1.0s）
    assert outcome.status == str(RunStatus.TIMEOUT)
    assert elapsed < 0.5


async def test_未传参数时Settings回退_排水显式传注入值(monkeypatch: pytest.MonkeyPatch) -> None:
    # 准备：事件汇/排水超时注入 2.5s；包装 drain_sink 捕获 loop 调用点实参（不改变排水行为）
    _patch_settings(monkeypatch, kernel_sink_timeout_s=2.5)
    seen: list[float] = []
    original = KernelLedger.drain_sink

    async def spy(self: KernelLedger, *, timeout_s: float = 5.0) -> None:
        seen.append(timeout_s)
        await original(self, timeout_s=timeout_s)

    monkeypatch.setattr(KernelLedger, "drain_sink", spy)
    planner = FakePlanner(make_candidate((make_step(),)))
    kernel = AgentKernel(make_tool_dispatcher(FakeTool(), register_planning_strategy=(planner,)))

    # 执行：单步成功运行（settlement 段两次排水，无取消/中断路径）
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(duration_s=10))

    # 断言：每处 drain_sink 调用都显式携带注入值（不再吃签名默认 5.0）
    assert outcome.status == str(RunStatus.COMPLETED)
    assert seen == [2.5, 2.5]


def test_四字段默认值30_10_1_5_正数约束与非正数拒绝() -> None:
    # 准备/执行：模型字段缺省 + 无环境覆盖实例（数值默认归配置层，D2/F-4 同款断言口径）
    fields = Settings.model_fields
    settings = Settings()

    # 断言：默认值与原模块级常量逐位一致（30/10/1/5），gt=0 约束在位
    assert fields["kernel_tool_timeout_s"].default == 30.0
    assert fields["kernel_planning_timeout_s"].default == 10.0
    assert fields["kernel_gate_timeout_s"].default == 1.0
    assert fields["kernel_sink_timeout_s"].default == 5.0
    assert (
        settings.kernel_tool_timeout_s,
        settings.kernel_planning_timeout_s,
        settings.kernel_gate_timeout_s,
        settings.kernel_sink_timeout_s,
    ) == (30.0, 10.0, 1.0, 5.0)
    for name in (
        "kernel_tool_timeout_s",
        "kernel_planning_timeout_s",
        "kernel_gate_timeout_s",
        "kernel_sink_timeout_s",
    ):
        assert any(isinstance(m, Gt) and m.gt == 0 for m in fields[name].metadata)

    # 断言：非正数构造被拒（Field gt=0）
    with pytest.raises(ValidationError):
        Settings(kernel_tool_timeout_s=0)
    with pytest.raises(ValidationError):
        Settings(kernel_planning_timeout_s=-1)

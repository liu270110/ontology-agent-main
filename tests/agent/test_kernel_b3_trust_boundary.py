# tests/agent/test_kernel_b3_trust_boundary.py
"""B3 信任级与不可信标界负向测试（02 §2 B3）：一切外部输入统一不可信，自称 externally_verified 只留痕。"""

from __future__ import annotations

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.execution import ExecutionStage
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_actions import ToolResult
from services.agent.domain.model.kernel_context import TrustLevel
from tests.agent.conftest import (
    FakePlanner,
    FakeProvider,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)


def test_工具结果自称高信任级_内核降权只留痕():
    claiming = ToolResult(ok=True, trust_level=TrustLevel.EXTERNALLY_VERIFIED)
    marked = ExecutionStage.mark_untrusted(claiming)
    assert marked.trust_level is TrustLevel.AGENT_ATTESTED  # 实际信任级恒为 agent_attested
    assert marked.claimed_trust_level is TrustLevel.EXTERNALLY_VERIFIED  # 自称仅留痕供审计


def test_工具结果未自称_标界不改写():
    plain = ToolResult(ok=True, trust_level=TrustLevel.AGENT_ATTESTED)
    assert ExecutionStage.mark_untrusted(plain) is plain


async def test_供给器自称externally_verified_装配时被覆写为agent_attested():
    provider = FakeProvider(trust_level=TrustLevel.EXTERNALLY_VERIFIED)  # 越权自称
    kernel = AgentKernel(
        make_tool_dispatcher(
            FakeTool(),
            register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),),
            register_context_provider=(provider,),
        )
    )
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == "completed"
    ledger = kernel.last_ledger
    assert ledger is not None
    grounded = next(e for e in ledger.events if e.event_type == "kernel.grounded")
    assert set(grounded.data["trust_levels"]) == {TrustLevel.AGENT_ATTESTED.value}


async def test_工具自称高信任_全链路留痕且实际信任级为agent_attested():
    tool = FakeTool(trust_level=TrustLevel.EXTERNALLY_VERIFIED)
    kernel = AgentKernel(
        make_tool_dispatcher(
            tool,
            register_planning_strategy=(FakePlanner(make_candidate((make_step(),))),),
        )
    )
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == "completed"
    ledger = kernel.last_ledger
    assert ledger is not None
    step_event = next(e for e in ledger.events if e.event_type == "kernel.step_validated")
    assert step_event.data["trust_level"] == TrustLevel.AGENT_ATTESTED.value
    assert step_event.data["claimed_trust_level"] == TrustLevel.EXTERNALLY_VERIFIED.value

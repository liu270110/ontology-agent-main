# tests/agent/test_kernel_b2_criteria.py
"""B2 判据求值器负向测试（02 §2 B2）：只认外部回执，agent_attested/agent 自述不参与求值。"""

from __future__ import annotations

from conftest import (
    FakePlanner,
    FakeTool,
    make_candidate,
    make_ctx,
    make_step,
    make_task,
    make_tool_dispatcher,
)

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.criteria import CriterionEvaluator
from services.agent.business.kernel.ledger import KernelLedger
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_planning import SuccessCriterion
from services.agent.domain.model.task import RunStatus

_CRITERION = SuccessCriterion(
    criterion_id="c1",
    focus_iri="http://ontology.example/task/停电分析",
    required_receipt_kind="delivery_confirmation",
)


async def test_判据只认外部回执_agent自述完成不满足判据():
    # 工具回执 ok=True + 产出自述 status=done，但无外部回执 → 判据暂不可求值，运行不可自宣完成
    tool = FakeTool(output={"status": "done", "summary": "我认为任务已完成"})
    planner = FakePlanner(make_candidate((make_step(),), criteria=(_CRITERION,)))
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == str(RunStatus.WAITING_TOOL)  # 不是 completed：agent 自述不采信
    assert outcome.criteria[0].satisfied is False
    assert outcome.criteria[0].blocked_by_trust is True


def test_账本无回执_判据blocked_回执落账即转可求值():
    ledger = KernelLedger(tenant_id=make_ctx().tenant_id, trace_id="trace-b2")
    evaluator = CriterionEvaluator()
    # 空：blocked
    (report,) = evaluator.evaluate((_CRITERION,), ledger)
    assert report.satisfied is False and report.blocked_by_trust is True
    # 外部回执落账（写回路径，工具/agent 无此入口）→ 转可求值
    ledger.record_external_receipt(
        kind="delivery_confirmation",
        focus_iri=_CRITERION.focus_iri,
        payload={"receipt": "PG 台账行指针", "trace_id": ledger.trace_id},
    )
    (report2,) = evaluator.evaluate((_CRITERION,), ledger)
    assert report2.satisfied is True and report2.blocked_by_trust is False


def test_回执类别或focus不匹配_判据仍blocked():
    ledger = KernelLedger(tenant_id=make_ctx().tenant_id, trace_id="trace-b2b")
    ledger.record_external_receipt(kind="别的回执", focus_iri=_CRITERION.focus_iri, payload={})
    (report,) = CriterionEvaluator().evaluate((_CRITERION,), ledger)
    assert report.satisfied is False


async def test_工具自称externally_verified不构成判据凭证():
    # B3 标界与 B2 联动：工具自报高信任级只留痕，账本零回执 → 判据照样 blocked
    from services.agent.domain.model.kernel_context import TrustLevel

    tool = FakeTool(trust_level=TrustLevel.EXTERNALLY_VERIFIED)
    planner = FakePlanner(make_candidate((make_step(),), criteria=(_CRITERION,)))
    kernel = AgentKernel(make_tool_dispatcher(tool, register_planning_strategy=(planner,)))
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == str(RunStatus.WAITING_TOOL)
    ledger = kernel.last_ledger
    assert ledger is not None
    assert ledger.receipts() == ()  # 工具无回执登记口：凭证源只剩账本（C1 v1）

# tests/agent/test_kernel_b2_criteria.py
"""B2 判据求值器负向测试（02 §2 B2）：只认外部回执，agent_attested/agent 自述不参与求值。

E-4 K1-c 增补（docs/Agent/13 §2）：回执优先（M3 口径不变）；回执缺失且判据声明投影
求值面且端口已注册 → focus_iri 作 focus node 走 CriterionProjection 端口确定性求值
（补充分支）。覆盖：投影命中/未命中/端口隔离（缺失·不可求值·异常·重复注册拒绝）。
"""

from __future__ import annotations

import pytest

from services.agent.business.kernel.budget import Budget
from services.agent.business.kernel.criteria import CriterionEvaluator
from services.agent.business.kernel.dispatcher import ExtensionDispatcher
from services.agent.business.kernel.errors import KernelContractError
from services.agent.business.kernel.ledger import KernelLedger
from services.agent.business.kernel.loop import AgentKernel
from services.agent.domain.model.kernel_context import ExtensionMeta
from services.agent.domain.model.kernel_planning import (
    CriterionProjectionSpec,
    ProjectionReport,
    SuccessCriterion,
)
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

_CRITERION = SuccessCriterion(
    criterion_id="c1",
    focus_iri="http://ontology.example/task/停电分析",
    required_receipt_kind="delivery_confirmation",
)

_SHAPES_IRI = "http://ontology.example/shape/任务完成"


def _projection_criterion() -> SuccessCriterion:
    """带投影求值面的判据（回执类别保持不变：命中仍优先）。"""
    return _CRITERION.model_copy(update={"projection": CriterionProjectionSpec(shapes_iri=_SHAPES_IRI)})


class _FakeProjection:
    """CriterionProjection 端口桩：可配置结论/异常，记录调用（回执优先与端口隔离验收面）。"""

    def __init__(self, *, report: ProjectionReport | None = None, raise_exc: Exception | None = None) -> None:
        self.meta = ExtensionMeta(
            name="fixture.projection",
            version="1.0.0",
            semantic_annotation={"rule_iri": "http://ontology.example/rule/判据投影"},
        )
        self.report = report
        self.raise_exc = raise_exc
        self.calls: list[SuccessCriterion] = []

    async def evaluate(self, criterion: SuccessCriterion, ctx, *, timeout_ms: int = 1_000) -> ProjectionReport:
        self.calls.append(criterion)
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.report is not None
        return self.report


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


# ── E-4 K1-c：投影补充分支（回执缺失时 focus_iri 单点求值；回执命中仍优先）────────


async def test_投影分支命中_回执缺失时端口求值满足_回执到达仍优先():
    ledger = KernelLedger(tenant_id=make_ctx().tenant_id, trace_id="trace-b2-p1")
    ctx = make_ctx()
    port = _FakeProjection(report=ProjectionReport(evaluated=True, conforms=True, detail="2 违例之外的合焦求值"))
    evaluator = CriterionEvaluator(projection=port)
    criterion = _projection_criterion()

    # 回执缺失 → 走端口：确定性投影满足 ⇒ satisfied（非 blocked）
    (report,) = await evaluator.evaluate_with_projection((criterion,), ledger, ctx)
    assert report.satisfied is True and report.blocked_by_trust is False
    assert len(port.calls) == 1 and port.calls[0].focus_iri == criterion.focus_iri
    assert _SHAPES_IRI in report.detail

    # 回执到达 → 仍优先走回执口径（M3），端口不再被调用
    ledger.record_external_receipt(kind=criterion.required_receipt_kind, focus_iri=criterion.focus_iri, payload={})
    (report2,) = await evaluator.evaluate_with_projection((criterion,), ledger, ctx)
    assert report2.satisfied is True and report2.blocked_by_trust is False
    assert "回执" in report2.detail
    assert len(port.calls) == 1  # 端口调用数未增（回执优先）


async def test_投影分支未命中_确定性负结论_运行不可判完成():
    # 内核级：端口 conforms=False ⇒ satisfied=False 且非 blocked ⇒ 运行 FAILED（不可判完成，A4）
    port = _FakeProjection(report=ProjectionReport(evaluated=True, conforms=False, detail="focus 违例 1 条"))
    tool = FakeTool()
    planner = FakePlanner(make_candidate((make_step(),), criteria=(_projection_criterion(),)))
    kernel = AgentKernel(
        make_tool_dispatcher(tool, register_planning_strategy=(planner,), register_criterion_projection=(port,))
    )
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))

    assert outcome.status == str(RunStatus.FAILED)  # 非 completed 亦非 WAITING_TOOL
    assert "判据未满足" in (outcome.reason or "")
    assert len(outcome.criteria) == 1
    assert outcome.criteria[0].satisfied is False
    assert outcome.criteria[0].blocked_by_trust is False  # 可求值且未满足（确定性负结论）
    assert len(port.calls) == 1  # 求值恰好发生在沉淀阶段


async def test_端口隔离_未注册_不可求值_异常_一律退回blocked等待回执():
    ctx = make_ctx()
    criterion = _projection_criterion()

    # (a) 端口未注册（None）→ 纯回执口径（M3 不变），判据 blocked
    ledger_a = KernelLedger(tenant_id=ctx.tenant_id, trace_id="t-p3a")
    (report,) = await CriterionEvaluator(projection=None).evaluate_with_projection((criterion,), ledger_a, ctx)
    assert report.satisfied is False and report.blocked_by_trust is True

    # (b) 端口明示不可求值（形状/图缺失，evaluated=False）→ 退回 blocked（求值不可得≠求值失败）
    port_unavailable = _FakeProjection(report=ProjectionReport(evaluated=False, detail="形状集未注册"))
    evaluator_b = CriterionEvaluator(projection=port_unavailable)
    ledger_b = KernelLedger(tenant_id=ctx.tenant_id, trace_id="t-p3b")
    (report_b,) = await evaluator_b.evaluate_with_projection((criterion,), ledger_b, ctx)
    assert report_b.satisfied is False and report_b.blocked_by_trust is True

    # (c) 端口异常逃逸 → 结构化退回 blocked（禁裸异常炸运行）
    port_broken = _FakeProjection(raise_exc=RuntimeError("投影通道不可用"))
    evaluator_c = CriterionEvaluator(projection=port_broken)
    ledger_c = KernelLedger(tenant_id=ctx.tenant_id, trace_id="t-p3c")
    (report_c,) = await evaluator_c.evaluate_with_projection((criterion,), ledger_c, ctx)
    assert report_c.satisfied is False and report_c.blocked_by_trust is True

    # (d) 内核级：不可求值 → 运行 WAITING_TOOL（等回执），端口未产任何账本回执
    tool = FakeTool()
    planner = FakePlanner(make_candidate((make_step(),), criteria=(_projection_criterion(),)))
    kernel = AgentKernel(
        make_tool_dispatcher(
            tool, register_planning_strategy=(planner,), register_criterion_projection=(port_unavailable,)
        )
    )
    outcome = await kernel.run(make_task(), make_ctx(), budget=Budget(max_steps=5, duration_s=10))
    assert outcome.status == str(RunStatus.WAITING_TOOL)
    assert outcome.criteria[0].blocked_by_trust is True
    assert kernel.last_ledger.receipts() == ()  # 端口无回执登记口（B3：只读求值）


def test_端口注册唯一_重复注册拒绝_契约面():
    dispatcher = ExtensionDispatcher()
    dispatcher.register_criterion_projection(_FakeProjection(report=ProjectionReport(evaluated=True, conforms=True)))
    with pytest.raises(KernelContractError):
        dispatcher.register_criterion_projection(
            _FakeProjection(report=ProjectionReport(evaluated=True, conforms=False))
        )
    assert dispatcher.criterion_projection is not None  # 首注册保留（取用面可达）

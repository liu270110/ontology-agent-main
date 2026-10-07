# tests/rsi/test_rsi_skeleton.py
"""RSI 阶段 A 骨架用例（architecture/09 阶段 A 收缩；13 篇 M5-2「apply 恒拒红线」）。

断言目标：
- 双轨触发注册面：经验轨事件触发 / 指标轨周期注册与到期；kill switch 关闭停止起草并审计；
- 五类白名单：类型外拒绝、目标禁区拒绝（内核/公理/权限/评估器…）、均落安全审计；
- 三级门禁链：0 级硬门槛真跑（信封缺键/缺轨迹 → rejected 留档）、通过 → evaluated
  （①②③为 not_configured 演练位）、状态机非法迁移拒绝；
- apply 恒拒红线：任何状态/操作者均拒 + 审计留痕 + 候选状态不变；
- 全动作审计：受理/评估/拒绝/apply 拒绝逐动作留痕（09 §6 红线 7）。
"""

from __future__ import annotations

import uuid

import pytest

from services.rsi.audit import (
    ACTION_APPLY_DENIED,
    ACTION_TRIGGER_SUPPRESSED,
    ACTION_WHITELIST_VIOLATION,
    InMemoryAuditTrail,
    RsiAuditRecord,
)
from services.rsi.proposal import Proposal, ProposalError, ProposalStatus, TriggerTrack
from services.rsi.service import RsiApplyForbiddenError, RsiService
from services.rsi.triggers import TriggerEvent
from services.rsi.whitelist import ImprovementType, WhitelistViolation, validate_improvement

TENANT = uuid.uuid4()
TRACE = "trace-rsi-test"

# 合法统一信封（09 §2）
VALID_ENVELOPE = {
    "patch": {"system_prefix_addition": "输出必须引用证据编号"},
    "expected_gain": "同型失败率 -10%",
    "risk_level": "low",
    "eval_plan": {"golden": "extract_power_v1", "threshold": -0.02},
}


def _service() -> tuple[RsiService, InMemoryAuditTrail]:
    trail = InMemoryAuditTrail()
    return RsiService(audit_trail=trail), trail


async def _submit_valid(service: RsiService, **kw: object) -> Proposal:
    params: dict = {
        "tenant_id": TENANT,
        "type_str": ImprovementType.PROMPT_TEMPLATE.value,
        "target": "prompt_templates/extract_power@v3",
        "envelope": dict(VALID_ENVELOPE),
        "trigger": TriggerTrack.EXPERIENCE,
        "source_trace_ids": (TRACE,),
    }
    params.update(kw)
    return await service.submit(**params)  # type: ignore[arg-type]


# ── 双轨触发注册面 ────────────────────────────────────────────────────────


async def test_双轨触发注册与分派_产出候选入池() -> None:
    service, trail = _service()

    async def experience_handler(event: TriggerEvent) -> Proposal | None:
        if event.payload.get("status") == "failed":
            return Proposal(
                tenant_id=TENANT,
                type=ImprovementType.PLAN_TEMPLATE,
                target="plan_templates/outage_analysis@v1",
                trigger=event.track,
                envelope=dict(VALID_ENVELOPE),
                source_trace_ids=event.trace_ids,
            )
        return None

    async def metric_handler(event: TriggerEvent) -> Proposal | None:
        return Proposal(
            tenant_id=TENANT,
            type=ImprovementType.RETRIEVAL_PARAMS,
            target="retrieval/rrf_k@v1",
            trigger=event.track,
            envelope=dict(VALID_ENVELOPE),
            source_trace_ids=event.trace_ids,
        )

    service.register_experience_trigger("task.review", experience_handler)
    service.register_metric_trigger("golden.qa", metric_handler, interval_s=3600.0)
    assert ("experience", "task.review") in service.triggers.registered()
    assert ("metric", "golden.qa") in service.triggers.registered()

    # 经验轨：失败终态事件 → 候选入池；成功事件 → 无候选
    failed = await service.fire_trigger(
        TriggerEvent(
            track=TriggerTrack.EXPERIENCE, kind="task.finished", payload={"status": "failed"}, trace_ids=(TRACE,)
        )
    )
    assert len(failed) == 1 and failed[0].id in service.pool
    empty = await service.fire_trigger(
        TriggerEvent(
            track=TriggerTrack.EXPERIENCE, kind="task.finished", payload={"status": "succeeded"}, trace_ids=(TRACE,)
        )
    )
    assert empty == []

    # 指标轨：周期到期名输出（首查即到期）
    assert service.triggers.due_metric_triggers() == ["golden.qa"]
    metric = await service.fire_trigger(
        TriggerEvent(
            track=TriggerTrack.METRIC,
            kind="metric.degraded",
            payload={"baseline": 0.9, "current": 0.85},
            trace_ids=(TRACE,),
        )
    )
    assert len(metric) == 1
    assert any(r.action == "rsi.proposal.accepted" for r in trail.entries)


async def test_kill_switch关闭_停止起草并审计() -> None:
    service, trail = _service()
    service.triggers.enabled = False  # 红线 6：平台级 kill switch

    async def handler(event: TriggerEvent) -> Proposal | None:  # pragma: no cover — 不应被调用
        raise AssertionError("kill switch 关闭后 handler 不得执行")

    service.register_experience_trigger("task.review", handler)
    produced = await service.fire_trigger(
        TriggerEvent(track=TriggerTrack.EXPERIENCE, kind="task.finished", trace_ids=(TRACE,))
    )
    assert produced == []
    assert any(r.action == ACTION_TRIGGER_SUPPRESSED and r.outcome == "suppressed" for r in trail.entries)


async def test_触发器注册约束_经验轨禁周期_指标轨必须周期() -> None:
    service, _trail = _service()

    async def handler(event: TriggerEvent) -> Proposal | None:  # pragma: no cover
        return None

    with pytest.raises(ValueError):
        service.register_metric_trigger("bad.metric", handler, interval_s=0)
    with pytest.raises(ValueError):
        service.triggers.register(TriggerTrack.EXPERIENCE, "bad.exp", handler, interval_s=60.0)


# ── 五类白名单 ────────────────────────────────────────────────────────────


async def test_白名单外类型拒绝并落安全审计() -> None:
    service, trail = _service()
    with pytest.raises(WhitelistViolation):
        await _submit_valid(service, type_str="kernel_code")
    with pytest.raises(WhitelistViolation):
        await _submit_valid(service, type_str="rule")  # 规则类已移出白名单（走本体 ChangeRequest）
    violations = [r for r in trail.entries if r.action == ACTION_WHITELIST_VIOLATION]
    assert len(violations) == 2
    assert all(v.outcome == "rejected" for v in violations)
    assert service.pool == {}  # 白名单外不入池


async def test_目标禁区拒绝_内核与公理与评估器() -> None:
    for forbidden in (
        "services.agent.business.kernel.loop@v1",  # 内核代码
        "ontology:axiom/power@v1",  # 本体公理
        "shacl_baseline/main@v2",  # SHACL 基线（门禁基线不可降）
        "permission matrix@v1",  # 权限
        "eval_set/golden_qa@v3",  # 评测集（评估器隔离）
        "approval router@v1",  # 审批路由
    ):
        with pytest.raises(WhitelistViolation):
            validate_improvement(ImprovementType.PROMPT_TEMPLATE.value, forbidden)


def test_五类白名单枚举齐全() -> None:
    assert {t.value for t in ImprovementType} == {
        "prompt_template",
        "plan_template",
        "memory_policy",
        "retrieval_params",
        "tool_description",
    }


# ── 三级门禁链 ────────────────────────────────────────────────────────────


async def test_门禁链_信封缺键0级拒绝_归因留档() -> None:
    service, trail = _service()
    proposal = await _submit_valid(service, envelope={"patch": {}})  # 缺 expected_gain/risk_level/eval_plan
    evaluated = await service.evaluate(proposal.id)
    assert evaluated.status is ProposalStatus.REJECTED
    assert evaluated.eval_report is not None
    assert evaluated.eval_report["gates"][0]["passed"] is False
    assert "统一信封缺键" in evaluated.eval_report["gates"][0]["verdict"]
    rejected = [r for r in trail.entries if r.action == "rsi.evaluate" and r.outcome == "rejected"]
    assert len(rejected) == 1


async def test_门禁链_通过后evaluated_演练位不_configured() -> None:
    service, _trail = _service()
    proposal = await _submit_valid(service)
    evaluated = await service.evaluate(proposal.id)
    assert evaluated.status is ProposalStatus.EVALUATED
    gates = {g["gate"]: g for g in (evaluated.eval_report or {})["gates"]}
    assert gates["level0_whitelist_schema"]["verdict"] == "pass"  # 0 级真跑
    for skeleton in ("level1_sandbox_replay", "level2_golden_regression", "level3_gray_compare"):
        assert gates[skeleton]["verdict"] == "not_configured:M5+"  # ①②③演练位
    assert (evaluated.eval_report or {})["baseline_delta"] is None  # 基线对比随 M5+


async def test_状态机_非法迁移拒绝_终态不可逆() -> None:
    proposal = Proposal(
        tenant_id=TENANT,
        type=ImprovementType.MEMORY_POLICY,
        target="memory/decay_half_life@v1",
        trigger=TriggerTrack.METRIC,
        envelope=dict(VALID_ENVELOPE),
    )
    with pytest.raises(ProposalError):
        proposal.transition(ProposalStatus.APPROVED)  # draft → approved 非法
    proposal.transition(ProposalStatus.EVALUATED)
    proposal.transition(ProposalStatus.REJECTED)
    with pytest.raises(ProposalError):
        proposal.transition(ProposalStatus.EVALUATED)  # 终态不可逆


# ── apply 恒拒红线 ────────────────────────────────────────────────────────


async def test_apply恒拒_任何状态_任何操作者() -> None:
    service, trail = _service()
    proposal = await _submit_valid(service)

    with pytest.raises(RsiApplyForbiddenError):  # draft 态即拒
        await service.apply(proposal.id, operator="agent://rsi")
    evaluated = await service.evaluate(proposal.id)
    assert evaluated.status is ProposalStatus.EVALUATED
    with pytest.raises(RsiApplyForbiddenError):  # evaluated 态仍拒（本批唯一可达的非终态）
        await service.apply(proposal.id, operator="human://admin")

    denials = [r for r in trail.entries if r.action == ACTION_APPLY_DENIED]
    assert len(denials) == 2
    assert all(r.outcome == "denied" for r in denials)
    assert all("M5+" in r.detail["reason"] for r in denials)
    assert service.pool[proposal.id].status is ProposalStatus.EVALUATED  # 候选状态不变
    assert service.apply_enabled is False  # 红线位无翻转入口


async def test_apply_未找到候选_聚合错误() -> None:
    service, _trail = _service()
    with pytest.raises(ProposalError):
        await service.apply(uuid.uuid4())


# ── 全动作审计 ────────────────────────────────────────────────────────────


async def test_全动作审计_受理评估拒绝逐动作留痕() -> None:
    service, trail = _service()
    proposal = await _submit_valid(service)
    await service.evaluate(proposal.id)
    actions = [r.action for r in trail.entries]
    assert "rsi.proposal.accepted" in actions and "rsi.evaluate" in actions
    assert all(r.created_at is not None for r in trail.entries)
    # LoggingAuditTrail 兜底汇可实例化且满足协议（M5+ PG 汇替换位）
    from services.rsi.audit import AuditTrail, LoggingAuditTrail

    logging_trail: AuditTrail = LoggingAuditTrail()
    await logging_trail.record(RsiAuditRecord(action="rsi.smoke", outcome="ok"))

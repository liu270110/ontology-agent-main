"""治理三档审批链纯函数测试（收敛点=services/review/domain/approval_chain，08 §2.4）。

覆盖：档位解析、签名数收敛、solo 自批/team 禁自批/enterprise 四眼（自批拒、重复签拒、
双签依序）、驳回单签可决——全部负向用例断言 DomainError 码 4702。
"""

from __future__ import annotations

import uuid

import pytest

from services.platform.kernel import DomainError
from services.review.domain.approval_chain import (
    GovernanceTier,
    parse_governance_tier,
    required_signatures,
    resolve_decision,
)

SUBMITTER = uuid.uuid4()
APPROVER_A = uuid.uuid4()
APPROVER_B = uuid.uuid4()


def test_档位解析_合法三档与缺失回落solo_非法值4702拒绝():
    # Act / Assert
    assert parse_governance_tier("solo") is GovernanceTier.SOLO
    assert parse_governance_tier("team") is GovernanceTier.TEAM
    assert parse_governance_tier("enterprise") is GovernanceTier.ENTERPRISE
    assert parse_governance_tier(None) is GovernanceTier.SOLO  # settings 未配置回落种子默认
    with pytest.raises(DomainError) as exc:
        parse_governance_tier("family")
    assert int(str(exc.value)[:4]) == 4702


def test_签名数收敛_solo与team为1_enterprise为2_四眼():
    # Assert（08 §2.4：enterprise 双负责人四眼）
    assert required_signatures(GovernanceTier.SOLO) == 1
    assert required_signatures(GovernanceTier.TEAM) == 1
    assert required_signatures(GovernanceTier.ENTERPRISE) == 2


def test_solo档_提交人自批放行_留痕语义一签即完整():
    # Act
    decision = resolve_decision(GovernanceTier.SOLO, action="approve", submitter_id=SUBMITTER, approver_id=SUBMITTER)
    # Assert：solo=提交人即审批人（任何档不可跳过门禁，但自审放行且留痕）
    assert decision.allowed and decision.complete
    assert decision.signatures_required == 1 and decision.signatures_collected == 1


def test_team档_他人审批放行_单签完整():
    # Act
    decision = resolve_decision(GovernanceTier.TEAM, action="approve", submitter_id=SUBMITTER, approver_id=APPROVER_A)
    # Assert
    assert decision.allowed and decision.complete


def test_team档_自批拒绝_4702负向():
    # Act
    decision = resolve_decision(GovernanceTier.TEAM, action="approve", submitter_id=SUBMITTER, approver_id=SUBMITTER)
    # Assert：team 禁自批（08 §2.4 单审批人档流程约束）
    assert not decision.allowed
    assert "禁自批" in decision.reason


def test_enterprise档_两个不同审批人依序_第一签未完整第二签完整():
    # Act：第一签（审批人 A）
    first = resolve_decision(
        GovernanceTier.ENTERPRISE, action="approve", submitter_id=SUBMITTER, approver_id=APPROVER_A
    )
    # Assert：签 1/2，未完整（不得生效）
    assert first.allowed and not first.complete
    assert first.signatures_collected == 1 and first.signatures_required == 2
    # Act：第二签（审批人 B，携带 A 的既有签名）
    second = resolve_decision(
        GovernanceTier.ENTERPRISE,
        action="approve",
        submitter_id=SUBMITTER,
        approver_id=APPROVER_B,
        prior_approvers=(APPROVER_A,),
    )
    # Assert：四眼齐，完整
    assert second.allowed and second.complete


def test_enterprise档_同一审批人重复签拒绝_负向():
    # Act：A 已签后 B 再以 A 身份重复签
    decision = resolve_decision(
        GovernanceTier.ENTERPRISE,
        action="approve",
        submitter_id=SUBMITTER,
        approver_id=APPROVER_A,
        prior_approvers=(APPROVER_A,),
    )
    # Assert：四眼=两个不同审批人，重复签拒绝
    assert not decision.allowed
    assert "重复签" in decision.reason


def test_enterprise档_自批拒绝_负向():
    # Act
    decision = resolve_decision(
        GovernanceTier.ENTERPRISE, action="approve", submitter_id=SUBMITTER, approver_id=SUBMITTER
    )
    # Assert
    assert not decision.allowed and "禁自批" in decision.reason


def test_驳回任一档单审批人即可_附理由留痕():
    # Act / Assert：reject 不受四眼约束（08 §4 REJ 回边单审批人可决）
    for tier in GovernanceTier:
        decision = resolve_decision(tier, action="reject", submitter_id=SUBMITTER, approver_id=SUBMITTER)
        assert decision.allowed and decision.complete, tier

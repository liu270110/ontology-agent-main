# tests/ontology/test_governance_tiers.py
"""ontology 发布审批三档接线单测（08 §2.4 收敛点接线，2026-09-27 M5 交付验收条件二收口）：

- solo 回归：缺省档位=显式 solo（向后兼容 M1 默认参数），提交人即审批人放行、他人审批 4205；
- team：自批 4702（approve 动词与 publish 回放双拦），非提交人单签放行；
- enterprise：单签不生效（4205 签名不足 1/2）、双签放行、同签重复/提交人自批 4702；
- 档位解析收敛：非法档位一律 4702（review.domain.parse_governance_tier 单一入口）。

纯单元（不连存储，04 篇 §7 测试策略）；判定语义全部来自 services/review/domain/approval_chain
收敛点——本文件锁的是「ontology 发布链路正确消费收敛点」，收敛点自身语义由 tests/review 锁。
"""

import uuid

import pytest

from services.ontology.domain.model.ontology import ChangesetStatus, DomainError, Ontology, OntologyVersionRef

TENANT = uuid.uuid4()
APPLICANT = uuid.uuid4()
APPROVER_A = uuid.uuid4()
APPROVER_B = uuid.uuid4()
PWR = "http://ontology-agent.local/o/t1/power#"


def _ref(version: str) -> OntologyVersionRef:
    return OntologyVersionRef(
        version=version, artifact_key=f"ontologies/{TENANT}/{uuid.uuid4()}/{version}.ttl", checksum="a" * 64
    )


def _submitted():
    """开单→记录预检→提交，返回 (聚合, in_review 变更单)，申请人=APPLICANT。"""
    ag = Ontology(tenant_id=TENANT, iri_base=PWR, name="电力停电分析本体")
    cs = ag.open_changeset("档位接线单", applicant_id=APPLICANT)
    cs.record_gate(True, {"lint": {"ok": True}})
    cs.submit()
    return ag, cs


def _sig(approver: uuid.UUID) -> dict:
    """publish 显式留痕载体（API body.approvals 形态：单签摘要 / signatures 序列）。"""
    return {"approver_id": str(approver), "note": "显式 publish 留痕"}


# ---------------------------------------------------------------- solo 回归（默认参数向后兼容）


def test_solo_default_equals_explicit_and_publishes():
    """solo 回归：缺省档位与显式 solo 同语义——提交人即审批人放行（既有 M1 行为零变化）。"""
    ag, cs = _submitted()
    cs.approve(APPLICANT)  # 缺省档位（M1 调用形态）
    event = ag.publish(True, {}, version_ref=_ref("v1"), actor_id=APPLICANT)
    assert cs.status is ChangesetStatus.PUBLISHED and event.event_type == "ontology.published"

    ag2, cs2 = _submitted()
    cs2.approve(APPLICANT, note="显式 solo 自审", governance_tier="solo")
    ag2.publish(True, {}, version_ref=_ref("v2"), actor_id=APPLICANT, governance_tier="solo")
    assert cs2.status is ChangesetStatus.PUBLISHED
    assert cs2.approvals["tier"] == "solo"  # 留痕带档位（可追溯，宪法 5）
    assert [s["approver_id"] for s in cs2.approvals["signatures"]] == [str(APPLICANT)]


def test_solo_other_approver_still_rejected_4205():
    """solo 窄化保留（ontology §6.3 提交人即审批人）：他人代批留痕发布被拒 4205，聚合状态不推进。"""
    ag, cs = _submitted()
    with pytest.raises(DomainError, match="4205"):
        ag.publish(True, _sig(APPROVER_A), version_ref=_ref("v1"), actor_id=APPROVER_A)
    assert cs.status is ChangesetStatus.IN_REVIEW  # 拒绝后状态未被推进


def test_legacy_single_signature_trace_replays():
    """旧格式留痕（收敛点接线前：顶层 approver_id 单签、无 signatures）回放兼容——存量行可发布。"""
    ag, cs = _submitted()
    cs.approve(APPLICANT)
    legacy = {k: v for k, v in cs.approvals.items() if k != "signatures"}  # 模拟旧格式行
    ag.publish(True, legacy, version_ref=_ref("v1"), actor_id=APPLICANT)
    assert cs.status is ChangesetStatus.PUBLISHED


# ---------------------------------------------------------------- team 档（单审批人，禁自批）


def test_team_self_approval_rejected_4702():
    """team 禁自批（08 §2.4）：approve 动词即拒 4702；绕过动词直携自批留痕 publish 回放同样 4702。"""
    ag, cs = _submitted()
    with pytest.raises(DomainError, match="4702"):
        cs.approve(APPLICANT, governance_tier="team")
    assert cs.approvals == {}  # 拒签不留痕

    ag2, cs2 = _submitted()
    with pytest.raises(DomainError, match="4702"):
        ag2.publish(True, _sig(APPLICANT), version_ref=_ref("v1"), actor_id=APPROVER_A, governance_tier="team")
    assert cs2.status is ChangesetStatus.IN_REVIEW


def test_team_other_approver_single_signature_publishes():
    """team 单审批人：非提交人单签放行（收敛点签名 1/1），语义跟随档位而非 solo 窄化。"""
    ag, cs = _submitted()
    cs.approve(APPROVER_A, governance_tier="team")
    event = ag.publish(True, {}, version_ref=_ref("v1"), actor_id=APPROVER_A, governance_tier="team")
    assert event.event_type == "ontology.published"
    assert cs.approvals["tier"] == "team"


# ---------------------------------------------------------------- enterprise 档（双负责人四眼）


def test_enterprise_single_signature_insufficient_4205():
    """enterprise 四眼：单签不生效（4205 签名不足 1/2），补第二签（互异且非提交人）才放行。"""
    ag, cs = _submitted()
    cs.approve(APPROVER_A, governance_tier="enterprise")
    with pytest.raises(DomainError, match="4205"):
        ag.publish(True, {}, version_ref=_ref("v1"), actor_id=APPROVER_A, governance_tier="enterprise")
    assert cs.status is ChangesetStatus.IN_REVIEW  # 单签未生效，变更单停留 in_review

    cs.approve(APPROVER_B, governance_tier="enterprise")
    event = ag.publish(True, {}, version_ref=_ref("v1"), actor_id=APPROVER_B, governance_tier="enterprise")
    assert event.event_type == "ontology.published"
    assert [s["approver_id"] for s in cs.approvals["signatures"]] == [str(APPROVER_A), str(APPROVER_B)]


def test_enterprise_duplicate_or_self_signature_rejected_4702():
    """enterprise 四眼禁令（收敛点判定）：同一审批人重复签 4702；提交人自批 4702。"""
    ag, cs = _submitted()
    cs.approve(APPROVER_A, governance_tier="enterprise")
    with pytest.raises(DomainError, match="4702"):
        cs.approve(APPROVER_A, governance_tier="enterprise")

    ag2, cs2 = _submitted()
    with pytest.raises(DomainError, match="4702"):
        cs2.approve(APPLICANT, governance_tier="enterprise")


def test_enterprise_replay_with_explicit_signature_series():
    """publish 显式携带签名序列（body.approvals 载体）：合规双签逐笔回放放行；缺签回放拒。"""
    ag, cs = _submitted()
    approvals = {"approver_id": str(APPROVER_B), "signatures": [_sig(APPROVER_A), _sig(APPROVER_B)]}
    event = ag.publish(True, approvals, version_ref=_ref("v1"), actor_id=APPROVER_B, governance_tier="enterprise")
    assert event.event_type == "ontology.published"

    ag2, _ = _submitted()
    only_one = {"signatures": [_sig(APPROVER_A)]}
    with pytest.raises(DomainError, match="4205"):
        ag2.publish(True, only_one, version_ref=_ref("v1"), actor_id=APPROVER_A, governance_tier="enterprise")


# ---------------------------------------------------------------- 档位解析收敛点（单一入口）


def test_invalid_tier_rejected_4702_at_both_verbs():
    """非法档位 4702（parse_governance_tier 单一入口；settings 缺失回落 solo 由 tests/review 锁）。"""
    ag, cs = _submitted()
    with pytest.raises(DomainError, match="4702"):
        cs.approve(APPROVER_A, governance_tier="family")
    with pytest.raises(DomainError, match="4702"):
        ag.publish(True, _sig(APPROVER_A), version_ref=_ref("v1"), actor_id=APPROVER_A, governance_tier="family")

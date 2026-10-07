"""L3 元素生命周期用例：撤除=标记（ONT-1.3/1.4）+ 候选拒绝与再提翻倍（ONT-1.3 层三）。

撤除语义（契约 06 篇 §ONT-1.3/1.4）：
- 撤除=打 withdrawn_at/withdrawn_reason 标记，**永不物理删行**（状态机外化）；
- classes/properties 撤除前 usage 守卫：kb 域全历史引用 >0 → 拒绝（409 语义）并审计
  ontology.usage.guard_triggered；rule/axiom 撤除同事务审计 ontology.criterion.changed
  （第 3 档：派生结论保解释、对账不级联删）；
- 撤除只作用于 head 版本行（旧版本行=历史，不动）；同版本幂等重投影由 repo 携带标记
  （replace_read_model，重放永不洗掉撤除标记）。

分层：L2 router → 本模块 → L4 Protocol（OntologyRepository）；禁 import L6/gateway。
"""

from __future__ import annotations

import uuid

from services.ontology.domain.model.audit_actions import OntologyAuditAction
from services.ontology.domain.model.ontology import DomainError, Ontology
from services.ontology.domain.repo.ontology_repo import OntologyRepository

_USAGE_GUARD_TYPES = ("class", "property")  # ONT-1.4 第 1 档：仅类/属性撤除有 usage 守卫
_CRITERION_TYPES = ("rule", "axiom")  # ONT-1.4 第 3 档：rule/axiom 撤除审计 criterion.changed


class UsageGuardTriggered(DomainError):
    """ONT-1.4 第 1 档 usage 守卫命中（409 语义）。

    守卫审计行已随当前事务写入但**拒绝路径会回滚业务事务**——路由侧捕获本类型后须先
    显式提交（事务内仅守卫审计写入，无业务变更可回滚），令审计越过拒绝存活（可追溯）。
    """


async def withdraw_read_model_element(
    repo: OntologyRepository,
    ontology: Ontology,
    *,
    element_type: str,
    key: str,
    reason: str,
    actor_id: uuid.UUID | None,
    trace_id: str | None = None,
) -> int:
    """撤除读模型元素（用例）：usage 守卫（类/属性）→ 打 withdrawn 标记 → 审计三件套。

    返回标记行数；元素不存在/已撤除 → DomainError 4202（调用方 domain_error 转 409，4202 码
    随消息前缀透出）；守卫命中 → DomainError 4207（同转 409，提示先迁移 kb 引用）并留
    usage.guard_triggered 审计。
    """
    if ontology.head_version is None:
        raise DomainError("4201 ONTOLOGY_NOT_PUBLISHED: 本体尚未发布，无可撤除的读模型元素")
    reason = (reason or "").strip()
    if not reason:
        raise DomainError("4205 WITHDRAW_REASON_REQUIRED: 撤除必须附理由（状态机外化留痕，ONT-1.3）")
    if element_type in _USAGE_GUARD_TYPES:
        refs = await repo.kb_usage_count(element_type, key)
        if refs > 0:  # 第 1 档：账本行挡删——拒绝 + 守卫审计（同事务，随回滚一并消失）
            await repo.record_audit(
                actor_id=actor_id,
                action=OntologyAuditAction.USAGE_GUARD_TRIGGERED,
                ontology_id=ontology.id,
                digest={"element_type": element_type, "key": key, "kb_references": refs, "decision": "rejected"},
                trace_id=trace_id,
            )
            raise UsageGuardTriggered(
                f"4207 USAGE_GUARD_TRIGGERED: kb 域存在 {refs} 条对该 IRI 的全历史引用——"
                "先迁移引用再撤除（ONT-1.4 第 1 档）"
            )
    marked = await repo.withdraw_head_element(
        ontology.id, version=ontology.head_version.version, element_type=element_type, key=key, reason=reason
    )
    if marked == 0:
        raise DomainError(f"4202 ELEMENT_NOT_FOUND: 元素不存在或已撤除（{element_type} {key}）")
    await repo.record_audit(
        actor_id=actor_id,
        action=OntologyAuditAction.ELEMENT_WITHDRAWN,
        ontology_id=ontology.id,
        digest={"element_type": element_type, "key": key, "reason": reason, "version": ontology.head_version.version},
        trace_id=trace_id,
    )
    if element_type in _CRITERION_TYPES:  # 第 3 档：派生结论保解释——criterion.changed 对账
        await repo.record_audit(
            actor_id=actor_id,
            action=OntologyAuditAction.CRITERION_CHANGED,
            ontology_id=ontology.id,
            digest={"element_type": element_type, "key": key, "reason": "withdrawn"},
            trace_id=trace_id,
        )
    return marked


async def decline_candidate(
    repo: OntologyRepository,
    ontology: Ontology,
    *,
    element_type: str,
    row_id: uuid.UUID,
    reason: str,
    actor_id: uuid.UUID | None,
    trace_id: str | None = None,
) -> None:
    """候选拒绝（防重提层三落点）：llm_candidate 行打 declined_reason + proposal.declined 审计。

    理由必附且去空白（4205，同 withdraw/reject 口径）；行不存在/非 llm_candidate/已 declined
    → DomainError 4202（调用方 domain_error 转 409，4202 码随消息前缀透出）。
    """
    if element_type not in ("axiom", "rule"):
        raise DomainError(f"4202 CANDIDATE_TYPE_INVALID: 候选仅存在 axiom/rule（收到 {element_type}）")
    reason = (reason or "").strip()
    if not reason:
        raise DomainError("4205 DECLINE_REASON_REQUIRED: 拒绝必须附理由（防重提留痕，ONT-1.3 层三）")
    declined = await repo.decline_candidate(
        ontology.id, element_type=element_type, row_id=row_id, reason=reason, actor_id=actor_id, trace_id=trace_id
    )
    if not declined:
        raise DomainError(f"4202 CANDIDATE_NOT_DECLINABLE: 候选不存在或非 llm_candidate 或已拒绝（{row_id}）")


async def assert_resubmission_allowed(
    repo: OntologyRepository,
    ontology: Ontology,
    *,
    element_type: str,
    element_key: str,
    evidence_count: int,
) -> None:
    """同形候选再提判据（ONT-1.3 防重提层三）：declined 历史存在时要求证据量翻倍。

    生成端（LLM 候选写入，未上线）在落库前调用；declined 历史 max(evidence_count)×2 为下限，
    不足即 DomainError 4208。无 declined 历史（首提）不受限。
    """
    floor = await repo.declined_evidence_floor(ontology.id, element_type=element_type, element_key=element_key)
    if floor is not None and evidence_count < floor:
        raise DomainError(
            f"4208 EVIDENCE_INSUFFICIENT: 同形候选曾被拒绝——再提需证据量翻倍"
            f"（要求 ≥{floor}，实际 {evidence_count}，ONT-1.3）"
        )

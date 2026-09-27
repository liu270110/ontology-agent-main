"""L4 领域模型：治理三档审批链（权威=architecture/08 §2.4 治理档位全平台定义）。

**单一收敛点裁决（任务 5 批硬约束）**：档位解析（:func:`parse_governance_tier`）、
各档签名数（:func:`required_signatures`）、单笔审批是否放行（:func:`resolve_decision`）
全部收敛在本文件纯函数——任何模块（插件上架/本体/记忆/RSI）接入审批链只许调这里，禁散写。

三档审批链（08 §2.4 表）：
- solo：提交人即审批人（高置信自动通过留痕语义；未冻结前不开自动通道——本函数只做
  「允许自审」判定，自动直通不在本批范围）；
- team：单审批人，**禁自批**（approver == submitter 即拒，4702）；
- enterprise：双负责人四眼——依序两个**互不相同且均非提交人**的审批人签齐才算过
  （profiles 的 owner_business/owner_engineer 语义由调用方映射为两个 approver 身份）。

共同底线：档位只调节审批链强度，不调节确定性硬门禁（门禁在插件 submit 用例服务端实跑）；
审批全程留痕（payload.approvals 追加，JSONB 整体重赋值）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from services.platform.kernel import DomainError

GovernanceAction = Literal["approve", "reject"]


class GovernanceTier(StrEnum):
    """治理档位（08 §2.4；租户级配置，存 tenants.settings.governance_tier）。"""

    SOLO = "solo"
    TEAM = "team"
    ENTERPRISE = "enterprise"


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """单笔审批判定结果（纯值；complete=签名集齐可终审生效）。"""

    action: GovernanceAction
    allowed: bool
    reason: str
    signatures_required: int
    signatures_collected: int

    @property
    def complete(self) -> bool:
        return self.allowed and self.signatures_collected >= self.signatures_required


def parse_governance_tier(raw: str | None) -> GovernanceTier:
    """档位解析单一入口：非法/缺失配置一律 DomainError（4207 语义同款——档位配置非法）。

    缺失（settings 未写 governance_tier）回落 solo——M1 起种子租户默认 solo
    （m1_seed_roles_and_admin settings='{"governance_tier": "solo"}'）。
    """
    if raw is None or raw == "":
        return GovernanceTier.SOLO
    try:
        return GovernanceTier(raw)
    except ValueError as exc:
        raise DomainError(f"4702 GOVERNANCE_TIER_INVALID: 治理档位配置非法: {raw!r}（08 §2.4）") from exc


def required_signatures(tier: GovernanceTier) -> int:
    """各档通过所需审批人签名数（solo=1 / team=1 / enterprise=2 四眼）。"""
    return 2 if tier is GovernanceTier.ENTERPRISE else 1


def resolve_decision(
    tier: GovernanceTier,
    *,
    action: GovernanceAction,
    submitter_id: uuid.UUID | None,
    approver_id: uuid.UUID,
    prior_approvers: tuple[uuid.UUID, ...] = (),
) -> ApprovalDecision:
    """单笔审批判定（收敛点；三档差异只许在此实现）。

    - reject：任一档均单审批人可驳回（附理由留痕，08 §4 REJ 回边）；
    - approve：solo 允许自审（提交人即审批人，留痕可回滚）；team 禁自批；
      enterprise 禁自批 + 禁同一审批人重复签（四眼=两个不同审批人依序）。
    """
    if action == "reject":
        return ApprovalDecision(
            action="reject",
            allowed=True,
            reason="驳回单审批人即可（附理由留痕）",
            signatures_required=1,
            signatures_collected=1,
        )

    required = required_signatures(tier)
    if approver_id == submitter_id:
        if tier is GovernanceTier.SOLO:
            return ApprovalDecision(
                action="approve",
                allowed=True,
                reason="solo 档：提交人即审批人（留痕自动语义，可回滚）",
                signatures_required=required,
                signatures_collected=len(prior_approvers) + 1,
            )
        hint = "enterprise 双负责人四眼" if tier is GovernanceTier.ENTERPRISE else "team 单审批人"
        return ApprovalDecision(
            action="approve",
            allowed=False,
            reason=f"{tier.value} 档禁自批（{hint}，08 §2.4）",
            signatures_required=required,
            signatures_collected=len(prior_approvers),
        )
    if tier is GovernanceTier.ENTERPRISE and approver_id in prior_approvers:
        return ApprovalDecision(
            action="approve",
            allowed=False,
            reason="enterprise 四眼：同一审批人不得重复签（两个不同审批人依序）",
            signatures_required=required,
            signatures_collected=len(prior_approvers),
        )
    return ApprovalDecision(
        action="approve",
        allowed=True,
        reason=f"{tier.value} 档审批通过签名 {len(prior_approvers) + 1}/{required}",
        signatures_required=required,
        signatures_collected=len(prior_approvers) + 1,
    )


class GovernanceTierReader(Protocol):
    """租户治理档位读取端口（governance_tier 走 tenants.settings——08 §2.4 权威存储位）。"""

    async def get_tier(self, tenant_id: uuid.UUID) -> GovernanceTier:
        """读取租户档位；缺失回落 solo，非法值抛 DomainError（parse_governance_tier 收敛）。"""
        ...  # pragma: no cover — Protocol 方法无实现

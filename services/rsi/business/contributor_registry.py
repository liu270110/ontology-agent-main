"""ORSI 贡献者注册面（architecture/09 §14.1 注册流 + §14.3 初次配置；批次 A）。

**红线（§14.1 + §13 阶段 A 裁决继承）**：本服务只登记与检索——注册≠生效，外部贡献只进
候选池，apply 恒拒绝红线不变；不触 RsiService/gates/apply/proposal 进化路径（行为断言+
源断言 tests/rsi/test_contributor.py，test_orsi_registry.py 同款）。注册动作落审计
（09 §6 红线 7；审计行是登记留痕而非进化副作用）。

SHACL 门禁收口（宪法 2/3：外部输入必须过校验）：注册 payload 先过 rsi:ContributorShape
（services/ontology/core/rsi_tbox.validate_contributor）——违规即 ContributorRejected
（携带违规清单回执，不静默）；领域构造期再做 slug 形构/档位值域机械校验。注册成功即生成
投递目录约定路径记录（§14.1 投递目录约定，字符串记录不触真实 FS）。
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from services.ontology.core.rsi_tbox import validate_contributor
from services.rsi.audit import AuditTrail, InMemoryAuditTrail, RsiAuditRecord
from services.rsi.domain.contributor import (
    DEFAULT_TRUST_SCORE,
    ContributorNotFound,
    ContributorRejected,
    GovernanceTier,
    RsiContributor,
    build_delivery_dir,
)
from services.rsi.domain.repo.contributor import ContributorFilter, ContributorRepository

# 审计动作（audit.py 集中登记风格；rsi.contributor 前缀=注册表面，非进化动作）
ACTION_CONTRIBUTOR_REGISTERED = "rsi.contributor.registered"
ACTION_CONTRIBUTOR_REJECTED = "rsi.contributor.rejected"
ACTION_CONTRIBUTOR_PROBED = "rsi.contributor.probed"


class ContributorService:
    """贡献者注册用例编排：注册/列表/详情/探测档案回写（零进化副作用；全动作审计）。

    repo 经构造注入（组合根/路由每请求装配 PgContributorRepository(db, tenant_id)；
    测试用内存假体）；审计汇缺省内存环形（audit.py 既有口径）。
    """

    def __init__(self, *, repo: ContributorRepository, audit_trail: AuditTrail | None = None) -> None:
        self._repo = repo
        self.audit_trail = audit_trail or InMemoryAuditTrail()

    # ------------------------------------------------------------- 注册（写面唯一入口）

    async def register(
        self,
        *,
        tenant_id: uuid.UUID,
        contributor_id: str,
        display_name: str,
        governance_tier: GovernanceTier | str = GovernanceTier.TEAM,
        trust_score: Decimal | str | None = None,
        trace_id: str | None = None,
    ) -> RsiContributor:
        """贡献者注册（SHACL 门禁 + 投递目录记录 + 落库 + 审计；注册≠生效红线）。

        - SHACL 违规 → ContributorRejected（回执违规清单；无标注/不合规产物不上架同铁律）；
        - slug 形构/档位值域违规 → ContributorError/ValueError（路由层映射 3001/400）；
        - 同 contributor_id 重复注册 → DuplicateContributorId（路由层映射 409）；
        - trust_score 缺省 1.0（§14.1 初始信誉分；批次 C 前无滑动窗判定）；
        - **本方法不触 proposal/门禁/apply 路径**（红线，测试断言锚点）。
        """
        score = Decimal(str(trust_score)) if trust_score is not None else DEFAULT_TRUST_SCORE

        # SHACL 门禁先行（rsi:ContributorShape；拒绝即回执违规清单——不静默不代写）。
        # 门禁吃原始输入（档位枚举构造放门禁后）：本体=控制面，值域违规同走 SHACL 回执路径。
        raw_tier = governance_tier.value if isinstance(governance_tier, GovernanceTier) else str(governance_tier)
        violations = validate_contributor(
            {
                "contributor_id": contributor_id,
                "display_name": display_name,
                "governance_tier": raw_tier,
                "trust_score": str(score),
            }
        )
        if violations:
            await self.audit_trail.record(
                RsiAuditRecord(
                    action=ACTION_CONTRIBUTOR_REJECTED,
                    outcome="rejected",
                    detail={"contributor_id": contributor_id, "violations": violations},
                    trace_id=trace_id,
                )
            )
            raise ContributorRejected(f"贡献者注册未过 rsi:ContributorShape 门禁: {'; '.join(violations)}")

        tier = GovernanceTier(governance_tier)  # 门禁后枚举化（值域已由 sh:in 收口）
        contributor = RsiContributor(
            tenant_id=tenant_id,
            contributor_id=contributor_id,
            display_name=display_name,
            governance_tier=tier,
            trust_score=score,
            delivery_dir=build_delivery_dir(contributor_id),  # 投递目录约定路径记录（§14.1）
        )
        await self._repo.add(contributor)
        await self.audit_trail.record(
            RsiAuditRecord(
                action=ACTION_CONTRIBUTOR_REGISTERED,
                outcome="ok",
                detail={
                    "contributor_id": contributor.contributor_id,
                    "governance_tier": contributor.governance_tier.value,
                    "trust_score": str(contributor.trust_score),
                    "delivery_dir": contributor.delivery_dir,
                    "tenant_id": str(tenant_id),
                },
                trace_id=trace_id,
            )
        )
        return contributor

    # ------------------------------------------------------------- 检索（读面，零副作用）

    async def list_contributors(self, *, offset: int = 0, limit: int = 20) -> tuple[list[RsiContributor], int]:
        """分页列表（updated_at 倒序），返回 (items, total)。"""
        return await self._repo.list(ContributorFilter(offset=offset, limit=limit))

    async def get_contributor(self, contributor_id: str) -> RsiContributor:
        """详情（租户过滤在仓储；未找到抛 ContributorNotFound → 路由层 404）。"""
        contributor = await self._repo.get(contributor_id)
        if contributor is None:
            raise ContributorNotFound(f"贡献者不存在: {contributor_id}")
        return contributor

    # ------------------------------------------------------------- 探测档案回写（写面，登记语义）

    async def record_probe_profile(
        self, contributor_id: str, profile: dict[str, Any], *, trace_id: str | None = None
    ) -> RsiContributor:
        """探测握手能力面档案回写（§14.3 步 1 产物落档；只登记不装配——装配走 assembly 面）。"""
        contributor = await self.get_contributor(contributor_id)
        contributor.probe_profile = profile
        await self._repo.update(contributor)
        await self.audit_trail.record(
            RsiAuditRecord(
                action=ACTION_CONTRIBUTOR_PROBED,
                outcome="ok",
                detail={
                    "contributor_id": contributor_id,
                    "healthy": bool(profile.get("healthy")),
                    "tool_count": int(profile.get("tool_count") or 0),
                    "source": profile.get("source"),
                },
                trace_id=trace_id,
            )
        )
        return contributor

"""升级单审批接续用例（M4P3-T5：memory_promotions 接入审批中心，L2→L3 骨架闭环）。

编排形态照抄 plugin lifecycle 先例（M5-1）：跨模块端口鸭子类型注入（PromotionReviewPort /
PromotionDecisionPort，实现=review.business.candidates 的 ReviewTicketService / ReviewApprovalService，
组合根 lifespan 装配 app.state.promotion_review / review_approvals——memory 侧零 review import 边，
契约六 review.data 模块私有且豁免边只覆盖 gateway.app）。

用例簇：
- :meth:`PromotionReviewService.submit` —— 升级单两写：memory_promotions(state=submitted) + 审批
  中心工单（target_type=memory_l2_upgrade，六态走 review_workflow，不另造状态机）+ approval_id
  回填；v1 同请求内多写、无跨服务事务（一致性靠状态机幂等 + 对账巡检，TODO(M5) 巡检缝）；
  仅 L2 记录可发起（layer 预检）；同记录已有 open 单（submitted/reviewing/approved）幂等返回
  既有（duplicate=true），不重复建单；
- :meth:`PromotionReviewService.decide` —— 决议回调接续（「approved 不等于生效」08 §4）：
  pending_review 单过三档审批链（禁自批/四眼/越档由收敛点保证，4702/4703）→ complete 且
  approve：仓储 apply_promotion（submitted/approved→applied + records.layer 2→3，06 篇 M4
  「L3/L4 骨架」落点；Neo4j 投影留 TODO 缝）+ 工单 mark_published；complete 且 reject：升级单
  rejected、记录保留 L2 不动（06 篇 §5.4 驳回退回）；未集齐：原状续等（enterprise 首签）；
  工单已 approved 的重入走幂等补齐（plugin catch-up 先例）。

触发方归属登记（M4P3-T5 评审修订）：decide 的生产触发方为 M5 审批中心 admin 决议分发（对齐
plugin 先例的分期写法——PluginMarketService.review_decision 同为服务方法级编排）；当前为服务
方法级半闭环，决议经 admin REST（POST /admin/reviews/{id}/decision）时不自动联动 memory，
一致性靠 apply_promotion 幂等重入（submitted/approved→applied 容差）+ 对账巡检 TODO(M5)。

事务纪律：仓储方法各自短事务（SessionDep 提交由调用方编排面决定）；审批端口自持短事务。
"""

from __future__ import annotations

import uuid
from typing import Any

from services.memory.data.repositories.records_repo import MemoryRepository
from services.memory.domain.model.memory import MemoryLayer
from services.memory.domain.repo.review_port import PromotionDecisionPort, PromotionReviewPort
from services.platform.kernel import DomainError

_TARGET_TYPE = "memory_l2_upgrade"


class PromotionReviewService:
    """L2→L3 升级单审批编排（records 仓储 + 工单/决策端口鸭子消费；组合根/测试装配）。"""

    def __init__(self, repo: MemoryRepository, review: PromotionReviewPort, approvals: PromotionDecisionPort) -> None:
        self._repo = repo
        self._review = review
        self._approvals = approvals

    # ---- 提交：升级单 + 审批中心建单（同请求两写）----

    async def submit(
        self,
        *,
        tenant_id: uuid.UUID,
        record_id: uuid.UUID,
        to_layer: int,
        submitter_id: uuid.UUID | None = None,
        trace_id: str = "",
    ) -> dict[str, Any]:
        """登记升级申请并同步建审批工单，返回 {id, state, approval_id, duplicate}。

        幂等预检：同记录已有 open 单（submitted/reviewing/approved）→ 原样返回既有
        （duplicate=true），不重复建升级单/审批工单；记录不存在抛 LookupError；非 L2 记录抛
        ValueError（仅 L2 可发起升级，06 篇 §5.4，API 映射 422）。
        """
        rec = await self._repo.get(tenant_id, record_id)  # 存在性 + 归属双校验（封跨租户引用）
        if rec is None:
            raise LookupError(f"record not found: {record_id}")
        if rec.layer != MemoryLayer.USER:  # L2 起点固定（06 篇 §5.4 v1：from_layer=2）
            raise ValueError(f"仅 L2 记录可发起升级（当前 L{int(rec.layer)}）: {record_id}")
        open_promos = await self._repo.list_open_promotions(tenant_id, record_id=record_id)
        if open_promos:  # 幂等：未终态升级单在审，不重复新建（对齐 uk_review_one_open 口径）
            existing = open_promos[0]
            return {
                "id": existing["id"],
                "state": existing["state"],
                "approval_id": existing["approval_id"],
                "duplicate": True,
            }
        promo_id = await self._repo.add_promotion(tenant_id, record_id=record_id, to_layer=to_layer)
        ticket_id = await self._review.submit_candidate(
            tenant_id=tenant_id,
            target_type=_TARGET_TYPE,
            target_id=promo_id,  # 工单多态引用 = 升级单 id（uk_review_one_open 同对象唯一 open 单）
            payload={
                "envelope_version": 1,
                "candidate_type": _TARGET_TYPE,
                "promotion_id": str(promo_id),
                "record_id": str(record_id),
                "from_layer": 2,
                "to_layer": to_layer,
                "trace_id": trace_id,
                "record_digest": {  # 审阅上下文（审什么：06 篇 §5.4 共享记忆涉及他人）
                    "record_type": str(rec.record_type),
                    "content": rec.content,
                    "subject_iri": rec.subject_iri,
                    "confidence": rec.confidence,
                },
            },
            submitter_id=submitter_id,
        )
        await self._repo.set_promotion_approval(tenant_id, promo_id, approval_id=ticket_id)
        return {"id": promo_id, "state": "submitted", "approval_id": ticket_id, "duplicate": False}

    # ---- 决议回调：审批链 + 生效联动（approved 不等于生效）----

    async def decide(
        self,
        *,
        tenant_id: uuid.UUID,
        promotion_id: uuid.UUID,
        action: str,
        approver_id: uuid.UUID,
        note: str = "",
    ) -> dict[str, Any]:
        """审批决议接续：返回 {promotion_id, ticket_id, ticket_status, promotion_state, applied}。

        触发方登记：生产触发方=M5 审批中心 admin 决议分发（分期写法对齐 plugin 先例）；当前
        服务方法级半闭环——admin REST 决议不自动联动 memory，靠本方法幂等重入 + 巡检 TODO(M5)。
        状态机：complete+approve → apply_promotion + 工单 published；complete+reject → 升级单
        rejected（记录 L2 不动）；未集齐 → pending_review 续等；工单已 approved 的重入走幂等
        补齐（不重复签名/不重复 apply）；终态单迟到决策由审批链 4703 拒绝。
        """
        if action not in ("approve", "reject"):
            raise DomainError("3001 PARAM_INVALID: action 仅支持 approve/reject")
        promo = await self._repo.get_promotion(tenant_id, promotion_id)
        if promo is None:
            raise LookupError(f"升级单不存在: {promotion_id}")
        ticket_id = promo["approval_id"]
        if ticket_id is None:
            # 4705 未占用（全仓 47xx 在用仅 4701/4702/4703，2026-09-28 核对）；02 §7 回登随 M5 收口
            raise DomainError(f"4705 PROMOTION_TICKET_MISSING: 升级单无审批工单（对账巡检应收敛）: {promotion_id}")
        ticket = await self._review.get_ticket(tenant_id=tenant_id, ticket_id=ticket_id)
        if ticket is None:
            raise LookupError(f"审核单不存在: {ticket_id}")

        catch_up = ticket["status"] == "approved" and action == "approve"  # 幂等补齐重入（plugin 先例）
        if not catch_up:
            result = await self._approvals.decide(
                tenant_id=tenant_id, ticket_id=ticket_id, action=action, approver_id=approver_id, note=note
            )
            if not result.decision.complete:  # enterprise 多签续等：单据/升级单均原状
                return self._outcome(promotion_id, ticket_id, "pending_review", promo["state"], applied=False)
            if result.status == "rejected":
                await self._repo.reject_promotion(tenant_id, promotion_id)  # 记录保留 L2 不动（06 篇 §5.4）
                return self._outcome(promotion_id, ticket_id, "rejected", "rejected", applied=False)
            ticket = await self._review.get_ticket(tenant_id=tenant_id, ticket_id=ticket_id)  # 决策后重读落态
            if ticket is None:  # 理论不可达；防御性留痕
                raise LookupError(f"审核单不存在: {ticket_id}")

        applied = await self._repo.apply_promotion(tenant_id, promotion_id)
        state = await self._promo_state(tenant_id, promotion_id)
        ticket_status = ticket["status"]
        if state == "applied" and ticket_status == "approved":  # published 才生效；已 published 幂等跳过
            await self._review.mark_published(
                tenant_id=tenant_id, ticket_id=ticket_id, note=note or "L2→L3 骨架升级生效（records.layer=3）"
            )
            ticket_status = "published"
        return self._outcome(promotion_id, ticket_id, ticket_status, state, applied=applied)

    async def _promo_state(self, tenant_id: uuid.UUID, promotion_id: uuid.UUID) -> str:
        promo = await self._repo.get_promotion(tenant_id, promotion_id)
        if promo is None:  # 理论不可达（decide 已校验）；防御性留痕
            raise LookupError(f"升级单不存在: {promotion_id}")
        return promo["state"]

    @staticmethod
    def _outcome(
        promotion_id: uuid.UUID, ticket_id: uuid.UUID, ticket_status: str, promotion_state: str, *, applied: bool
    ) -> dict[str, Any]:
        return {
            "promotion_id": promotion_id,
            "ticket_id": ticket_id,
            "ticket_status": ticket_status,
            "promotion_state": promotion_state,
            "applied": applied,
        }

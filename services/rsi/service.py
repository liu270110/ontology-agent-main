"""RSI 阶段 A 服务面（architecture/09 §9 阶段 A 收缩：骨架 + 最小闭环框架）。

做了什么（阶段 A 范围内）：
- 双轨触发注册面（triggers.py）+ 候选池（进程内，PG 表 DDL 欠账见报告）；
- 五类白名单机械校验（whitelist.py），白名单外拒绝记安全审计（09 §3 铁律）；
- 三级门禁链骨架（gates.py）：0 级硬门槛真跑，①②③为 M5+ 演练位；
- 状态机推进（draft→evaluated→rejected；其余迁移由状态机断言承载）；
- 全动作审计（09 §6 红线 7）。

明确不做（红线与收缩边界）：
- **apply 恒拒绝**：``apply()`` 是唯一"生效"入口，恒抛 ``RsiApplyForbiddenError`` 并留审计
  （登记「M5+ 启用」）——本批无自改能力，候选非成品宪法（09 §1 宪法 1）的机械执行点；
- 不接 LLM 归因/起草（复盘雏形的语义步随评估批次接入）；不接沙箱/金标/灰度评估；
- 不接审核工作流工单（08 §4，M5 复用 review_workflow target_type=rsi_proposal）；
- 不写库（rsi_proposals DDL 欠账）；无 /api/v1/rsi/* 端点（09 §10 随阶段 B 详设）。
"""

from __future__ import annotations

import uuid
from typing import Any

from services.rsi.audit import (
    ACTION_APPLY_DENIED,
    ACTION_WHITELIST_VIOLATION,
    AuditTrail,
    InMemoryAuditTrail,
    RsiAuditRecord,
)
from services.rsi.gates import build_eval_report, evaluate_chain
from services.rsi.proposal import Proposal, ProposalError, ProposalStatus, TriggerTrack
from services.rsi.triggers import TriggerRegistry
from services.rsi.whitelist import WhitelistViolation, validate_improvement

APPLY_ENABLED_STAGE = "M5+"  # 生效通路启用里程碑（09 §9 阶段 B）；阶段 A 恒拒


class RsiApplyForbiddenError(Exception):
    """apply 恒拒（阶段 A 红线）：任何候选、任何状态、任何操作者均不得经 RSI 直接生效。"""


class RsiService:
    """RSI 阶段 A 门面：触发注册/候选受理/门禁评估/apply 恒拒（全动作审计）。"""

    def __init__(self, *, audit_trail: AuditTrail | None = None) -> None:
        self.audit_trail = audit_trail or InMemoryAuditTrail()
        self.triggers = TriggerRegistry(audit_trail=self.audit_trail)
        self.pool: dict[uuid.UUID, Proposal] = {}  # 候选池（进程内；PG 承载随 DDL 欠账清偿）
        self.apply_enabled = False  # 红线位：恒 False（M5+ 通道启用位，阶段 A 不提供翻转入口）

    # ------------------------------------------------------------- 触发注册

    def register_experience_trigger(self, name: str, handler: Any) -> None:
        """经验轨触发器注册（事件驱动；见 TriggerRegistry.register）。"""
        self.triggers.register(TriggerTrack.EXPERIENCE, name, handler)

    def register_metric_trigger(self, name: str, handler: Any, *, interval_s: float) -> None:
        """指标轨触发器注册（周期驱动，interval_s 为周期秒数）。"""
        self.triggers.register(TriggerTrack.METRIC, name, handler, interval_s=interval_s)

    async def fire_trigger(self, event: Any) -> list[Proposal]:
        """触发分派入口：产出候选收进候选池（白名单校验 + 审计），返回池内候选。"""
        produced = await self.triggers.fire(event)
        accepted: list[Proposal] = []
        for proposal in produced:
            try:
                await self._accept(proposal)
                accepted.append(proposal)
            except WhitelistViolation:
                continue  # 白名单外候选不入池（安全审计已在 _accept 落）
        return accepted

    # ------------------------------------------------------------- 候选受理与评估

    async def submit(
        self,
        *,
        tenant_id: uuid.UUID,
        type_str: str,
        target: str,
        envelope: dict[str, Any],
        trigger: TriggerTrack,
        source_trace_ids: tuple[str, ...] = (),
    ) -> Proposal:
        """候选受理入口（复盘归因/指标退化侧的统一落池口；白名单校验先行）。

        白名单外 → 安全审计（09 §3 铁律）后上抛 WhitelistViolation，不入池。
        """
        try:
            improvement_type = validate_improvement(type_str, target)
        except WhitelistViolation as exc:
            await self.audit_trail.record(
                RsiAuditRecord(
                    action=ACTION_WHITELIST_VIOLATION,
                    outcome="rejected",
                    detail={"reason": str(exc), "type": type_str, "target": target},
                    trace_id=source_trace_ids[0] if source_trace_ids else None,
                )
            )
            raise
        proposal = Proposal(
            tenant_id=tenant_id,
            type=improvement_type,
            target=target,
            trigger=trigger,
            envelope=dict(envelope),
            source_trace_ids=tuple(source_trace_ids),
        )
        await self._accept(proposal)
        return proposal

    async def evaluate(self, proposal_id: uuid.UUID) -> Proposal:
        """三级门禁链评估（阶段 A 演练位）：0 级 fail → rejected（归因留档）；pass → evaluated。"""
        proposal = self._require(proposal_id)
        passed, results = evaluate_chain(proposal)
        proposal.eval_report = build_eval_report(results)
        if not passed:
            proposal.transition(ProposalStatus.REJECTED)  # 门禁拒绝进终态（09 §5 状态机主链）
            await self.audit_trail.record(
                RsiAuditRecord(
                    action="rsi.evaluate",
                    outcome="rejected",
                    proposal_id=str(proposal.id),
                    detail={"level0": results[0].verdict, "target": proposal.target},
                )
            )
            return proposal
        proposal.transition(ProposalStatus.EVALUATED)
        await self.audit_trail.record(
            RsiAuditRecord(
                action="rsi.evaluate",
                outcome="ok",
                proposal_id=str(proposal.id),
                detail={"gates": [r.gate for r in results], "stage": "A_skeleton"},
            )
        )
        return proposal

    # ------------------------------------------------------------- apply（红线）

    async def apply(self, proposal_id: uuid.UUID, *, operator: str | None = None) -> None:
        """生效入口——**恒拒绝**（阶段 A 红线；任何状态/操作者均不可经此生效）。

        机械执行点（09 §1 宪法 1「候选非成品无一生效豁免」+ 收缩裁决「apply 恒拒」）：
        先落拒绝审计（登记 M5+ 启用），再抛 RsiApplyForbiddenError；候选状态不变。
        """
        proposal = self._require(proposal_id)
        await self.audit_trail.record(
            RsiAuditRecord(
                action=ACTION_APPLY_DENIED,
                outcome="denied",
                proposal_id=str(proposal.id),
                detail={
                    "reason": f"RSI apply 随 {APPLY_ENABLED_STAGE} 启用（阶段 A 骨架：apply 恒拒绝）",
                    "operator": operator,
                    "status": proposal.status.value,
                },
            )
        )
        raise RsiApplyForbiddenError(
            f"RSI apply 恒拒绝：改进项生效通路随 {APPLY_ENABLED_STAGE} 启用（阶段 A 骨架红线）"
        )

    # ------------------------------------------------------------- 内部

    async def _accept(self, proposal: Proposal) -> None:
        """候选入池（白名单复验 + 审计；白名单外 → 安全审计 + WhitelistViolation）。"""
        try:
            validate_improvement(proposal.type.value, proposal.target)
        except WhitelistViolation as exc:
            await self.audit_trail.record(
                RsiAuditRecord(
                    action=ACTION_WHITELIST_VIOLATION,
                    outcome="rejected",
                    proposal_id=str(proposal.id),
                    detail={"reason": str(exc), "type": proposal.type.value, "target": proposal.target},
                    trace_id=proposal.source_trace_ids[0] if proposal.source_trace_ids else None,
                )
            )
            raise
        self.pool[proposal.id] = proposal
        await self.audit_trail.record(
            RsiAuditRecord(
                action="rsi.proposal.accepted",
                outcome="ok",
                proposal_id=str(proposal.id),
                detail={"type": proposal.type.value, "target": proposal.target, "trigger": proposal.trigger.value},
                trace_id=proposal.source_trace_ids[0] if proposal.source_trace_ids else None,
            )
        )

    def _require(self, proposal_id: uuid.UUID) -> Proposal:
        proposal = self.pool.get(proposal_id)
        if proposal is None:
            raise ProposalError(f"候选不存在: {proposal_id}")
        return proposal

    async def get_proposal(self, proposal_id: uuid.UUID) -> Proposal:
        """候选读取（含审计面外只读；未找到抛 ProposalError）。"""
        return self._require(proposal_id)

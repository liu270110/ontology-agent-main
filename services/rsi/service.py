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
from collections.abc import Awaitable, Callable
from typing import Any

from services.rsi.audit import (
    ACTION_APPLY_BASELINE_DRIFT,
    ACTION_APPLY_DENIED,
    ACTION_WHITELIST_VIOLATION,
    AuditTrail,
    InMemoryAuditTrail,
    RsiAuditRecord,
)
from services.rsi.gates import build_eval_report, evaluate_chain
from services.rsi.proposal import (
    Proposal,
    ProposalError,
    ProposalStatus,
    TriggerTrack,
    entry_baseline_hash,
)
from services.rsi.triggers import TriggerRegistry
from services.rsi.whitelist import WhitelistViolation, validate_improvement

APPLY_ENABLED_STAGE = "M5+"  # 生效通路启用里程碑（09 §9 阶段 B）；阶段 A 恒拒

# 当前条目内容取数口（K9-b）：target → 条目当前内容（None=条目不存在/已删除）。
# 阶段 A 无真实条目注册表，由组合根按载体注入（测试用闭包假体）；None 注入=无取数口，
# 基线校验跳过（无法取当前内容 ≠ 已漂移，不虚拒）。
EntryLoader = Callable[[str], Awaitable[str | None]]


class RsiApplyForbiddenError(Exception):
    """apply 恒拒（阶段 A 红线）：任何候选、任何状态、任何操作者均不得经 RSI 直接生效。"""


class BaselineDriftError(ProposalError):
    """基线漂移拒（K9-b，G-9）：目标条目内容相对提案创建时快照已变化，拒绝按过期基线生效。

    语义对齐 prime-agent planner.rs:368-379「entry changed during refinement planning」
    即拒（方案依据=docs/Agent/13 §15）；调用方应废弃旧提案重新起草。
    """


class RsiService:
    """RSI 阶段 A 门面：触发注册/候选受理/门禁评估/apply 恒拒（全动作审计）。"""

    def __init__(self, *, audit_trail: AuditTrail | None = None, entry_loader: EntryLoader | None = None) -> None:
        self.audit_trail = audit_trail or InMemoryAuditTrail()
        self.triggers = TriggerRegistry(audit_trail=self.audit_trail)
        self.pool: dict[uuid.UUID, Proposal] = {}  # 候选池（进程内；PG 承载随 DDL 欠账清偿）
        self.apply_enabled = False  # 红线位：恒 False（M5+ 通道启用位，阶段 A 不提供翻转入口）
        self.entry_loader = entry_loader  # K9-b 当前条目内容取数口（None=无取数口，基线校验跳过）

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
        baseline_content: str | None = None,
    ) -> Proposal:
        """候选受理入口（复盘归因/指标退化侧的统一落池口；白名单校验先行）。

        白名单外 → 安全审计（09 §3 铁律）后上抛 WhitelistViolation，不入池。
        ``baseline_content``（K9-b 可选）：受理时对目标能力条目内容取 sha256 快照
        （``entry_baseline_hash`` 口径）落 ``Proposal.baseline_hash``，供 apply/审批通过
        路径执行前比对漂移；``None`` = 不建快照（旧提案形态，apply 跳过基线校验）。
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
            baseline_hash=None if baseline_content is None else entry_baseline_hash(baseline_content),
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
        先过 K9-b 基线校验（有快照且能取到当前内容时，漂移即拒——防「基于过期基线的进化」，
        M5+ 通道启用时本检查点即执行前 CAS 门的执行位置），再落拒绝审计（登记 M5+ 启用），
        抛 RsiApplyForbiddenError；候选状态不变。
        """
        proposal = self._require(proposal_id)
        await self._assert_baseline_fresh(proposal)
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

    # ------------------------------------------------------------- K9-b 基线校验

    async def _assert_baseline_fresh(self, proposal: Proposal) -> None:
        """基线快照比对（K9-b，执行前并发防线；阶段 A 挂在 apply 唯一执行点之前）。

        - ``baseline_hash is None`` → 旧提案无快照，跳过（向后兼容既有调用方）；
        - ``entry_loader is None`` → 无当前内容取数口，跳过（无法取当前内容 ≠ 已漂移，不虚拒；
          M5+ 组合根注入取数口后本防线在真实执行 await 前后各调一次收口双查）；
        - 快照与当前内容不符，或条目已不存在（取数 None，删除亦属漂移）→ 拒绝审计 +
          BaselineDriftError（错误信息含 changed during refinement 语义），候选状态不变。
        """
        if proposal.baseline_hash is None or self.entry_loader is None:
            return
        current = await self.entry_loader(proposal.target)
        if current is not None and entry_baseline_hash(current) == proposal.baseline_hash:
            return
        await self.audit_trail.record(
            RsiAuditRecord(
                action=ACTION_APPLY_BASELINE_DRIFT,
                outcome="rejected",
                proposal_id=str(proposal.id),
                detail={
                    "reason": "目标条目内容相对提案创建时基线快照已漂移",
                    "target": proposal.target,
                    "baseline_hash": proposal.baseline_hash,
                    "entry_present": current is not None,
                },
            )
        )
        raise BaselineDriftError(
            f"目标条目 changed during refinement：{proposal.target} 内容相对基线快照已漂移，"
            "拒绝按过期基线生效（K9-b/G-9；废弃旧提案重新起草）"
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

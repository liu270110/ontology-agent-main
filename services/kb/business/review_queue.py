"""needs_review 聚合复核服务（OntRAG 知识库GraphRAG设计 §7.1 批次纪律 + §8.1 b 类承接判定）。

痛点（主文档 §11 待办「needs_review 聚合复核的交互设计」）：单部标准换版可产生数千条
needs_review 候选（§8.1 b 类「确认删除，转 needs_review」），逐条复核不可运行——审核单元
聚合为 subject/主题批次（§7.1 裁决 2「批次纪律」），一次呈现同组候选摊薄上下文重建成本；
批量通过/驳回走全组裁决，留痕仍逐行落（§7「单据仍逐条留痕，可追溯不破」）。

状态词汇：kb_facts.status 现行 CheckConstraint 为 candidate|rejected|authoritative
（database/01 §3.3 v1 权威）；needs_review 是 §8.1 b 类承接判定的目标态（04 篇 v0.2
state 列，§11 待办回填落库侧另切片）。本服务对词汇不作硬编码断言——statuses 透传过滤，
缺省 ("needs_review",) 即设计目标态；现行 v1 队列（candidate 态）经
statuses=("candidate",) 复用同一聚合/裁决路径，测试亦经此口跑通。

留痕复用结论（任务要求注明）：services/review/business/candidates.``ReviewTicketService``
**可复用**——经鸭子类型端口注入（kb/api 终审工作台同款零静态 import 纪律）：
- 行有 open 审核单（draft/pending_review）→ 决策记录追加进 payload["decisions"]
  （merge_payload 整体重赋值，JSONB 不可原地变更；kb 工作台 _append_decision_trail 同式）；
- 无单/单已终态/票据面故障 → 行内 meta["review_queue"] 最小审计兜底（裁决人/时间/意见
  随裁决同事务落行，宁可行内留痕不产生幽灵痕/零痕；行内审计恒写，单据留痕为增量面）。

事务纪律（03 §6.1 短事务 + 组级原子）：queue_summary 只读；batch_decide 全组单事务提交
（聚合复核语义：同组要么全裁要么全不裁，区别于逐条面部分成功语义）；单据留痕后置
best-effort（留痕先于翻转=中途失败产生幽灵审计，kb 工作台 ocr 评审同结论）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.kb.data.orm import KbFact

logger = logging.getLogger(__name__)

# 队列缺省词汇 = §8.1 b 类 needs_review（目标态；v1 candidate 队列经参数显式传入）
_DEFAULT_STATUSES: tuple[str, ...] = ("needs_review",)
_DECISION_STATUSES: tuple[str, ...] = ("authoritative", "rejected")  # 全组裁决合法值（映射 kb_facts.status）
_TICKET_TARGET_TYPE = "knowledge_instance"  # kb_extraction._persist_candidates 双写同款 target
_TICKET_OPEN_STATUSES = ("draft", "pending_review")  # uk_review_one_open 同口径（kb 工作台同款）
_SAMPLE_LIMIT = 3  # 任务口径：每组样本 chunk/document 引用 ≤3 条（最旧优先）

__all__ = ["BatchDecideOutcome", "ReviewQueueGroup", "ReviewQueueService", "ReviewSampleRef"]


# ---------------------------------------------------------------- 结果载荷（business 层只数据无行为，api 层投影 DTO）


@dataclass(frozen=True, slots=True)
class ReviewSampleRef:
    """组内样本引用（≤3 条，最旧优先）：回指候选事实与其 chunk/document 出处。"""

    fact_id: uuid.UUID
    document_id: uuid.UUID
    chunk_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class ReviewQueueGroup:
    """一个 subject/主题聚合组（§7.1 批次：同组一次呈现、一次裁决）。"""

    subject: str
    subject_type: str | None
    count: int
    oldest_created_at: datetime
    samples: tuple[ReviewSampleRef, ...]


@dataclass(frozen=True, slots=True)
class BatchDecideOutcome:
    """全组裁决结果（decided=裁决行数；trail_recorded=其中落到 open 单留痕的行数）。"""

    subject: str
    decision: str
    decided: int
    trail_recorded: int


class ReviewQueueService:
    """needs_review 聚合复核服务（PG 会话工厂注入；组合根/路由侧装配）。

    ``tickets`` 为可选单据服务端口（鸭子类型满足 review ``ReviewTicketService`` 的
    ``get_latest_ticket``/``merge_payload`` 面，kb/api app.state.candidate_review 同源）——
    未装配时留痕仅行走行内审计（最小实现，见模块头复用结论）。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        tickets: Any = None,
    ) -> None:
        self._factory = session_factory
        self._tickets = tickets

    # ---------------------------------------------------------------- 聚合查询

    async def queue_summary(
        self,
        tenant_id: uuid.UUID,
        *,
        statuses: tuple[str, ...] = _DEFAULT_STATUSES,
        group_by: str = "subject",
    ) -> list[ReviewQueueGroup]:
        """按 subject 分组聚合复核队列：每组计数 + 最旧 created_at + 样本引用 ≤3 条。

        - 排序：最旧组优先（含 subject 终序防同刻并列不稳定）；
        - 分组键 = (subject, subject_type)（subject_type 为组属性，无损展示；同 subject 异型
          按 (subject, subject_type) 拆组展示，batch_decide 仍按 subject 全组裁决）；
        - 样本 = 组内最旧 ≤3 条（窗口函数单 SQL，免 N+1；PG/SQLite 方言中立 Core）；
        - 空队列返回 []（合法态，前端渲染空工作台）。
        """
        if group_by != "subject":
            raise ValueError("3001 PARAM_INVALID: group_by 仅支持 subject（主题分组随 v1.5 语境术语表落地）")
        conds = (KbFact.tenant_id == tenant_id, KbFact.status.in_(tuple(statuses)))
        async with self._factory() as session:
            oldest = func.min(KbFact.created_at)  # 仅作 DB 侧组排序（ISO 串序≡时序，方言一致）
            groups = (
                await session.execute(
                    select(KbFact.subject, KbFact.subject_type, func.count())
                    .where(*conds)
                    .group_by(KbFact.subject, KbFact.subject_type)
                    .order_by(oldest, KbFact.subject)
                )
            ).all()
            if not groups:
                return []
            # 样本 ≤3：窗口函数单 SQL 取每组最旧 3 行（免 N+1；PG/SQLite 方言中立 Core）。
            # 组最旧时间戳取自 rn=1 样本行（纯 ORM 列，经方言类型处理必得 datetime——
            # func.min 聚合列在 SQLite 下会退化为原始串，故不直接回读）。
            row_number = (
                func.row_number()
                .over(
                    partition_by=(KbFact.subject, KbFact.subject_type),
                    order_by=(KbFact.created_at, KbFact.id),  # id 终序：同刻并列样本不重不漏可复现
                )
                .label("rn")
            )
            sample_rows = (
                await session.execute(
                    select(
                        KbFact.id,
                        KbFact.subject,
                        KbFact.subject_type,
                        KbFact.document_id,
                        KbFact.chunk_id,
                        KbFact.created_at,
                    )
                    .add_columns(row_number)
                    .where(*conds)
                )
            ).all()
        samples: dict[tuple[str, str | None], list[ReviewSampleRef]] = {}
        oldest_of: dict[tuple[str, str | None], datetime] = {}
        for row in sample_rows:
            key = (row.subject, row.subject_type)
            if row.rn == 1:
                oldest_of[key] = row.created_at
            if row.rn > _SAMPLE_LIMIT:
                continue
            samples.setdefault(key, []).append(
                ReviewSampleRef(fact_id=row.id, document_id=row.document_id, chunk_id=row.chunk_id)
            )
        return [
            ReviewQueueGroup(
                subject=subject,
                subject_type=subject_type,
                count=int(count),
                oldest_created_at=oldest_of[(subject, subject_type)],
                samples=tuple(samples.get((subject, subject_type), ())),
            )
            for subject, subject_type, count in groups
        ]

    # ---------------------------------------------------------------- 全组裁决

    async def batch_decide(
        self,
        tenant_id: uuid.UUID,
        *,
        subject: str,
        decision: str,
        reviewer_id: uuid.UUID,
        comment: str | None = None,
        statuses: tuple[str, ...] = _DEFAULT_STATUSES,
    ) -> BatchDecideOutcome:
        """同 subject 全组裁决（decision ∈ authoritative/rejected，映射 kb_facts.status）。

        - 组范围 = 该租户该 subject、status ∈ statuses 的全部行（跨文档——换版场景同 subject
          候选本就散布多文档，批次纪律按 subject 聚拢）；
        - 全组单事务（组级原子：要么全裁要么全不裁）；行内审计 meta["review_queue"] 随裁决
          同事务落行（恒写，无单也有痕）；open 单 payload["decisions"] 增量留痕后置
          best-effort（失败仅告警不回滚已生效裁决）；
        - 空组（subject 不在队列/拼写失误）→ decided=0 幂等返回，不 404（行数即真值）；
        - 非法 decision → ValueError（3001；路由面由 DTO Literal 先行 422，此处服务防御）。
        """
        if decision not in _DECISION_STATUSES:
            raise ValueError(f"3001 PARAM_INVALID: decision 仅支持 {'/'.join(_DECISION_STATUSES)}（收到 {decision!r}）")
        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        select(KbFact)
                        .where(
                            KbFact.tenant_id == tenant_id,
                            KbFact.subject == subject,
                            KbFact.status.in_(tuple(statuses)),
                        )
                        .order_by(KbFact.created_at, KbFact.id)
                        .with_for_update()  # PG 行锁防并发双写穿透（SQLite 无 op 静默忽略）
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return BatchDecideOutcome(subject=subject, decision=decision, decided=0, trail_recorded=0)
            decided_at = datetime.now(UTC).isoformat()
            inline_audit = {
                "decision": decision,
                "reviewer_id": str(reviewer_id),
                "comment": comment,
                "decided_at": decided_at,
                "batch": True,
                "via": "review_queue.batch_decide",
            }
            for fact in rows:
                fact.status = decision
                meta = dict(fact.meta or {})  # JSONB 不可原地变更：整体重赋值（align 等既有键不触碰）
                meta["review_queue"] = inline_audit
                fact.meta = meta
            await session.commit()  # 组级原子持久（先翻转+行内审计，后单据留痕——不产生幽灵审计）
        trail_recorded = 0
        for fact in rows:
            trailed = await self._append_ticket_trail(
                tenant_id,
                fact_id=fact.id,
                decision=decision,
                reviewer_id=reviewer_id,
                comment=comment,
                decided_at=decided_at,
            )
            trail_recorded += int(trailed)
        return BatchDecideOutcome(subject=subject, decision=decision, decided=len(rows), trail_recorded=trail_recorded)

    async def _append_ticket_trail(
        self,
        tenant_id: uuid.UUID,
        *,
        fact_id: uuid.UUID,
        decision: str,
        reviewer_id: uuid.UUID,
        comment: str | None,
        decided_at: str,
    ) -> bool:
        """增量单据留痕（复用 ReviewTicketService 面）：仅 open 单追加，终态单/无单/故障跳过。

        行内审计已在裁决事务落行，此处失败不致命——宁可缺单据痕不回滚裁决（kb 工作台同款）。
        """
        tickets = self._tickets
        if tickets is None:
            return False
        try:
            ticket = await tickets.get_latest_ticket(
                tenant_id=tenant_id, target_type=_TICKET_TARGET_TYPE, target_id=fact_id
            )
            if ticket is None or ticket["status"] not in _TICKET_OPEN_STATUSES:
                return False
            payload = dict(ticket["payload"] or {})
            decisions = list(payload.get("decisions") or [])
            decisions.append(
                {
                    "action": "accept" if decision == "authoritative" else "reject",
                    "batch": True,
                    "candidate_id": str(fact_id),
                    "decided_by": str(reviewer_id),
                    "comment": comment,
                    "decided_at": decided_at,
                }
            )
            payload["decisions"] = decisions
            await tickets.merge_payload(tenant_id=tenant_id, ticket_id=ticket["id"], payload=payload)
        except Exception:  # noqa: BLE001 — 票据面故障不阻断裁决（审计缺口走日志告警）
            logger.warning("review queue ticket trail failed: fact_id=%s", fact_id, exc_info=True)
            return False
        return True

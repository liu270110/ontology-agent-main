"""kb 知识治理 ORM：事实关系表 kb_fact_relations / 冲突工单表 kb_conflicts（2 表）。

设计权威：docs/OntRAG/知识库GraphRAG设计.md §8.1（冲突分诊四型四处置）/ §8.2（失效分边
三语义）。lite 档落位（2026-09-29 波次④）：设计三边（SUPERSEDES/OUTRANKED_BY/INVALIDATED_BY）
在 lite 以 PG 关系表承载（枚举值同名小写下划线），方向 = from（失效方）→ to（接任方）：
from superseded_by to / from outranked_by to / from invalidated_by to，三种边不复用、机器裁决
结果不混入版本演变史（§8.2 边语义纪律）。

DDL 权威 database/01 回填待办（OntRAG §11 已登记，DB owner 认领），本 ORM 按设计先行落地；
约束命名沿用 services/platform/db/base.py 约定。
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from services.platform.db.base import Base, PkMixin, TenantMixin, TimestampMixin


class KbFactRelation(Base, PkMixin, TenantMixin, TimestampMixin):
    """失效分边（§8.2 三语义不复用）：版本承接 / 裁决淘汰 / 无替代撤回。

    uk(from, to, relation) 幂等：承接判定/T2 裁决重放不重连边（失效=封口不物理删的配对物）。
    evidence 记边依据可追溯（lite：T1 承接记 rule/cardinality/successor_group/valid_to——
    kb_facts 双时间线列 DDL 待 DB owner（OntRAG §6/§11），封口时间暂由边 evidence 与事实
    meta 承载；T2 自动档开启后 OUTRANKED_BY 边记 conflict_id/score/decided_by，§8.2）。
    """

    __tablename__ = "kb_fact_relations"
    # from=失效方 → to=接任方（三边语义见类 docstring 与模块头 §8.2）
    from_fact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("kb_facts.id"), nullable=False)
    to_fact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("kb_facts.id"), nullable=False)
    relation: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "relation IN ('superseded_by','outranked_by','invalidated_by')",
            name="ck_kb_fact_relations_relation",
        ),
        UniqueConstraint("from_fact_id", "to_fact_id", "relation", name="uk_kb_fact_relations_from_to_relation"),
        # 「这条事实被谁取代」反查（失效说明面板/时间线通道）；from 侧由 uk 最左前缀覆盖
        Index("ix_kb_fact_relations_to", "to_fact_id", "relation"),
    )


class KbConflict(Base, PkMixin, TenantMixin, TimestampMixin):
    """冲突工单（§8.1）：v1 全人工裁决，score 仅工单内参考排序分、不触发任何自动动作。

    fact_a=候选/新方，fact_b=既有/旧方（分诊与承接判定同口径）；uk(fact_a, fact_b) 幂等，
    工单重放/nightly 补扫不重开单。resolution：winner_a|winner_b=人工点选胜者（败者封口连
    outranked_by 边）、t3_coexist=人工判定限定共存（人工填 scope）、pending=待裁决；
    conflict_type 枚举含 T3 供人工复核补录（lite 分诊只自动开 T2 单：T3 判共存不建单，§8.1）。
    工单流转复用 §7 review_workflow 状态机（对齐 2026-09-26 旧稿合入注记），本表只承载裁决
    结论与参考分明细。
    """

    __tablename__ = "kb_conflicts"
    conflict_type: Mapped[str] = mapped_column(String(2), nullable=False)
    fact_a_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("kb_facts.id"), nullable=False)
    fact_b_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("kb_facts.id"), nullable=False)
    score_a: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)  # 参考分（standards §5.3 同款口径）
    score_b: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    resolution: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))  # 人工裁决人
    comment: Mapped[str | None] = mapped_column(Text)  # 裁决说明（可审计，宪法 5）
    __table_args__ = (
        CheckConstraint("conflict_type IN ('T2','T3')", name="ck_kb_conflicts_conflict_type"),
        CheckConstraint(
            "resolution IN ('winner_a','winner_b','t3_coexist','pending')",
            name="ck_kb_conflicts_resolution",
        ),
        UniqueConstraint("fact_a_id", "fact_b_id", name="uk_kb_conflicts_fact_a_id_fact_b_id"),
        Index("ix_kb_conflicts_queue", "tenant_id", "resolution"),  # 待裁决队列主查询面
    )

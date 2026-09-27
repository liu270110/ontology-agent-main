"""沉淀领域纯函数（06 篇 §5.1 步骤②的确定性捷径 + §5.5 sleep-time 观察固化）。

推理分级：同实体同属性异值的冲突**不自动裁决**——标记 CONFLICT 进待复核；
LLM 裁断（recency+confidence 定新旧）是 plan3 接缝。纯函数，零 I/O。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .memory import MemoryRecord, MemoryType, RecordState


class VerdictKind(StrEnum):
    ADD = "add"  # 无冲突，直接入库
    DUPLICATE = "duplicate"  # 同实体同属性同值，跳过
    CONFLICT = "conflict"  # 同实体同属性异值，进待复核


@dataclass(slots=True)
class ConflictVerdict:
    kind: VerdictKind
    old_record_id: uuid.UUID | None = None  # CONFLICT/DUPLICATE 时指向既有记录


@dataclass(slots=True)
class ObsDraft:
    """观察固化草稿（应用层落 mem:Observation 记录）。"""

    subject_iri: str
    content: str
    proof_count: int
    supported_by: list[uuid.UUID] = field(default_factory=list)


def detect_conflicts(
    new_attr: str,
    new_value: str,
    new_subject: str | None,
    existing: Sequence[MemoryRecord],
    now: datetime,  # 判定基准时刻（保留：plan3 LLM 裁断需要 recency 对比）
) -> ConflictVerdict:
    """确定性冲突捷径：仅对 AttributeFact 语义槽（structured.attribute/value）判定。"""
    if new_subject is None:
        return ConflictVerdict(kind=VerdictKind.ADD)
    for r in existing:
        if (
            r.record_type is MemoryType.FACT_CLAIM
            and r.state is RecordState.ACTIVE
            and r.subject_iri == new_subject
            and r.structured.get("attribute") == new_attr
        ):
            if str(r.structured.get("value")) == str(new_value):
                return ConflictVerdict(kind=VerdictKind.DUPLICATE, old_record_id=r.id)
            return ConflictVerdict(kind=VerdictKind.CONFLICT, old_record_id=r.id)
    return ConflictVerdict(kind=VerdictKind.ADD)


def consolidate_observations(records: Sequence[MemoryRecord], *, min_proof: int, now: datetime) -> list[ObsDraft]:
    """观察固化（Hindsight 机制）：同实体同属性的活跃事实 ≥min_proof 条 → 观察草稿。"""
    groups: dict[tuple[str, str], list[MemoryRecord]] = defaultdict(list)
    for r in records:
        if (
            r.record_type is MemoryType.FACT_CLAIM
            and r.state is RecordState.ACTIVE
            and r.subject_iri is not None
            and r.structured.get("attribute")
        ):
            groups[(r.subject_iri, str(r.structured["attribute"]))].append(r)
    drafts: list[ObsDraft] = []
    for (subject, attr), group in groups.items():
        if len(group) < min_proof:
            continue
        values = sorted({str(g.structured.get("value")) for g in group})
        drafts.append(
            ObsDraft(
                subject_iri=subject,
                content=f"{attr}：{'；'.join(values)}（{len(group)} 条独立事实支持）",
                proof_count=len(group),
                supported_by=[g.id for g in group],
            )
        )
    return drafts

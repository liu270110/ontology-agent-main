"""L4 领域模型：MemoryRecord 聚合（规格：docs/架构设计/06 篇 §3/§4；DDL：database/01 记忆域）。

纪律同 session.py：validate_assignment=True；状态只经聚合方法流转（白名单）；Observation 类型
仅允许后台管线产生（守卫在应用层 MemoryService，领域层不感知来源）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict, Field


class MemoryType(StrEnum):
    """规格 §3.1 七类（mem: IRI 本地名）。"""

    PREFERENCE = "mem:Preference"
    FACT_CLAIM = "mem:FactClaim"
    OBSERVATION = "mem:Observation"
    EPISODE = "mem:Episode"
    DECISION = "mem:Decision"
    GOAL = "mem:Goal"
    PROCEDURE_REF = "mem:ProcedureRef"


class RecordState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"


class MemoryLayer(IntEnum):
    SESSION = 1
    USER = 2
    ORG = 3
    KNOWLEDGE = 4


class MemoryScope(StrEnum):
    PERSONAL = "personal"
    ORG = "org"


_VALID_TRANSITIONS: dict[RecordState, set[RecordState]] = {
    RecordState.ACTIVE: {RecordState.SUPERSEDED, RecordState.INVALIDATED, RecordState.EXPIRED},
    RecordState.SUPERSEDED: set(),
    RecordState.INVALIDATED: set(),
    RecordState.EXPIRED: set(),
}


class MemoryStateError(Exception):
    """非法状态流转（网关映射 4xxx 业务错误）。"""


class MemoryRecord(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    layer: MemoryLayer
    record_type: MemoryType
    subject_iri: str | None = None  # 术语对齐产物；对不齐置空（规格 §5.1）
    content: str = Field(min_length=1)
    structured: dict = Field(default_factory=dict)
    scope: MemoryScope = MemoryScope.PERSONAL
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source_ref: list[dict] = Field(default_factory=list)  # 观察型记录为证据数组
    proof_count: int | None = None  # 仅 mem:Observation 使用
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    superseded_by: uuid.UUID | None = None
    state: RecordState = RecordState.ACTIVE
    decay_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def _transition(self, target: RecordState, now: datetime) -> None:
        if target not in _VALID_TRANSITIONS[self.state]:
            raise MemoryStateError(f"{self.state} -> {target} 不合法")
        self.state = target
        self.updated_at = now

    def supersede(self, by_id: uuid.UUID, now: datetime) -> None:
        """新事实取代旧事实（软删，规格 §4.2 不物理删除）。"""
        self._transition(RecordState.SUPERSEDED, now)
        self.superseded_by = by_id

    def invalidate(self, now: datetime) -> None:
        """本体变更联动/人工下线/遗忘指令（规格 §5.3）。"""
        self._transition(RecordState.INVALIDATED, now)

    def expire(self, now: datetime) -> None:
        """衰减调度置为过期（规格 §5.3）。"""
        self._transition(RecordState.EXPIRED, now)

    def is_injectable(self, now: datetime) -> bool:
        """可注入上下文：active 且双时间线未失效（规格 §5.2 第 4 点）。"""
        if self.state is not RecordState.ACTIVE:
            return False
        return self.valid_to is None or self.valid_to > now

    def decay_score(self, now: datetime, half_life_days: float) -> float:
        """confidence × 时间衰减（规格 §5.3 衰减调度器的打分函数）。"""
        age_days = max((now - self.created_at).total_seconds(), 0.0) / 86400.0
        return self.confidence * 0.5 ** (age_days / half_life_days)

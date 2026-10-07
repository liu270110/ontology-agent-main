"""L4 领域模型：MemoryRecord 聚合（规格：docs/架构设计/06 篇 §3/§4；DDL：database/01 记忆域）。

纪律同 session.py：validate_assignment=True；状态只经聚合方法流转（白名单）；Observation 类型
仅允许后台管线产生（守卫在应用层 MemoryService，领域层不感知来源）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Any, Final

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


DEFAULT_EXPIRY_FLOOR: Final[float] = 0.1  # D-6 软时效地板分缺省（Settings memory_expiry_floor 同源默认）


def expiry_multiplier(valid_to: datetime | None, now: datetime, *, start: datetime, floor: float) -> float:
    """valid_to 剩余寿命连续乘子（D-6 软时效降权，Agent/13 §28；上游 mem0 §6 expiration_date 软时效）。

    - 无时效线（valid_to=None）：恒 1.0，不降权；
    - 未过期：自 start（调用方传记录 created_at）至 valid_to 随剩余寿命消耗线性滑落 1.0 → floor；
    - 已过点：floor 常量（自然时效软过期——仍可注入，得分压至地板）。

    双时间线边界：本乘子只承载"时效到点"一支的自然软化；人工失效（invalidate →
    INVALIDATED 终态）语义严格分离，仍走状态硬门，不经本函数松动。
    start 形参是线性滑落的起点锚——纯 (valid_to, now) 无法定义线性段；取 created_at
    与半衰期 age 同锚（两模型该字段恒非空，语义=整段寿命即滑落窗）。
    """
    if not 0.0 <= floor <= 1.0:
        raise ValueError(f"floor 须在 [0,1] 区间，当前 {floor}")
    if valid_to is None:
        return 1.0
    if now >= valid_to:
        return floor
    span = (valid_to - start).total_seconds()
    if span <= 0:  # 时效窗退化（valid_to ≤ created_at）：无滑落段，按过点口径取地板
        return floor
    progress = (now - start).total_seconds() / span
    return 1.0 - (1.0 - floor) * min(max(progress, 0.0), 1.0)


class MemoryRecord(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    owner_user_id: uuid.UUID | None = None  # 归属用户；L2 画像/预热维度（§9.2-5，c3d5e7f9a1b3）
    layer: MemoryLayer
    record_type: MemoryType
    subject_iri: str | None = None  # 术语对齐产物；对不齐置空（规格 §5.1）
    content: str = Field(min_length=1)
    structured: dict[str, Any] = Field(default_factory=dict)  # JSON 载荷（形状由使用方收窄）
    scope: MemoryScope = MemoryScope.PERSONAL
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source_ref: list[dict[str, Any]] = Field(default_factory=list)  # 观察型记录为证据数组（source_ref 四元组）
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
        """可注入上下文：仅状态硬门（规格 §5.2 第 4 点；valid_to 硬门随 D-6 软时效放宽，Agent/13 §28）。

        K22 起 valid_to 过点不再拒绝注入——自然时效软过期仍可注入，得分经 decay_score 的
        expiry_multiplier 线性滑落/压至地板（消费面 TimeChannel/context 按分吃零改动）。
        终态语义绝不动：INVALIDATED（本体变更联动/人工下线/遗忘指令）、SUPERSEDED、
        EXPIRED（衰减调度终局）一律不可注入——valid_to 双时间线只软化"时效到点"一支，
        与人工失效严格区分。now 形参保留（调用面兼容 + 谓词时间参数化惯例），软化后不再参与判定。
        """
        return self.state is RecordState.ACTIVE

    def half_life_score(self, now: datetime, half_life_days: float) -> float:
        """confidence × 半衰期衰减（纯半衰期分量 = K22 前 decay_score 原语义）。

        衰减调度器过期判定专用（tasks.decay_scan_task）：EXPIRED 自然衰减终局与 D-6
        软时效降权分离——调度打分不吃软时效乘子，终态语义零漂移。
        """
        age_days = max((now - self.created_at).total_seconds(), 0.0) / 86400.0
        return float(self.confidence * 0.5 ** (age_days / half_life_days))

    def decay_score(self, now: datetime, half_life_days: float, *, expiry_floor: float = DEFAULT_EXPIRY_FLOOR) -> float:
        """排序/召回得分 = half_life_score × expiry_multiplier（D-6 软时效，Agent/13 §28）。

        valid_to 未过点线性滑落、过点压至 expiry_floor（缺省 0.1，Settings
        memory_expiry_floor 同源可配）；TimeChannel 排序零改动按分吃。"""
        return float(
            self.half_life_score(now, half_life_days)
            * expiry_multiplier(self.valid_to, now, start=self.created_at, floor=expiry_floor)
        )

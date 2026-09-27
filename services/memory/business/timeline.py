"""事实变更时间线（api/01 §5.5 GET /memory/facts/{id}/timeline；FR-MEM-06 全程留痕）。

纯函数（无 IO）：仓储 chain_for_user 给出按代际从旧到新的链上事实（supersedes 版本链），
本模块投影为事件流——产生（created）→ 被取代（superseded）→ 失效（invalidated），
墓碑留痕全程可审计回放（memory §4：不物理删除，UPDATE/DELETE 均为版本翻转）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from services.memory.domain.model.l2_fact import FactStatus, L2Fact

_EVENT_CREATED: Final[str] = "created"
_EVENT_SUPERSEDED: Final[str] = "superseded"
_EVENT_INVALIDATED: Final[str] = "invalidated"


class TimelineEvent(BaseModel):
    """单条时间线事件（api/01 §6.6 精神：留痕三元组=类型/时刻/事实指针）。"""

    model_config = ConfigDict(frozen=True)

    type: str  # created | superseded | invalidated
    at: datetime
    fact_id: UUID
    superseded_by: UUID | None = None  # 仅 superseded：新事实指针（版本链前进方向）
    note: str = ""


class FactTimeline(BaseModel):
    """GET /memory/facts/{id}/timeline 载荷（业务层形状；DTO 层同构透出）。"""

    model_config = ConfigDict(frozen=True)

    fact_id: UUID
    chain: list[UUID]  # 版本链（从旧到新，含本事实）
    events: list[TimelineEvent]  # 全程留痕事件（时间升序）


def build_timeline(chain: list[L2Fact], fact_id: UUID) -> FactTimeline:
    """版本链事实 → 时间线（事件时间升序；同刻按 created→superseded→invalidated 稳定序）。

    fact_id=请求锚定事实（可位于链中段）；chain=仓储 chain_for_user 产物（从旧到新）。
    """
    if not chain:
        raise ValueError("chain 为空：timeline 锚定事实必须存在（端点层先 404）")
    _ORDER = {_EVENT_CREATED: 0, _EVENT_SUPERSEDED: 1, _EVENT_INVALIDATED: 2}
    events: list[TimelineEvent] = []
    for fact in chain:
        events.append(TimelineEvent(type=_EVENT_CREATED, at=fact.created_at, fact_id=fact.id))
        if fact.status is FactStatus.SUPERSEDED:
            events.append(
                TimelineEvent(
                    type=_EVENT_SUPERSEDED,
                    at=fact.updated_at,
                    fact_id=fact.id,
                    superseded_by=fact.supersedes_id,
                )
            )
        if fact.status is FactStatus.INVALIDATED:
            events.append(TimelineEvent(type=_EVENT_INVALIDATED, at=fact.valid_to or fact.updated_at, fact_id=fact.id))
    events.sort(key=lambda e: (e.at, _ORDER[e.type]))
    return FactTimeline(fact_id=fact_id, chain=[fact.id for fact in chain], events=events)

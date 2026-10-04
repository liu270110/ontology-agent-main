"""ORSI 能力仓储端口（组合根/路由装配绑 PG 实现；测试用内存假体）。

租户作用域纪律（06 篇 §4 同 plugin 先例）：构造期绑定 tenant_id，方法级不再收租户参数
——跨租户读写从端口形状上不可表达。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from services.rsi.domain.orsi import GapFaceTrack, OrsiCapability, OrsiCapabilityStatus
from services.rsi.surfaces import EvolutionSurface


@dataclass(slots=True)
class OrsiCapabilityFilter:
    """列表过滤（Agent14 §3：进化面/缺口轨/状态过滤）+ 偏移分页（None=不过滤）。"""

    face: EvolutionSurface | None = None
    track: GapFaceTrack | None = None
    status: OrsiCapabilityStatus | None = None
    offset: int = 0
    limit: int = 20


@runtime_checkable
class OrsiCapabilityRepository(Protocol):
    """ORSI 能力仓储端口（读写均限构造期租户；软删行对读面不可见）。"""

    async def add(self, capability: OrsiCapability) -> None: ...  # pragma: no cover — Protocol 无实现

    async def get(self, capability_id: uuid.UUID) -> OrsiCapability | None: ...  # pragma: no cover

    async def list(
        self, filter_: OrsiCapabilityFilter
    ) -> tuple[list[OrsiCapability], int]: ...  # pragma: no cover  (items, total)

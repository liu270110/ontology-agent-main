"""PG 真实通道（v1 四策略先实装两条；向量/图通道在计划 3 接入同一协议）。"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from services.memory.business.retrieval_rrf import rrf_merge, rrf_scores, stale_observation_ids
from services.memory.data.repositories.records_repo import MemoryRepository
from services.memory.domain.model.memory import MemoryLayer, MemoryRecord

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Scored:
    record_id: uuid.UUID
    score: float
    record: MemoryRecord


class RecallChannel(Protocol):
    """召回通道协议（规格 §5.2：任一通道不可用即跳过，RRF 按剩余通道融合=§9.4-7）。"""

    name: str

    async def recall(self, tenant_id: uuid.UUID, text_q: str, limit: int, *, now: datetime) -> list[MemoryRecord]: ...


class KeywordChannel:
    name = "keyword"

    def __init__(self, repo: MemoryRepository) -> None:
        self._repo = repo

    async def recall(self, tenant_id: uuid.UUID, text_q: str, limit: int, *, now: datetime) -> list[MemoryRecord]:
        return await self._repo.search_keyword(tenant_id, text_q=text_q, limit=limit)


class TimeChannel:
    """时间通道：近期高置信（decay 打分排序，规格 §5.3 衰减函数复用）。"""

    name = "time"

    def __init__(self, repo: MemoryRepository, half_life_days: float) -> None:
        self._repo = repo
        self._half_life = half_life_days

    async def recall(self, tenant_id: uuid.UUID, text_q: str, limit: int, *, now: datetime) -> list[MemoryRecord]:
        recent = await self._repo.list_recent(tenant_id, subject_user_layer=MemoryLayer.USER, limit=limit * 3)
        recent.sort(key=lambda r: r.decay_score(now, self._half_life), reverse=True)
        return recent[:limit]


async def search_by_channels(
    repo: MemoryRepository,
    channels: list[RecallChannel],
    *,
    tenant_id: uuid.UUID,
    text_q: str,
    limit: int,
    rrf_k: int,
    now: datetime,
    layer: int | None = None,
) -> list[Scored]:
    """通道顺序召回（v1 并发量足够；通道异常即跳过=§9.4-7 降级）→ 层过滤 → RRF → 新鲜度过滤。

    layer 非空时在通道召回完成后、融合前按记录层剔除（v1 召回后过滤近似；pushdown 到存储层在计划 3）。
    """
    per_channel: dict[str, list[uuid.UUID]] = {}
    pool: dict[uuid.UUID, MemoryRecord] = {}
    for ch in channels:
        try:
            hits = await ch.recall(tenant_id, text_q, limit, now=now)
        except Exception as exc:  # 通道故障降级（规格 §9.4-7），可观测不阻断
            logger.warning("memory recall channel %s failed: %s", ch.name, exc)
            continue
        per_channel[ch.name] = [r.id for r in hits]
        pool.update({r.id: r for r in hits})
    if layer is not None:
        per_channel = {name: [rid for rid in ids if pool[rid].layer == layer] for name, ids in per_channel.items()}
        pool = {rid: rec for rid, rec in pool.items() if rec.layer == layer}
    scores = rrf_scores(per_channel, k=rrf_k)
    ordered = rrf_merge(per_channel, k=rrf_k)

    records = list(pool.values())
    stale = stale_observation_ids(records, now)
    out: list[Scored] = []
    for rid in ordered:
        if rid in stale:
            continue
        out.append(Scored(record_id=rid, score=scores[rid], record=pool[rid]))
    return out[:limit]

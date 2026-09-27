"""检索纯函数：RRF 融合 + 新鲜度降权（规格 06 篇 §5.2；推理分级=确定性规则，无 LLM）。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID

from services.memory.domain.model.memory import MemoryRecord, MemoryType, RecordState


def rrf_scores(channels: Mapping[str, Sequence[UUID]], k: int = 60) -> dict[UUID, float]:
    """score(d)=Σ 1/(k+rank)，完整保留各通道贡献（可解释融合分）。"""
    scores: dict[UUID, float] = defaultdict(float)
    for ranked in channels.values():
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] += 1.0 / (k + rank)
    return dict(scores)


def rrf_merge(channels: Mapping[str, Sequence[UUID]], k: int = 60) -> list[UUID]:
    """多通道排序融合（基于 rrf_scores）；同分按首次出现序（规格 §9.4-7：通道缺失即跳过）。"""
    scores = rrf_scores(channels, k)
    first_seen: dict[UUID, int] = {}
    for ranked in channels.values():
        for doc_id in ranked:
            first_seen.setdefault(doc_id, len(first_seen))
    return sorted(scores, key=lambda d: (-scores[d], first_seen[d]))


def stale_observation_ids(records: Sequence[MemoryRecord], now: datetime) -> set[UUID]:
    """新鲜度降权（规格 §5.2 第 5 点）：subject 存在晚于观察 created_at 的活跃事实且该观察
    尚未被固化刷新时，观察视为 stale。纯集合运算，调用方决定降权或回退。now 是调用方统一的
    判定基准时刻（本函数用 created_at 互比较，固化状态由调用方判定）。"""
    latest_active_fact: dict[str, datetime] = {}
    for r in records:
        if r.record_type is MemoryType.FACT_CLAIM and r.state is RecordState.ACTIVE and r.subject_iri is not None:
            prev = latest_active_fact.get(r.subject_iri)
            if prev is None or r.created_at > prev:
                latest_active_fact[r.subject_iri] = r.created_at

    stale: set[UUID] = set()
    for r in records:
        if r.record_type is not MemoryType.OBSERVATION or r.state is not RecordState.ACTIVE:
            continue
        if r.subject_iri is None:
            continue
        newest_fact = latest_active_fact.get(r.subject_iri)
        if newest_fact is not None and newest_fact > r.created_at:
            stale.add(r.id)
    return stale
